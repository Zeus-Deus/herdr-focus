"""Codex CLI: rollouts under ~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread id>.jsonl.

Titles: Codex names threads itself (and on /rename); the name is in state_*.sqlite
`threads.name`, with ~/.codex/session_index.jsonl as a fallback.
Background processes (unified exec) outlive turns and are recorded:
  start   a function_call_output "Process running with session ID N" (or, in code mode,
          a custom_tool_call_output carrying "session_id": N)
  end     event_msg item_completed {item: {type: CommandExecution, process_id: N}}, written
          when the process really exits; legacy rollouts only show it via a later
          write_stdin output with "Process exited with code".
"""

import glob
import json
import os
import re

from .common import Tail, first_texts, jsonl, query, text_of, timestamp

SUPPORTS_TASKS = True
NATIVE_TITLES = True  # the agent names its own sessions

_RUNNING = re.compile(r"Process running with session ID (\d+)")
_CODE_SESSION = re.compile(r'\\?"session_id\\?"\s*:\s*(\d+)')
_EXITED = re.compile(r'Process exited with code|\\?"exit_code\\?"\s*:')


def home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def locate(session, pane=None):
    if not session:
        return None
    value = session.get("value") or ""
    if session.get("kind") == "path":
        return value if os.path.isfile(value) else None
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    rows = query(_state_db(), "SELECT rollout_path FROM threads WHERE id = ?", (value,))
    if rows and rows[0][0] and os.path.isfile(rows[0][0]):
        return rows[0][0]
    pattern = os.path.join(glob.escape(home()), "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % value)
    matches = glob.glob(pattern)
    return max(matches, key=os.path.getmtime) if matches else None


def _state_db():
    found = sorted(glob.glob(os.path.join(glob.escape(home()), "state_*.sqlite")))
    return found[-1] if found else None


def _thread_id(source):
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", source or "")
    return match.group(1) if match else None


def title(source):
    thread = _thread_id(source)
    if not thread:
        return None
    rows = query(_state_db(), "SELECT name FROM threads WHERE id = ?", (thread,))
    if rows and rows[0][0]:
        return rows[0][0]
    name = None
    for record in jsonl(os.path.join(home(), "session_index.jsonl")):
        if record.get("id") == thread and record.get("thread_name"):
            name = record["thread_name"]
    return name


def _typed(text):
    text = (text or "").strip()
    # Injected blocks, replies to questions and slash commands are not intent.
    return None if not text or text.startswith(("<", "/", "# AGENTS.md")) else text


def user_messages(source, limit_chars=2000, max_messages=3):
    typed, legacy, items = [], [], []
    for record in jsonl(source):
        payload = record.get("payload") or {}
        if record.get("type") == "response_item" and payload.get("type") == "message" \
                and payload.get("role") == "user":
            # Oldest format: typed prompts mixed with injected blocks, filtered by _typed.
            items.extend(_typed(text_of([c])) for c in payload.get("content") or [])
            continue
        if record.get("type") != "event_msg":
            continue
        if payload.get("type") == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") == "UserMessage":
                typed.append(_typed(text_of(item.get("content"))))
        elif payload.get("type") == "user_message":
            legacy.append(_typed(payload.get("message")))
        if len([t for t in typed + legacy if t]) >= max_messages:
            break
    return first_texts(typed or legacy or items, limit_chars, max_messages)


def _command(call):
    name, raw = call.get("name"), call.get("arguments") or call.get("input") or ""
    if name == "exec_command":
        try:
            return json.loads(raw).get("cmd") or ""
        except (ValueError, AttributeError):
            return ""
    match = re.search(r"cmd\s*:\s*[\"'`](.+?)[\"'`]", raw)
    return match.group(1) if match else raw[:80]


def _flat(output):
    """Tool output as text, whatever shape this Codex version used."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in output)
    return json.dumps(output) if output is not None else ""


class TaskReader:
    def __init__(self, source):
        self.tail = Tail(source)
        self.calls = {}     # call_id -> {"name", "command", "session_id"}
        self.tasks = {}     # process id -> {"tool", "command", "label", "started"}

    def poll(self, now):
        before = set(self.tasks)
        records, reset, mtime = self.tail.read()
        if reset:
            self.calls.clear()
            self.tasks.clear()
        for record in records:
            self._apply(record, timestamp(record) or now)
        return set(self.tasks) != before

    def _apply(self, record, when):
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if record.get("type") == "response_item":
            if kind in ("function_call", "custom_tool_call"):
                args = payload.get("arguments") or ""
                session_id = None
                if payload.get("name") == "write_stdin":
                    try:
                        session_id = str(json.loads(args).get("session_id"))
                    except (ValueError, AttributeError):
                        pass
                self.calls[payload.get("call_id")] = {
                    "name": payload.get("name"), "command": _command(payload), "session_id": session_id}
            elif kind in ("function_call_output", "custom_tool_call_output"):
                call = self.calls.pop(payload.get("call_id"), None) or {}
                text = _flat(payload.get("output"))
                if call.get("name") == "write_stdin":
                    if call.get("session_id") and _EXITED.search(text or ""):
                        self.tasks.pop(call["session_id"], None)
                    return
                match = _RUNNING.search(text or "")
                if not match and kind == "custom_tool_call_output" and not _EXITED.search(text or ""):
                    match = _CODE_SESSION.search(text or "")
                if match:
                    command = call.get("command") or ""
                    self.tasks[match.group(1)] = {"tool": "bash", "command": command,
                                                  "label": command[:60], "started": when}
        elif record.get("type") == "event_msg" and kind == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") == "CommandExecution" and item.get("process_id") is not None:
                self.tasks.pop(str(item["process_id"]), None)

    def live(self, now, max_age):
        return {k: v for k, v in self.tasks.items()
                if not max_age or now - (v.get("started") or now) <= max_age}
