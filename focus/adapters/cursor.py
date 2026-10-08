"""Cursor CLI (cursor-agent): ~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl.

Herdr's integration reports the conversation id. Transcripts carry no title and no
background-task records, so only the first prompt comes from here.
"""

import glob
import os
import re

from .common import first_texts, jsonl, text_of

SUPPORTS_TASKS = False
TaskReader = None


def locate(session, pane=None):
    value = (session or {}).get("value") or ""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    root = os.path.join(os.path.expanduser("~"), ".cursor", "projects")
    matches = glob.glob(os.path.join(glob.escape(root), "*", "agent-transcripts", value, value + ".jsonl"))
    return matches[0] if matches else None


def title(source):
    return None


def user_messages(source, limit_chars=2000, max_messages=3):
    texts = (text_of((r.get("message") or {}).get("content")) for r in jsonl(source)
             if r.get("role") == "user")
    texts = (t for t in texts if not t.lstrip().startswith("<"))
    return first_texts(texts, limit_chars, max_messages)
