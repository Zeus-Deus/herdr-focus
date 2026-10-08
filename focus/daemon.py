"""The one resident process: Herdr events in, display tokens and an Agents view out.

Event-driven: Herdr lifecycle and status events trigger a debounced snapshot, the
projection is diffed and only changed tokens are reported. No model call, Git fetch or
process scan happens on that path; titles run on a worker thread.
"""

import fcntl
import json
import os
import selectors
import socket
import subprocess
import sys
import time
import traceback

from . import attention as A
from . import config as configmod
from . import providers, state, tasks, titles, triage, transport
from .adapters import for_agent

SOURCE_PREFIX = "plugin:"
LIFECYCLE = [
    "workspace.created", "workspace.updated", "workspace.renamed", "workspace.moved",
    "workspace.reordered", "workspace.closed", "workspace.focused",
    "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
    "pane.created", "pane.closed", "pane.updated", "pane.focused", "pane.moved",
    "pane.exited", "pane.agent_detected",
]
PANE_TOKENS = ("ft_hot", "ft", "ft_quiet", "fstatus", "fwatch", "frank", "fhide")
WORKSPACE_TOKENS = ("fws", "fwswatch")
DEBOUNCE = 0.12
TICK = 30.0
TASK_POLL = 3.0
PLUGIN_CHECK = 60.0
RECONNECT_FOR = 60.0
LOG_LIMIT = 512 * 1024


