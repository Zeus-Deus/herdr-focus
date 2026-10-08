"""Background watches per agent session, from provider evidence only.

A watch is a live background task classified as monitoring work. Dev servers and other
services never light the eye, and nothing here scans processes.
"""

import re

from . import adapters


class Watches:
    def __init__(self, config):
        mon = config["monitoring"]
        self.services = [re.compile(p) for p in mon["service_patterns"]]
        self.max_age = float(mon["max_task_hours"]) * 3600
        self.readers = {}   # record key -> (path, TaskReader)

    def is_service(self, command):
        return any(p.search(command or "") for p in self.services)

    def classify(self, info):
        if info.get("tool") in ("monitor", "subagent"):
            return "monitor"
        return "service" if self.is_service(info.get("command")) else "monitor"

    def bind(self, key, agent, source):
        """Track this record's evidence; returns False when the provider records none."""
        adapter = adapters.for_agent(agent)
        if not adapter or not getattr(adapter, "SUPPORTS_TASKS", False) or not source:
            self.readers.pop(key, None)
            return False
        current = self.readers.get(key)
        if current is None or current[0] != source:
            self.readers[key] = (source, adapter.TaskReader(source))
        return True

    def reader(self, key):
        entry = self.readers.get(key)
        return entry[1] if entry else None

    def drop(self, key):
        """The agent left the pane or its session changed: its evidence is stale."""
        return self.readers.pop(key, None) is not None

    def poll(self, key, now):
        entry = self.readers.get(key)
        return entry[1].poll(now) if entry else False

    def live(self, key, now, since=None):
        """Live tasks; `since` drops evidence from before the agent process was relaunched."""
        entry = self.readers.get(key)
        if not entry:
            return {}
        return {task_id: info for task_id, info in entry[1].live(now, self.max_age).items()
                if not since or (info.get("started") or now) >= since}

    def count(self, key, now, since=None):
        return sum(1 for info in self.live(key, now, since).values()
                   if self.classify(info) == "monitor")

    def describe(self, key, now, since=None):
        return [(task_id, self.classify(info), info.get("label") or "")
                for task_id, info in self.live(key, now, since).items()]
