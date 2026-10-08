"""Codex: rollouts under ~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl.

Codex rollouts do not record classified background watches the way Claude Code does,
so Codex panes get titles from here but only the manual Watch toggle for monitoring.
"""

import glob
import json
import os
import re

SUPPORTS_TASKS = False
TaskReader = None


def sessions_root():
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "sessions")


def transcript(session):
    if not session:
        return None
    value = session.get("value") or ""
    if session.get("kind") == "path":
        return value if os.path.isfile(value) else None
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    pattern = os.path.join(glob.escape(sessions_root()), "*", "*", "*", "rollout-*-%s.jsonl" % value)
    matches = glob.glob(pattern)
    return max(matches, key=os.path.getmtime) if matches else None


def user_messages(path, limit_chars=2000, max_messages=3):
    out, total = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"role":"user"' not in line.replace(" ", ""):
                    continue
                try:
                    payload = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                for part in payload.get("content") or []:
                    text = (part.get("text") or "").strip() if isinstance(part, dict) else ""
                    # AGENTS.md, environment context and other injected blocks are not intent.
                    if not text or text.startswith(("<", "# AGENTS.md")):
                        continue
                    out.append(text[: limit_chars - total])
                    total += len(out[-1])
                if total >= limit_chars or len(out) >= max_messages:
                    break
    except OSError:
        return []
    return out
