"""Claude Code: transcripts under ~/.claude/projects/<slug>/<session>.jsonl.

Background-task evidence is structured, so watches are detected, not guessed:
  start   assistant tool_use Bash{run_in_background:true} or Monitor, then the matching
          tool_result's toolUseResult.backgroundTaskId / taskId
  end     a user record carrying <task-notification> with a terminal <status>,
          or a TaskStop tool_result naming the task
"""

import glob
import json
import os
import re

SUPPORTS_TASKS = True

_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)
TERMINAL = {"completed", "failed", "killed", "stopped", "cancelled", "canceled", "error"}


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def transcript(session):
    if not session:
        return None
    value = session.get("value") or ""
    if session.get("kind") == "path":
        return value if os.path.isfile(value) else None
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value):
        return None
    matches = glob.glob(os.path.join(glob.escape(projects_root()), "*", value + ".jsonl"))
    return matches[0] if matches else None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        if len(parts) == len(content):
            return "\n".join(parts)
    return None  # tool results and images are not user intent


def _is_intent(record):
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    if (record.get("origin") or {}).get("kind"):
        return None  # task notifications and other synthetic turns
    text = _text((record.get("message") or {}).get("content"))
    if not text:
        return None
    stripped = text.lstrip()
    if stripped.startswith(("<command-", "<local-command", "<task-notification",
                            "<system-reminder", "Caveat:", "[Request interrupted")):
        return None
    return text.strip()


def user_messages(path, limit_chars=2000, max_messages=3):
    out, total = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"user"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                text = _is_intent(record)
                if text:
                    out.append(text[: limit_chars - total])
                    total += len(out[-1])
                    if total >= limit_chars or len(out) >= max_messages:
                        break
    except OSError:
        return []
    return out


class TaskReader:
    """Tails one transcript incrementally and keeps the set of live background tasks."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.inode = None
        self.pending = {}   # tool_use_id -> task info awaiting its id
        self.tasks = {}     # task id -> {"kind", "label", "started", "timeout"}
        self._partial = b""

    def poll(self, now):
        """Read new records. Returns True when the live task set changed."""
        try:
            stat = os.stat(self.path)
        except OSError:
            changed = bool(self.tasks)
            self.tasks.clear()
            return changed
        if self.inode != stat.st_ino or stat.st_size < self.offset:
            self.inode, self.offset, self._partial = stat.st_ino, 0, b""
            self.pending.clear()
            self.tasks.clear()
        if stat.st_size == self.offset:
            return False
        before = set(self.tasks)
        # Backlog lines without a timestamp are at least as old as the file's last write.
        fallback = stat.st_mtime if self.offset == 0 else now
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = self._partial + handle.read()
            self.offset = handle.tell()
        lines = data.split(b"\n")
        self._partial = lines.pop()
        for raw in lines:
            if raw.strip():
                try:
                    self._apply(json.loads(raw), fallback)
                except ValueError:
                    continue
        return set(self.tasks) != before

    def _apply(self, record, now):
        kind = record.get("type")
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
                match = re.search(r"\b(?:ID:|task)\s+([a-z0-9]{6,})", _flat(block.get("content")))
                task_id = match.group(1) if match else None
            if task_id and not block.get("is_error"):
                info["started"] = _timestamp(record) or now
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


def _flat(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _timestamp(record):
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
