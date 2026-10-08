"""Hermes Agent: <home>/state.db (SQLite, WAL) and <home>/processes.json.

<home> is ~/.hermes or a profile, ~/.hermes/profiles/<name> (`hermes -p <name>`).
Session: Herdr's Hermes integration reports it (herdr:hermes) once the plugin is installed
in the profile you run. Without it, the session is found through Hermes' own runtime files:
the live entry in runtime/active_sessions.json whose process runs in this pane (its
HERDR_PANE_ID), then, for the classic CLI, the per-terminal breadcrumb that follows /new.
Titles: Hermes names sessions itself (`sessions.title`).
Background: terminal(background=true) processes are listed in processes.json while they
run, keyed to the owning session; an entry counts only while its pid is alive with the
recorded start time.
"""

import glob
import json
import os
import re
import time

from .common import first_texts, query

SUPPORTS_TASKS = True
NATIVE_TITLES = True  # the agent names its own sessions
_SESSION = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{4,12}$")


def homes():
    root = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    found = [root] + sorted(glob.glob(os.path.join(glob.escape(root), "profiles", "*")))
    return [h for h in found if os.path.exists(os.path.join(h, "state.db"))]


def _read_json(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _environ(pid):
    try:
        with open("/proc/%s/environ" % pid, "rb") as handle:
            pairs = handle.read().split(b"\0")
    except OSError:
        return {}
    return dict(p.decode(errors="replace").split("=", 1) for p in pairs if b"=" in p)


def _alive(pid, start_ticks=None):
    try:
        with open("/proc/%s/stat" % pid) as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    if fields[0] in ("Z", "X"):
        return False  # exited, just not reaped yet
    return start_ticks is None or str(fields[19]) == str(start_ticks)


def _breadcrumb(home, pid):
    try:
        tty = os.readlink("/proc/%s/fd/0" % pid)
    except OSError:
        return None
    name = "tty-" + re.sub(r"[^A-Za-z0-9]+", "-", tty).strip("-")
    crumb = _read_json(os.path.join(home, "terminal-sessions", name)) or {}
    return crumb.get("session_id")


def _from_runtime(pane_id):
    """Session of the live Hermes process whose environment names this pane."""
    for home in homes():
        data = _read_json(os.path.join(home, "runtime", "active_sessions.json")) or {}
        for entry in data.get("entries") or []:
            pid = entry.get("pid")
            if not pid or not _alive(pid) or _environ(pid).get("HERDR_PANE_ID") != pane_id:
                continue
            session = None
            if entry.get("surface") == "cli":
                session = _breadcrumb(home, pid)  # the CLI never updates its lease on /new
            session = session or (entry.get("metadata") or {}).get("live_session_id") or entry.get("session_id")
            if session:
                return home, session
    return None


def locate(session, pane=None):
    value = (session or {}).get("value") or ""
    if _SESSION.match(value):
        for home in homes():
            if query(os.path.join(home, "state.db"), "SELECT 1 FROM sessions WHERE id = ?", (value,)):
                return "%s|%s" % (home, value)
    if pane and pane.get("pane_id"):
        found = _from_runtime(pane["pane_id"])
        if found:
            return "%s|%s" % found
    return None


def _split(source):
    home, _, session = (source or "").rpartition("|")
    return home, session


def title(source):
    home, session = _split(source)
    rows = query(os.path.join(home, "state.db"), "SELECT title FROM sessions WHERE id = ?", (session,))
    return rows[0][0] if rows and rows[0][0] else None


def user_messages(source, limit_chars=2000, max_messages=3):
    home, session = _split(source)
    rows = query(os.path.join(home, "state.db"), """
        SELECT content FROM messages WHERE session_id = ? AND role = 'user'
          AND display_kind IS NULL AND coalesce(_compressed_summary, 0) = 0
        ORDER BY id LIMIT 6""", (session,))
    texts = (r[0] for r in rows if r[0] and not r[0].lstrip().startswith("<"))
    return first_texts(texts, limit_chars, max_messages)


class TaskReader:
    def __init__(self, source):
        self.home, self.session = _split(source)
        self.tasks = {}

    def poll(self, now):
        entries = _read_json(os.path.join(self.home, "processes.json")) or []
        tasks = {}
        for entry in entries if isinstance(entries, list) else []:
            if entry.get("session_key") != self.session:
                continue
            if not _alive(entry.get("pid"), entry.get("host_start_time")):
                continue
            command = entry.get("command") or ""
            tasks[entry.get("session_id") or str(entry.get("pid"))] = {
                "tool": "monitor" if (entry.get("watch_patterns") or entry.get("notify_on_complete")) else "bash",
                "command": command, "label": command[:60],
                "started": entry.get("started_at") or time.time()}
        changed = set(tasks) != set(self.tasks)
        self.tasks = tasks
        return changed

    def live(self, now, max_age):
        return {k: v for k, v in self.tasks.items()
                if not max_age or now - (v.get("started") or now) <= max_age}
