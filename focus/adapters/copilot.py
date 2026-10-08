"""GitHub Copilot CLI: ~/.copilot/session-state/<id>/workspace.yaml (+ events.jsonl).

Herdr's integration reports the session id. The workspace file's `summary` is Copilot's own
title when it wrote one; events.jsonl (when present) holds `user.message` events.
"""

import os
import re

from .common import first_texts, jsonl

SUPPORTS_TASKS = False
TaskReader = None


def locate(session, pane=None):
    value = (session or {}).get("value") or ""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    path = os.path.join(os.path.expanduser("~"), ".copilot", "session-state", value)
    return path if os.path.isdir(path) else None


def title(source):
    try:
        with open(os.path.join(source, "workspace.yaml"), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r"^summary:\s*(.+?)\s*$", line)
                if match:
                    value = match.group(1).strip("'\"")
                    return value or None
    except OSError:
        pass
    return None


def user_messages(source, limit_chars=2000, max_messages=3):
    texts = ((r.get("data") or {}).get("content") for r in jsonl(os.path.join(source, "events.jsonl"))
             if r.get("type") == "user.message")
    return first_texts(texts, limit_chars, max_messages)
