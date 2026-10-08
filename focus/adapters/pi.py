"""Pi: JSONL sessions under ~/.pi/agent/sessions/<cwd slug>/<ts>_<uuid>.jsonl.

Herdr's Pi integration reports the session file path (or its id). Pi has no background
shell ("use tmux"), so only titles come from here: a `session_info` name when the user or an
extension named the session, else the first prompt.
"""

import glob
import os
import re

from .common import first_texts, jsonl, text_of

SUPPORTS_TASKS = False
TaskReader = None


def sessions_root():
    base = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(os.path.expanduser("~"), ".pi", "agent")
    return os.path.join(base, "sessions")


def locate(session, pane=None):
    if not session:
        return None
    value = session.get("value") or ""
    if session.get("kind") == "path" or value.startswith("/"):
        return value if os.path.isfile(value) else None
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    matches = glob.glob(os.path.join(glob.escape(sessions_root()), "*", "*_%s.jsonl" % value))
    return matches[0] if matches else None


def title(source):
    name = None
    for record in jsonl(source):
        if record.get("type") == "session_info" and record.get("name"):
            name = record["name"]
    return name


def user_messages(source, limit_chars=2000, max_messages=3):
    texts = (text_of((r.get("message") or {}).get("content")) for r in jsonl(source)
             if r.get("type") == "message" and (r.get("message") or {}).get("role") == "user")
    return first_texts(texts, limit_chars, max_messages)
