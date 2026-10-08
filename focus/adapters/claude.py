"""Claude Code: transcripts under ~/.claude/projects/<slug>/<session>.jsonl.

Titles: Claude writes its own `ai-title` records; `/rename` writes `custom-title`.
Background-task evidence is structured, so watches are detected, not guessed:
  start   assistant tool_use Bash{run_in_background:true} or Monitor, then the matching
          tool_result's toolUseResult.backgroundTaskId / taskId
  end     a user record carrying <task-notification> with a terminal <status>,
          or a TaskStop tool_result naming the task
"""

import glob
import os
import re

from .common import Tail, first_texts, jsonl, text_of, timestamp

SUPPORTS_TASKS = True
NATIVE_TITLES = True  # the agent names its own sessions

_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)
TERMINAL = {"completed", "failed", "killed", "stopped", "cancelled", "canceled", "error"}


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def locate(session, pane=None):
    if not session:
        return None
    value = session.get("value") or ""
    if session.get("kind") == "path":
        return value if os.path.isfile(value) else None
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    matches = glob.glob(os.path.join(glob.escape(projects_root()), "*", value + ".jsonl"))
    return matches[0] if matches else None


def _intent(record):
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    if (record.get("origin") or {}).get("kind"):
        return None  # task notifications and other synthetic turns
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") not in ("text",) for c in content):
        return None  # tool results and images are not user intent
    text = text_of(content).strip()
    if text.startswith(("<command-", "<local-command", "<task-notification",
                        "<system-reminder", "Caveat:", "[Request interrupted")):
        return None
    return text


def user_messages(source, limit_chars=2000, max_messages=3):
    texts = (_intent(r) for r in jsonl(source) if r.get("type") == "user")
    return first_texts(texts, limit_chars, max_messages)


def title(source):
    """Read by TaskReader as it tails; this full scan is for one-off use."""
    reader = TaskReader(source)
    reader.poll(0)
    return reader.title


class TaskReader:
    """Tails one transcript: live background tasks plus Claude's own title."""

    def __init__(self, source):
        self.tail = Tail(source)
        self.pending = {}   # tool_use_id -> task info awaiting its id
        self.tasks = {}     # task id -> {"tool", "command", "label", "started", "timeout"}
        self.ai_title = None
        self.custom_title = None

    @property
    def title(self):
        return self.custom_title or self.ai_title

    def poll(self, now):
        """Read new records. Returns True when the live task set or the title changed."""
        before = (set(self.tasks), self.title)
        backlog = self.tail.inode is None
        records, reset, mtime = self.tail.read()
        if reset:
            self.pending.clear()
            self.tasks.clear()
            backlog = True
        # Backlog lines without a timestamp are at least as old as the file's last write.
        fallback = mtime if (backlog and mtime) else now
        for record in records:
            self._apply(record, fallback)
        return (set(self.tasks), self.title) != before

    def _apply(self, record, now):
        kind = record.get("type")
        if kind == "ai-title" and record.get("aiTitle"):
            self.ai_title = record["aiTitle"]
            return
        if kind == "custom-title" and record.get("customTitle"):
            self.custom_title = record["customTitle"]
            return
        message = record.get("message") or {}
        content = message.get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, args = block.get("name"), block.get("input") or {}
                if name == "Bash" and args.get("run_in_background"):
                    self.pending[block.get("id")] = {
                        "tool": "bash", "command": args.get("command", ""),
                        "label": args.get("description") or args.get("command", "")[:60]}
                elif name == "Monitor":
                    self.pending[block.get("id")] = {
                        "tool": "monitor", "command": args.get("command", ""),
                        "label": args.get("description") or "monitor",
                        "timeout": (args.get("timeout_ms") or 0) / 1000.0}
                elif name in ("TaskStop", "KillShell"):
                    task_id = args.get("task_id") or args.get("shell_id")
                    if task_id:
                        self.pending[block.get("id")] = {"stop": task_id}
            return
        if kind != "user":
            return
        if isinstance(content, str) and "<task-notification>" in content:
            self._notification(content)
            return
        result = record.get("toolUseResult")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            info = self.pending.pop(block.get("tool_use_id"), None)
            if not info:
                continue
            if "stop" in info:
                self.tasks.pop(info["stop"], None)
                continue
            task_id = None
            if isinstance(result, dict):
                task_id = result.get("backgroundTaskId") or result.get("taskId")
            if not task_id:
                match = re.search(r"\b(?:ID:|task)\s+([a-z0-9]{6,})", text_of(block.get("content")))
                task_id = match.group(1) if match else None
            if task_id and not block.get("is_error"):
                info["started"] = timestamp(record) or now
                self.tasks[task_id] = info

    def _notification(self, text):
        for chunk in text.split("<task-notification>")[1:]:
            task = _TASK_ID.search(chunk)
            if not task:
                continue
            status = _STATUS.search(chunk)
            summary = _SUMMARY.search(chunk)
            ended = status and status.group(1).strip().lower() in TERMINAL
            if summary and "stream ended" in summary.group(1):
                ended = True
            if ended:
                self.tasks.pop(task.group(1).strip(), None)

    def live(self, now, max_age):
        """Live tasks, dropping evidence that outlived its own timeout or max_age."""
        out = {}
        for task_id, info in self.tasks.items():
            started = info.get("started") or now
            timeout = info.get("timeout") or 0
            if timeout and now - started > timeout + 60:
                continue
            if max_age and now - started > max_age:
                continue
            out[task_id] = info
        return out
