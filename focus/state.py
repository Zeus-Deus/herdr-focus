"""Durable plugin state: one record per agent conversation, kept apart from Herdr's runtime facts.

A record holds human triage (unread, settled, snoozed, pinned), title ownership and the
manual watch flag. Runtime evidence (native status, background tasks) is never persisted
as truth; it is re-read from Herdr and the provider on every start.
"""

import copy
import hashlib
import json
import os
import tempfile
import time

VERSION = 1
PRUNE_AFTER = 14 * 86400

TRIAGE_FIELDS = ("unread", "unread_reason", "settled", "snoozed_until", "pinned",
                 "keep_active", "woke_at", "manual_watch", "ack_seq")


def new_record(key):
    return {
        "key": key,
        "unread": False,
        "unread_reason": None,      # "manual" | "woke"
        "settled": False,
        "settled_at": None,
        "snoozed_until": None,
        "pinned": False,
        "keep_active": False,       # user un-settled: skip auto-settle until new activity
        "woke_at": None,
        "ack_seq": None,            # Herdr state_change_seq of a "done" result the user marked read
        "manual_watch": False,
        "title": None,
        "title_owner": None,        # "manual" | "generated" | "fallback"
        "title_input": None,        # hash of the context the generated title came from
        "title_generation": 0,
        "title_requested": False,   # user asked for a model title: it beats the agent's own      # bumped on every ownership change; late results compare it
        "status": None,
        "status_since": None,
        "last_live": None,
        "evidence_since": None,     # ignore background-task evidence older than a relaunch
        "pane_id": None,
        "terminal_id": None,
        "workspace_id": None,
        "agent": None,
    }


def scope_id(socket_path):
    """Separate state per Herdr server so identical pane IDs on two servers never merge."""
    return hashlib.sha1(os.path.realpath(socket_path).encode()).hexdigest()[:12]


def record_key(pane):
    """Durable identity: the native agent session when known, else the terminal.

    Terminal IDs survive pane moves; pane IDs do not and are reused, so they are never keys.
    """
    session = pane.get("agent_session") or {}
    if session.get("value"):
        return "session:%s:%s" % (session.get("agent") or pane.get("agent") or "", session["value"])
    return "terminal:%s" % pane.get("terminal_id", pane.get("pane_id"))


class Store:
    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, "state.json")
        self.records = {}
        self.undo = []
        self.view = {"collapse_settled": False}
        self.title_cache = {}
        self.dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if data.get("version") != VERSION:
            return
        for key, record in data.get("records", {}).items():
            merged = new_record(key)
            merged.update(record)
            self.records[key] = merged
        self.undo = data.get("undo", [])
        self.view.update(data.get("view", {}))
        self.title_cache = data.get("title_cache", {})

    def save(self):
        if not self.dirty:
            return
        os.makedirs(self.directory, exist_ok=True)
        data = {"version": VERSION, "records": self.records, "undo": self.undo,
                "view": self.view, "title_cache": self.title_cache}
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".state.")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False

    def get(self, key, create=True):
        record = self.records.get(key)
        if record is None and create:
            record = self.records[key] = new_record(key)
            self.dirty = True
        return record

    def rekey(self, old, new):
        """An agent reported its session after we first saw its terminal: keep the triage."""
        if old == new or old not in self.records:
            return
        record = self.records.pop(old)
        if new in self.records:
            # The session already had a record (e.g. after a restart): it wins, but keep
            # anything the user did to the terminal-keyed record meanwhile.
            target = self.records[new]
            for field in TRIAGE_FIELDS:
                if record.get(field) and not target.get(field):
                    target[field] = record[field]
            if record.get("title_owner") == "manual":
                target["title"] = record["title"]
                target["title_owner"] = "manual"
        else:
            record["key"] = new
            self.records[new] = record
        self.dirty = True

    def push_undo(self, label, before):
        """before: {key: record snapshot} taken prior to a triage change."""
        self.undo.append({"label": label, "at": time.time(), "before": before})
        del self.undo[:-20]
        self.dirty = True

    def pop_undo(self):
        if not self.undo:
            return None
        entry = self.undo.pop()
        for key, snapshot in entry["before"].items():
            record = self.get(key)
            for field in TRIAGE_FIELDS + ("settled_at",):
                record[field] = snapshot.get(field)
        self.dirty = True
        return entry["label"]

    def snapshot(self, keys):
        return {key: copy.deepcopy(self.records[key]) for key in keys if key in self.records}

    def prune(self, live_keys, now=None):
        now = now or time.time()
        for key in list(self.records):
            record = self.records[key]
            if key in live_keys:
                continue
            last = record.get("last_live") or 0
            if now - last > PRUNE_AFTER and not record.get("pinned"):
                del self.records[key]
                self.dirty = True