def nerd_font_available():
    try:
        out = subprocess.run(["fc-list", ":", "family"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return b"nerd font" in out.lower()


def resolve_glyphs(config):
    wanted = config["glyphs"]
    nerd = None
    out = {}
    for name, nerd_glyph, plain in (("watch", "", "◉"), ("pin", "", "◆")):
        value = wanted.get(name, "auto")
        if value == "auto":
            if nerd is None:
                nerd = nerd_font_available()
            value = nerd_glyph if nerd else plain
        out[name] = value
    return out


def view_query(source, show_hidden, layout):
    sort = [{"field": {"token": "frank"}, "order": "asc"}]
    if layout == "attention":
        sort.append({"field": "state_change_seq", "order": "desc"})
    sort += [{"field": "workspace_order"}, {"field": "tab_order"}, {"field": "pane_order"}]
    params = {"source": source, "label": "focus", "sort": sort}
    if not show_hidden:
        params["filter"] = {"op": "any", "filters": [
            {"op": "not", "filter": {"op": "exists", "field": {"token": "fhide"}}},
            {"op": "eq", "field": "status", "value": "blocked"},
        ]}
    return params


def clear_tokens(request, source):
    """Clear every token this plugin reported and its Agents view. request(method, params)."""
    snap = request("session.snapshot", {}) or {}
    snap = snap.get("snapshot", snap)
    for pane in snap.get("panes", []):
        request("pane.report_metadata", {"pane_id": pane["pane_id"], "source": source,
                                         "tokens": {k: None for k in PANE_TOKENS}})
    for ws in snap.get("workspaces", []):
        request("workspace.report_metadata", {"workspace_id": ws["workspace_id"], "source": source,
                                              "tokens": {k: None for k in WORKSPACE_TOKENS}})
    request("agent.view.clear", {"source": source})


class Daemon:
    def __init__(self, socket_path, state_dir, plugin_id):
        self.socket_path = socket_path
        self.plugin_id = plugin_id
        self.source = SOURCE_PREFIX + plugin_id
        self.dir = os.path.join(state_dir, state.scope_id(socket_path))
        os.makedirs(self.dir, exist_ok=True)
        self.log_path = os.path.join(self.dir, "daemon.log")
        self.control_path = os.path.join(self.dir, "control.sock")
        self.store = state.Store(self.dir)
        self.selector = selectors.DefaultSelector()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.lifecycle = None
        self.status_sub = None
        self.status_panes = frozenset()
        self.snapshot = None
        self.published = {}         # pane_id -> {token: value}
        self.published_ws = {}      # workspace_id -> {token: value}
        self.rows = {}              # pane_id -> row dict for the menu and actions
        self.intents = {}           # record key -> (checked_at, messages)
        self.title_errors = {}
        self.refresh_at = 0.0
        self.next_tick = 0.0
        self.next_task_poll = 0.0
        self.next_plugin_check = time.time() + PLUGIN_CHECK
        self.save_at = None
        self.running = True
        self.disconnected_since = None
        self.reload_config()

    # ---- setup -------------------------------------------------------------------------

    def log(self, message):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_LIMIT:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a") as handle:
                handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass

    def reload_config(self):
        self.config, error = configmod.load()
        if error:
            self.log("config error, using defaults: " + error)
        self.config_error = error
        self.glyphs = resolve_glyphs(self.config)
        self.watches = tasks.Watches(self.config)
        self.provider = None
        if self.config["titles"]["enabled"]:
            try:
                self.provider = providers.build(self.config)
            except ValueError as exc:
                self.log("title backend disabled: %s" % exc)
        old = getattr(self, "worker", None)
        self.worker = None
        if self.provider is not None:
            self.worker = titles.Worker(self.provider, self.config["titles"]["max_chars"],
                                        self.config["titles"]["max_per_minute"], self.poke)
        if old is not None:
            # The old thread keeps finishing its job; its results are re-checked like any other.
            self.stale_workers = getattr(self, "stale_workers", []) + [old]
        self.published.clear()
        self.published_ws.clear()

    def poke(self):
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def acquire_lock(self):
        self.lock_handle = open(os.path.join(self.dir, "daemon.lock"), "a+")
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(str(os.getpid()))
        self.lock_handle.flush()
        return True

    def start_control(self):
        try:
            os.unlink(self.control_path)
        except FileNotFoundError:
            pass
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.control.bind(self.control_path)
        os.chmod(self.control_path, 0o600)
        self.control.listen(8)
        self.control.setblocking(False)
        self.selector.register(self.control, selectors.EVENT_READ, "control")
        self.selector.register(self.wake_r, selectors.EVENT_READ, "wake")

    def connect(self):
        self.close_subs()
        self.lifecycle = transport.Subscription([{"type": t} for t in LIFECYCLE], self.socket_path)
        self.selector.register(self.lifecycle, selectors.EVENT_READ, "lifecycle")
        self.status_panes = frozenset()
        self.published.clear()
        self.published_ws.clear()
        self.disconnected_since = None
        self.apply_view()
        self.refresh()
        self.log("connected to %s" % self.socket_path)

    def close_subs(self):
        for sub in (self.lifecycle, self.status_sub):
            if sub is not None:
                try:
                    self.selector.unregister(sub)
                except (KeyError, ValueError):
                    pass
                sub.close()
        self.lifecycle = self.status_sub = None

    def resubscribe_status(self, pane_ids):
        pane_ids = frozenset(pane_ids)
        if pane_ids == self.status_panes and self.status_sub is not None:
            return
        if self.status_sub is not None:
            self.selector.unregister(self.status_sub)
            self.status_sub.close()
            self.status_sub = None
        self.status_panes = pane_ids
        if not pane_ids:
            return
        subs = [{"type": "pane.agent_status_changed", "pane_id": p} for p in sorted(pane_ids)]
        self.status_sub = transport.Subscription(subs, self.socket_path)
        self.selector.register(self.status_sub, selectors.EVENT_READ, "status")

    def apply_view(self):
        if not self.config["sidebar"]["agent_view"]:
            self.request("agent.view.clear", {"source": self.source})
            return
        params = view_query(self.source, self.store.view.get("show_hidden"),
                            self.config["sidebar"]["layout"])
        self.request("agent.view.set", params)

    def request(self, method, params):
        try:
            return transport.request(method, params, self.socket_path)
        except transport.HerdrError as exc:
            self.log("%s failed: %s" % (method, exc))
        except OSError as exc:
            self.log("%s failed: %s" % (method, exc))
            self.disconnected_since = self.disconnected_since or time.time()
        return None

    # ---- reconcile ---------------------------------------------------------------------

    def refresh(self):
        self.refresh_at = 0.0
        result = self.request("session.snapshot", {})
        if not result:
            return
        self.snapshot = result.get("snapshot", result)
        now = time.time()
        panes = {p["pane_id"]: p for p in self.snapshot.get("panes", [])}
        agents = self.snapshot.get("agents", [])
        live = set()
        rows = {}
        for agent in agents:
            pane = dict(panes.get(agent["pane_id"], {}))
            pane.update(agent)
            row = self.reconcile_agent(pane, now)
            rows[pane["pane_id"]] = row
            live.add(row["key"])
        # Agents that left their pane take their background evidence with them.
        for key in list(self.watches.readers):
            if key not in live:
                self.watches.drop(key)
        self.rows = rows
        self.store.prune(live, now)
        try:
            self.resubscribe_status(panes.keys())
        except (OSError, transport.HerdrError) as exc:
            self.log("status subscription failed: %s" % exc)
            self.status_panes = frozenset()
            self.schedule_refresh(2.0)
        self.publish(panes, now)

    def reconcile_agent(self, pane, now):
        key = state.record_key(pane)
        terminal_key = "terminal:%s" % pane.get("terminal_id")
        if key != terminal_key:
            self.store.rekey(terminal_key, key)
        # A different conversation in the same terminal is a new record; the old session's
        # tasks are stale from now on.
        for other_key, other in self.store.records.items():
            if (other_key != key and other.get("terminal_id") == pane.get("terminal_id")
                    and other.get("pane_id") is not None):
                other["pane_id"] = None
                self.watches.drop(other_key)
        record = self.store.get(key)
        if record.get("terminal_id") and record["terminal_id"] != pane.get("terminal_id"):
            # Same conversation, new terminal: the agent was relaunched (e.g. resumed after a
            # server restart), so background tasks of the old process are gone.
            record["evidence_since"] = now
            self.watches.drop(key)
            self.mark_dirty()
        record.update(pane_id=pane["pane_id"], terminal_id=pane.get("terminal_id"),
                      workspace_id=pane.get("workspace_id"), agent=pane.get("agent"),
                      last_live=now)

        status = pane.get("agent_status") or "unknown"
        seq = pane.get("state_change_seq")
        if status == "done" and record.get("ack_seq") is not None and record["ack_seq"] == seq:
            status = "idle"
        focused = pane["pane_id"] == self.snapshot.get("focused_pane_id")
        previous = record.get("status")
        if previous != status:
            triage.note_status(record, status, previous, now, focused)
            bucket = lambda s: "idle" if s in ("idle", "done") else s
            if previous is None:
                record["status_since"] = None   # unknown start: show no fabricated time
            elif bucket(previous) != bucket(status):
                record["status_since"] = now
            record["status"] = status
            self.mark_dirty()
        elif focused and self.config["triage"]["ack_on_focus"] and triage.note_focus(record):
            self.mark_dirty()

        adapter = for_agent(pane.get("agent"))
        path = adapter.transcript(pane.get("agent_session")) if adapter else None
        self.watches.bind(key, pane.get("agent"), path)
        self.watches.poll(key, now)
        messages = self.intent(key, adapter, path, now)
        title = self.title_for(record, pane, messages)
        return {"key": key, "pane": pane, "status": status, "seq": seq, "title": title,
                "adapter": adapter, "path": path}

    def intent(self, key, adapter, path, now):
        checked = self.intents.get(key)
        if checked and checked[1]:
            return checked[1]
        if checked and now - checked[0] < 10:
            return checked[1]
        messages = []
        if adapter and path:
            messages = adapter.user_messages(path, self.config["titles"]["context_chars"])
        self.intents[key] = (now, messages)
        return messages

    def title_for(self, record, pane, messages):
        max_chars = self.config["titles"]["max_chars"]
        if record.get("title_owner") == "manual" and record.get("title"):
            return record["title"]
        if pane.get("label"):
            return titles.clip(pane["label"], max_chars)   # a Herdr pane name is manual too
        if messages and self.worker is not None and record.get("title_owner") != "generated":
            # Initial naming, once, from the first prompt. Later prompts never rename the
            # row; "generate a new title" does, with more of the conversation.
            context = titles.context_from(messages[:1], self.config["titles"]["context_chars"])
            digest = titles.input_hash(context)
            cached = self.store.title_cache.get(digest)
            if record.get("title_input") != digest:
                if cached:
                    self.set_generated(record, cached, digest)
                elif digest not in self.title_errors:
                    self.worker.submit(record["key"], record["title_generation"], digest, context)
        if record.get("title_owner") == "generated" and record.get("title"):
            return record["title"]
        title = titles.fallback(pane, messages, max_chars)
        if record.get("title") != title or record.get("title_owner") != "fallback":
            record["title"], record["title_owner"] = title, "fallback"
            self.mark_dirty()
        return title

    def set_generated(self, record, title, digest):
        record["title"] = title
        record["title_owner"] = "generated"
        record["title_input"] = digest
        self.mark_dirty()

    def take_titles(self):
        changed = False
        for worker in [self.worker] + getattr(self, "stale_workers", []):
            if worker is None:
                continue
            for key, generation, digest, title, error in worker.drain():
                record = self.store.get(key, create=False)
                if error:
                    self.title_errors[digest] = error
                    self.log("title for %s kept its fallback (%s)" % (key, error))
                    continue
                self.store.title_cache[digest] = title
                if len(self.store.title_cache) > 500:
                    self.store.title_cache.pop(next(iter(self.store.title_cache)))
                if (record is None or record.get("title_owner") == "manual"
                        or record.get("title_generation") != generation):
                    self.log("dropped a late title for %s" % key)
                    continue
                self.set_generated(record, title, digest)
                changed = True
        if changed:
            self.schedule_refresh(0)

    # ---- projection --------------------------------------------------------------------

    def publish(self, panes, now):
        layout = self.config["sidebar"]["layout"]
        elapsed = self.config["sidebar"]["elapsed"]
        days = self.config["triage"]["auto_settle_days"]
        per_workspace = {}
        for pane_id, row in self.rows.items():
            record = self.store.records[row["key"]]
            watches = self.watches.count(row["key"], now, record.get("evidence_since"))
            cat = A.category(row["status"], record, watches)
            if triage.due_wake(record, now):
                triage.wake(record, cat, row["seq"], reason="timer", now=now)
                self.mark_dirty()
            elif triage.auto_settle_due(record, cat, watches, now, days):
                triage.settle(record, cat, row["seq"], now)
                self.mark_dirty()
            focused = pane_id == self.snapshot.get("focused_pane_id")
            cat, tokens = A.project(record, row["status"], focused,
                                    watches, row["title"], now, self.glyphs, layout, elapsed)
            row.update(category=cat, tokens=tokens, watches=watches)
            self.report_pane(pane_id, tokens)
            per_workspace.setdefault(row["pane"].get("workspace_id"), []).append(
                (cat, watches, bool(record.get("manual_watch"))))
        # Panes that stopped hosting an agent lose our tokens.
        for pane_id in list(self.published):
            if pane_id not in self.rows:
                if pane_id in panes:
                    self.report_pane(pane_id, {k: None for k in PANE_TOKENS})
                self.published.pop(pane_id, None)
        for workspace in self.snapshot.get("workspaces", []):
            workspace_id = workspace["workspace_id"]
            tokens = A.rollup(per_workspace.get(workspace_id, []), self.glyphs)
            self.report_workspace(workspace_id, tokens)

    def report_pane(self, pane_id, tokens):
        current = self.published.get(pane_id)
        patch = {k: v for k, v in tokens.items() if current is None or current.get(k) != v}
        if current is None:
            # First report after (re)connect: clear anything a previous run left behind.
            patch = dict(tokens)
        if not patch:
            return
        if self.request("pane.report_metadata",
                        {"pane_id": pane_id, "source": self.source, "tokens": patch}) is not None:
            self.published[pane_id] = dict(tokens)

    def report_workspace(self, workspace_id, tokens):
        current = self.published_ws.get(workspace_id)
        patch = tokens if current is None else {
            k: v for k, v in tokens.items() if current.get(k) != v}
        if not patch:
            return
        if self.request("workspace.report_metadata",
                        {"workspace_id": workspace_id, "source": self.source,
                         "tokens": patch}) is not None:
            self.published_ws[workspace_id] = dict(tokens)

    def clear_all(self):
        """Remove every token and the owned view (used on uninstall/disable)."""
        clear_tokens(self.request, self.source)

    # ---- actions -----------------------------------------------------------------------

    def schedule_refresh(self, delay=DEBOUNCE):
        at = time.time() + delay
        if not self.refresh_at or at < self.refresh_at:
            self.refresh_at = at

    def mark_dirty(self):
        self.store.dirty = True
        if self.save_at is None:
            self.save_at = time.time() + 1.0

    def resolve(self, request):
        pane_id = request.get("pane_id")
        row = self.rows.get(pane_id)
        if row is None:
            raise triage.Refused("no agent in pane %s" % (pane_id or "?"))
        if request.get("key") and request["key"] != row["key"]:
            raise triage.Refused("that pane now hosts a different conversation")
        return row, self.store.records[row["key"]]

    def act(self, request):
        action = request.get("action")
        now = time.time()
        if action == "undo":
            label = self.store.pop_undo()
            self.mark_dirty()
            self.schedule_refresh(0)
            return "Undid: %s" % label if label else "Nothing to undo"
        if action == "show-hidden":
            self.store.view["show_hidden"] = not self.store.view.get("show_hidden")
            self.mark_dirty()
            self.apply_view()
            return "Showing settled and snoozed" if self.store.view["show_hidden"] else "Hiding settled and snoozed"
        if action == "next":
            return self.focus_next(request.get("pane_id"))
        if action == "settle-idle":
            return self.settle_idle(request.get("workspace_id"), now)
        if action == "reload":
            self.reload_config()
            self.apply_view()
            self.schedule_refresh(0)
            return "Config reloaded" + (" with errors: %s" % self.config_error if self.config_error else "")

        row, record = self.resolve(request)
        cat = row.get("category") or A.category(row["status"], record, 0)
        seq = row.get("seq")
        if action == "rename":
            title = titles.clip(" ".join((request.get("title") or "").split()),
                                self.config["titles"]["max_chars"])
            if not title:
                raise triage.Refused("empty title")
            record.update(title=title, title_owner="manual")
            record["title_generation"] += 1
            message = "Renamed"
        elif action == "title-reset":
            record.update(title=None, title_owner=None, title_input=None)
            record["title_generation"] += 1
            message = "Title reset"
        elif action == "title-regenerate":
            if self.worker is None:
                raise triage.Refused("no title backend configured (titles.backend = \"none\")")
            record.update(title_owner=None if record.get("title_owner") == "manual"
                          else record.get("title_owner"), title_input=None)
            record["title_generation"] += 1
            self.intents.pop(row["key"], None)
            messages = self.intent(row["key"], row["adapter"], row["path"], now)
            if not messages:
                raise triage.Refused("no user prompt found for this agent yet")
            context = titles.context_from(messages, self.config["titles"]["context_chars"])
            digest = titles.input_hash(context)
            self.store.title_cache.pop(digest, None)
            self.title_errors.pop(digest, None)
            self.worker.submit(row["key"], record["title_generation"], digest, context)
            message = "Generating a new title"
        else:
            before = self.store.snapshot([row["key"]])
            if action == "toggle-unread":
                message = triage.toggle_unread(record, cat, seq)
            elif action == "unread":
                message = triage.mark_unread(record, cat, seq)
            elif action == "read":
                message = triage.mark_read(record, cat, seq)
            elif action == "toggle-settle":
                message = triage.toggle_settle(record, cat, seq, now)
            elif action == "settle":
                message = triage.settle(record, cat, seq, now)
            elif action == "unsettle":
                message = triage.unsettle(record, cat, seq)
            elif action == "snooze":
                until = float(request.get("until") or now + 3600)
                message = triage.snooze(record, cat, seq, until)
            elif action == "wake":
                message = triage.wake(record, cat, seq, "user", now)
            elif action == "pin":
                message = triage.toggle_pin(record, cat, seq)
            elif action == "watch":
                message = triage.toggle_watch(record, cat, seq)
            else:
                raise triage.Refused("unknown action %r" % action)
            self.store.push_undo(message, before)
            del self.store.undo[:-int(self.config["triage"]["undo_depth"])]
        self.mark_dirty()
        self.schedule_refresh(0)
        return message

    def settle_idle(self, workspace_id, now):
        keys = [row["key"] for row in self.rows.values()
                if (workspace_id is None or row["pane"].get("workspace_id") == workspace_id)
                and row.get("category") == A.IDLE and not row.get("watches")
                and not self.store.records[row["key"]].get("pinned")
                and not self.store.records[row["key"]].get("settled")]
        if not keys:
            return "Nothing idle to settle"
        before = self.store.snapshot(keys)
        for key in keys:
            triage.settle(self.store.records[key], A.IDLE, None, now)
        self.store.push_undo("Settle %d idle" % len(keys), before)
        self.mark_dirty()
        self.schedule_refresh(0)
        return "Settled %d idle agent%s" % (len(keys), "" if len(keys) == 1 else "s")

    def focus_next(self, current_pane):
        def order(row):
            record = self.store.records[row["key"]]
            if row.get("category") == A.NEEDS:
                return (0, record.get("status_since") or 0)
            return (1, record.get("status_since") or 0)
        wanted = [r for r in self.rows.values() if r.get("category") in (A.NEEDS, A.REVIEW, A.UNREAD)]
        if not wanted:
            return "Nothing needs you"
        wanted.sort(key=order)
        ids = [r["pane"]["pane_id"] for r in wanted]
        target = ids[(ids.index(current_pane) + 1) % len(ids)] if current_pane in ids else ids[0]
        if self.request("agent.focus", {"target": target}) is None:
            raise triage.Refused("could not focus %s" % target)
        return "Focused %s" % target

    def menu_rows(self):
        out = []
        workspaces = {w["workspace_id"]: w for w in (self.snapshot or {}).get("workspaces", [])}
        now = time.time()
        for row in self.rows.values():
            record = self.store.records[row["key"]]
            tokens = row.get("tokens") or {}
            out.append({
                "key": row["key"], "pane_id": row["pane"]["pane_id"],
                "workspace_id": row["pane"].get("workspace_id"),
                "workspace": (workspaces.get(row["pane"].get("workspace_id")) or {}).get("label"),
                "agent": row["pane"].get("display_agent") or row["pane"].get("agent"),
                "title": row["title"], "title_owner": record.get("title_owner"),
                "category": row.get("category"), "status": tokens.get("fstatus"),
                "watch": tokens.get("fwatch"), "rank": tokens.get("frank"),
                "hidden": bool(tokens.get("fhide")), "pinned": bool(record.get("pinned")),
                "focused": row["pane"]["pane_id"] == (self.snapshot or {}).get("focused_pane_id"),
                "tasks": self.watches.describe(row["key"], now, record.get("evidence_since")),
            })
        out.sort(key=lambda r: (r["rank"] or "9", r["workspace_id"] or "", r["pane_id"]))
        return out

    def status(self):
        return {
            "pid": os.getpid(), "socket": self.socket_path, "state_dir": self.dir,
            "agents": len(self.rows), "records": len(self.store.records),
            "titles": titles.env_summary(self.provider),
            "layout": self.config["sidebar"]["layout"],
            "show_hidden": bool(self.store.view.get("show_hidden")),
            "glyphs": self.glyphs, "config_error": self.config_error,
            "undo": [u["label"] for u in self.store.undo[-5:]],
            "title_errors": len(self.title_errors),
        }

    def handle_control(self, conn):
        try:
            conn.settimeout(2)
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data or b"{}")
            op = request.get("op")
            if op == "act":
                reply = {"ok": True, "message": self.act(request)}
            elif op == "rows":
                reply = {"ok": True, "rows": self.menu_rows(), "presets": triage.snooze_presets(time.time()),
                         "show_hidden": bool(self.store.view.get("show_hidden")),
                         "glyphs": self.glyphs}
            elif op == "status":
                reply = {"ok": True, "status": self.status()}
            elif op == "stop":
                self.running = False
                if request.get("clear"):
                    self.clear_all()
                reply = {"ok": True, "message": "stopping"}
            else:
                reply = {"ok": False, "message": "unknown op"}
        except triage.Refused as exc:
            reply = {"ok": False, "message": str(exc)}
        except Exception as exc:  # never let one bad request kill the daemon
            self.log("control error: %s" % traceback.format_exc())
            reply = {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        conn.close()
        if self.store.dirty:
            self.store.save()
            self.save_at = None

    # ---- loop --------------------------------------------------------------------------

    def plugin_enabled(self):
        result = self.request("plugin.list", {})
        if result is None:
            return True  # unknown: keep running, the reconnect logic owns disconnects
        for plugin in result.get("plugins", []):
            if plugin.get("plugin_id") == self.plugin_id:
                return plugin.get("enabled", True)
        return False

    def run(self):
        if not self.acquire_lock():
            self.lock_handle.close()
            return 0
        self.start_control()
        self.log("daemon %d starting (%s)" % (os.getpid(), titles.env_summary(self.provider)))
        try:
            self.connect()
        except OSError as exc:
            self.log("cannot reach Herdr: %s" % exc)
            self.disconnected_since = time.time()
        try:
            self.loop()
        finally:
            self.store.save()
            self.close_subs()
            self.control.close()
            try:
                os.unlink(self.control_path)
            except OSError:
                pass
            self.log("daemon %d stopped" % os.getpid())
            self.lock_handle.close()
        return 0

    def loop(self):
        while self.running:
            now = time.time()
            if self.disconnected_since is not None:
                if now - self.disconnected_since > RECONNECT_FOR:
                    self.log("Herdr is gone; exiting")
                    return
                try:
                    self.connect()
                except OSError:
                    pass
            deadlines = [self.next_tick, self.next_plugin_check]
            if self.refresh_at:
                deadlines.append(self.refresh_at)
            if self.save_at:
                deadlines.append(self.save_at)
            if self.watches.readers:
                deadlines.append(self.next_task_poll)
            snoozes = [r["snoozed_until"] for r in self.store.records.values()
                       if r.get("snoozed_until") and r.get("pane_id")]
            if snoozes:
                deadlines.append(min(snoozes) + 0.5)
            timeout = max(0.0, min(deadlines) - now)
            if self.disconnected_since is not None:
                timeout = min(timeout, 2.0)
            for key, _ in self.selector.select(timeout):
                tag = key.data
                if tag == "control":
                    try:
                        conn, _ = self.control.accept()
                    except OSError:
                        continue
                    self.handle_control(conn)
                elif tag == "wake":
                    try:
                        os.read(self.wake_r, 4096)
                    except OSError:
                        pass
                    self.take_titles()
                else:
                    sub = key.fileobj
                    events, alive = sub.read_events()
                    if events:
                        self.schedule_refresh()
                    if not alive:
                        self.log("event stream closed")
                        self.close_subs()
                        self.disconnected_since = time.time()
            now = time.time()
            if self.disconnected_since is not None:
                continue
            if self.refresh_at and now >= self.refresh_at:
                self.refresh()
            if self.watches.readers and now >= self.next_task_poll:
                self.next_task_poll = now + TASK_POLL
                if any(self.watches.poll(k, now) for k in list(self.watches.readers)):
                    self.schedule_refresh(0)
            if now >= self.next_tick:
                self.next_tick = now + TICK
                if self.snapshot is not None:
                    self.publish({p["pane_id"]: p for p in self.snapshot.get("panes", [])}, now)
            if any(r.get("snoozed_until") and r["snoozed_until"] <= now and r.get("pane_id")
                   for r in self.store.records.values()):
                self.schedule_refresh(0)
            if now >= self.next_plugin_check:
                self.next_plugin_check = now + PLUGIN_CHECK
                if not self.plugin_enabled():
                    self.log("plugin disabled or unlinked; cleaning up")
                    self.clear_all()
                    return
            if self.save_at and now >= self.save_at:
                self.store.save()
                self.save_at = None


def main():
    socket_path = transport.socket_path()
    from .cli import state_root
    state_dir = state_root()
    plugin_id = os.environ.get("HERDR_PLUGIN_ID") or "focus"
    try:
        return Daemon(socket_path, state_dir, plugin_id).run()
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return 1
