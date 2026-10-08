"""OpenCode: one SQLite store, ~/.local/share/opencode/opencode.db (read-only, WAL).

Herdr's OpenCode integration reports the root session id (ses_...).
Titles: OpenCode names sessions itself (`session.title`); "New session - <date>" is its
placeholder until then.
Background work: OpenCode's bash tool is synchronous, but `task` can run a subagent in the
background as a child session. A child whose latest assistant message has not completed
is live background work.
Only the session, message and part tables are ever read.
"""

import os
import re

from .common import data_home, first_texts, query

SUPPORTS_TASKS = True
NATIVE_TITLES = True  # the agent names its own sessions

_PLACEHOLDER = re.compile(r"^(New session|Child session) - ")


def db_path():
    return os.path.join(data_home(), "opencode", "opencode.db")


def locate(session, pane=None):
    value = (session or {}).get("value") or ""
    return value if re.fullmatch(r"ses_[A-Za-z0-9]{10,60}", value) else None


def title(source):
    rows = query(db_path(), "SELECT title FROM session WHERE id = ?", (source,))
    if rows and rows[0][0] and not _PLACEHOLDER.match(rows[0][0]):
        return rows[0][0]
    return None


def user_messages(source, limit_chars=2000, max_messages=3):
    rows = query(db_path(), """
        SELECT json_extract(p.data, '$.text') FROM message m
        JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ? AND json_extract(m.data, '$.role') = 'user'
          AND json_extract(p.data, '$.type') = 'text'
          AND coalesce(json_extract(p.data, '$.synthetic'), 0) = 0
        ORDER BY m.time_created, p.id LIMIT 12""", (source,))
    return first_texts((r[0] for r in rows), limit_chars, max_messages)


class TaskReader:
    def __init__(self, source):
        self.session = source
        self.tasks = {}

    def poll(self, now):
        rows = query(db_path(), """
            SELECT s.id, s.title, s.time_created,
              (SELECT json_extract(m.data, '$.role') || ':' ||
                      coalesce(json_extract(m.data, '$.time.completed'), '')
                 FROM message m WHERE m.session_id = s.id
                 ORDER BY m.time_created DESC LIMIT 1)
            FROM session s WHERE s.parent_id = ?""", (self.session,))
        tasks = {}
        for child, label, created, last in rows:
            if last and (last == "user:" or last == "assistant:"):
                tasks[child] = {"tool": "subagent", "command": "", "label": label or "subagent",
                                "started": (created or 0) / 1000.0}
        changed = set(tasks) != set(self.tasks)
        self.tasks = tasks
        return changed

    def live(self, now, max_age):
        return {k: v for k, v in self.tasks.items()
                if not max_age or now - (v.get("started") or now) <= max_age}
