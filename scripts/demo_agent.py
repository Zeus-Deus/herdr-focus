#!/usr/bin/env python3
"""A stand-in coding agent for demos and end-to-end tests. Not a real agent or model.

It runs under a process named after the agent (claude, codex, opencode, hermes) so Herdr
detects it the normal way, and it shows the screen/title signals Herdr's detection rules
read for that agent (working, blocked, idle). It stores its conversation in that agent's
own format and place: Claude/Codex JSONL transcripts, OpenCode's SQLite store, Hermes'
state.db and processes.json. Session ids reach Herdr the way its integrations send them;
for Hermes the default is to send none, like a profile without the Herdr plugin.

    demo_agent.py --agent codex --prompt "review the webhook retries" \
        --title "Webhook retry review" --steps working:4 task:b1:gh-run-watch idle
Steps: working[:secs] | blocked[:secs] | idle[:secs] | sleep:secs |
       task:ID:CMD     background shell command (Claude Bash, Codex exec, Hermes process)
       monitor:ID:CMD  Claude Monitor tool
       subagent:ID:LABEL  OpenCode background subagent session
       end:ID          that task finishes
"""

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import uuid

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
SPINNER = "⠋"


def as_agent_process(agent):
    """Re-exec under a file named like the agent so Herdr's process detection sees it."""
    if os.path.basename(sys.argv[0]) == agent:
        return
    bindir = os.path.expanduser("~/.cache/focus-demo/bin")
    os.makedirs(bindir, exist_ok=True)
    link = os.path.join(bindir, agent)
    if not os.path.islink(link):
        os.symlink(os.path.abspath(__file__), link)
    os.execv(link, [link] + sys.argv[1:])


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def append(path, record):
    record.setdefault("timestamp", stamp())
    with open(path, "a") as handle:
        handle.write(json.dumps(record) + "\n")


class Claude:
    source, osc_idle = "herdr:claude", "✳ {title}"

    def __init__(self, prompt):
        self.id = str(uuid.uuid4())
        folder = os.path.expanduser("~/.claude/projects/-focus-demo")
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, self.id + ".jsonl")
        append(self.path, {"type": "user", "message": {"role": "user", "content": prompt}})

    def named(self, title):
        append(self.path, {"type": "ai-title", "aiTitle": title, "sessionId": self.id})

    def task(self, task_id, command, monitor=False):
        tool_id = "toolu_" + uuid.uuid4().hex[:12]
        if monitor:
            use = {"name": "Monitor", "input": {"command": command, "description": command,
                                                "timeout_ms": 3600000}}
            result = {"taskId": task_id}
        else:
            use = {"name": "Bash", "input": {"command": command, "description": command,
                                             "run_in_background": True}}
            result = {"backgroundTaskId": task_id}
        use.update(type="tool_use", id=tool_id)
        append(self.path, {"type": "assistant", "message": {"role": "assistant", "content": [use]}})
        append(self.path, {"type": "user", "toolUseResult": result, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": "Command running in background with ID: %s." % task_id}]}})

    def end(self, task_id):
        append(self.path, {"type": "user", "origin": {"kind": "task-notification"}, "message": {
            "role": "user", "content": "<task-notification>\n<task-id>%s</task-id>\n"
            "<status>completed</status>\n<summary>done</summary>\n</task-notification>" % task_id}})


class Codex:
    source, osc_idle = "herdr:codex", "{title}"

    def __init__(self, prompt):
        self.id = str(uuid.uuid4())
        self.home = os.path.expanduser("~/.codex")
        folder = os.path.join(self.home, "sessions", time.strftime("%Y/%m/%d"))
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S"), self.id))
        self.processes = {}
        append(self.path, {"type": "session_meta", "payload": {"id": self.id, "history_mode": "paginated"}})
        append(self.path, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>demo</environment_context>"}]}})
        append(self.path, {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": prompt}]}}})

    def named(self, title):
        append(os.path.join(self.home, "session_index.jsonl"),
               {"id": self.id, "thread_name": title, "updated_at": stamp()})

    def task(self, task_id, command, monitor=False):
        call = "call_" + uuid.uuid4().hex[:10]
        process = str(random.randint(1000, 99999))
        self.processes[task_id] = process
        append(self.path, {"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": call,
            "arguments": json.dumps({"cmd": command, "yield_time_ms": 1000})}})
        append(self.path, {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": call,
            "output": "Chunk ID: 1a2b\nWall time: 1.0 seconds\nProcess running with session ID %s\nOutput:\n" % process}})

    def end(self, task_id):
        append(self.path, {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "process_id": self.processes.get(task_id, task_id),
            "status": "completed", "exit_code": 0}}})


class OpenCode:
    source, osc_idle = "herdr:opencode", "OC | {title}"
    # Herdr's OpenCode plugin owns the whole lifecycle: it reports state and session together.
    full_lifecycle = True
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT NOT NULL,
            directory TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """

    def __init__(self, prompt):
        folder = os.path.expanduser("~/.local/share/opencode")
        os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(folder, "opencode.db"), timeout=5)
        self.db.executescript(self.SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.id = "ses_" + uuid.uuid4().hex[:24]
        now = int(time.time() * 1000)
        self.run("INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
                 self.id, "New session - " + stamp(), os.getcwd(), now, now)
        message = "msg_" + uuid.uuid4().hex[:20]
        self.run("INSERT INTO message VALUES (?, ?, ?, ?, ?)", message, self.id, now, now,
                 json.dumps({"role": "user", "time": {"created": now}}))
        self.run("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", "prt_" + uuid.uuid4().hex[:20], message,
                 self.id, now, now, json.dumps({"type": "text", "text": prompt}))
        self.children = {}

    def run(self, sql, *args):
        self.db.execute(sql, args)
        self.db.commit()

    def named(self, title):
        self.run("UPDATE session SET title = ? WHERE id = ?", title, self.id)

    def task(self, task_id, label, monitor=False):
        child = "ses_" + uuid.uuid4().hex[:24]
        message = "msg_" + uuid.uuid4().hex[:20]
        now = int(time.time() * 1000)
        self.children[task_id] = message
        self.run("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)", child, self.id, label, os.getcwd(), now, now)
        self.run("INSERT INTO message VALUES (?, ?, ?, ?, ?)", message, child, now, now,
                 json.dumps({"role": "assistant", "time": {"created": now}}))

    def end(self, task_id):
        now = int(time.time() * 1000)
        self.run("UPDATE message SET data = ? WHERE id = ?",
                 json.dumps({"role": "assistant", "time": {"created": now, "completed": now}}),
                 self.children.get(task_id))


class Hermes:
    source, osc_idle = "herdr:hermes", "{title}"
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, title TEXT,
            title_source TEXT, started_at REAL);
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, display_kind TEXT, _compressed_summary INTEGER DEFAULT 0, timestamp REAL);
    """

    def __init__(self, prompt):
        self.home = os.path.expanduser("~/.hermes")
        os.makedirs(os.path.join(self.home, "runtime"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(self.home, "state.db"), timeout=5)
        self.db.executescript(self.SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        self.db.execute("INSERT INTO sessions VALUES (?, 'tui', ?, NULL, NULL, ?)", (self.id, os.getcwd(), time.time()))
        self.db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                        (self.id, prompt, time.time()))
        self.db.commit()
        # Hermes' own record of which process runs which session.
        self.update_json("runtime/active_sessions.json", lambda data: dict(data or {}, entries=(
            (data or {}).get("entries", []) + [{"session_id": self.id, "surface": "tui", "pid": os.getpid(),
                                                "started_at": time.time()}])))
        self.procs = {}

    def update_json(self, rel, change):
        path = os.path.join(self.home, rel)
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = None
        with open(path + ".tmp", "w") as handle:
            json.dump(change(data), handle)
        os.replace(path + ".tmp", path)

    def named(self, title):
        self.db.execute("UPDATE sessions SET title = ?, title_source = 'llm' WHERE id = ?", (title, self.id))
        self.db.commit()

    def task(self, task_id, command, monitor=False):
        proc = subprocess.Popen(["sleep", "3600"], start_new_session=True)
        self.procs[task_id] = proc
        with open("/proc/%d/stat" % proc.pid) as handle:
            ticks = int(handle.read().rsplit(")", 1)[1].split()[19])
        entry = {"session_id": "proc_" + task_id, "command": command, "pid": proc.pid, "host_start_time": ticks,
                 "session_key": self.id, "started_at": time.time(), "notify_on_complete": True}
        self.update_json("processes.json", lambda data: (data or []) + [entry])

    def end(self, task_id):
        proc = self.procs.pop(task_id, None)
        if proc:
            proc.kill()
            proc.wait()
        self.update_json("processes.json", lambda data: [e for e in data or []
                                                         if e.get("session_id") != "proc_" + task_id])


AGENTS = {"claude": Claude, "codex": Codex, "opencode": OpenCode, "hermes": Hermes}


class Screen:
    def __init__(self, agent, prompt, title):
        self.agent, self.prompt, self.title = agent, prompt, title
        self.notes = []
        self.color = {"claude": "\033[38;2;240;160;112m", "codex": "\033[38;2;130;170;255m",
                      "opencode": "\033[38;2;250;178;131m", "hermes": "\033[38;2;180;150;255m"}[agent]

    def draw(self, state, named):
        title = self.title if named else self.agent.capitalize()
        if state == "working":
            osc = {"hermes": "⏳ %s", "opencode": "OC | %s"}.get(self.agent, SPINNER + " %s") % title
        elif state == "blocked" and self.agent == "hermes":
            osc = "⚠ %s" % title
        else:
            osc = AGENTS[self.agent].osc_idle.format(title=title)
        out = ["\033]0;%s\007\033[2J\033[H" % osc,
               "%s%s\033[0m  \033[2m(demo stand-in, not a real model)\033[0m\n\n" % (self.color, self.agent),
               "\033[2m>\033[0m %s\n\n" % self.prompt]
        out += ["  \033[2m⎿ %s\033[0m\n" % note for note in self.notes]
        if state == "working":
            mark = "✻" if self.agent == "claude" else "•"
            out.append("\n%s%s Working…\033[0m (4s · esc to interrupt)\n" % (self.color, mark))
        elif state == "blocked":
            if self.agent == "opencode":
                out.append("\n△ Permission required\n  bash: npm test -- checkout\n")
            else:
                out.append("\n" + "─" * 40 + "\n Bash command\n   npm test -- checkout\n\n"
                           " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n"
                           " Esc to cancel · Enter to confirm\n")
        else:
            out.append("\n%s●\033[0m Done. Ready for the next step.\n\n" % self.color + "─" * 40 + "\n> \n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="claude", choices=sorted(AGENTS))
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--title", default=None, help="the name the agent gives its session")
    parser.add_argument("--report-session", choices=["yes", "no"], default=None,
                        help="send the session id to Herdr (default: yes, except Hermes)")
    parser.add_argument("--steps", nargs="*", default=["idle"])
    args = parser.parse_args()
    as_agent_process(args.agent)

    store = AGENTS[args.agent](args.prompt)
    screen = Screen(args.agent, args.prompt, args.title or args.agent.capitalize())
    named = False
    screen.draw("idle", named)
    time.sleep(1.0)  # let Herdr detect the process before the session report arrives
    report = args.report_session or ("no" if args.agent == "hermes" else "yes")
    lifecycle = getattr(store, "full_lifecycle", False) and report == "yes"

    def report_state(state):
        if lifecycle:
            subprocess.run([HERDR, "pane", "report-agent", os.environ.get("HERDR_PANE_ID", ""),
                            "--source", store.source, "--agent", args.agent, "--state", state,
                            "--agent-session-id", store.id],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if lifecycle:
        report_state("idle")
    if lifecycle:
        # Herdr's OpenCode TUI plugin reports the session the TUI selected, unsequenced.
        subprocess.run([HERDR, "pane", "report-agent-session", os.environ.get("HERDR_PANE_ID", ""),
                        "--source", store.source, "--agent", args.agent, "--agent-session-id", store.id,
                        "--session-start-source", "select"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif report == "yes":
        subprocess.run([HERDR, "pane", "report-agent-session", os.environ.get("HERDR_PANE_ID", ""),
                        "--source", store.source, "--agent", args.agent, "--agent-session-id", store.id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for step in args.steps:
        name, _, rest = step.partition(":")
        if name in ("working", "blocked", "idle"):
            if name != "working" and args.title and not named:
                store.named(args.title)  # agents name the session after their first turn
                named = True
            screen.draw(name, named)
            report_state(name)
            if rest:
                time.sleep(float(rest))
        elif name in ("task", "monitor", "subagent"):
            task_id, _, command = rest.partition(":")
            store.task(task_id, command, monitor=(name == "monitor"))
            screen.notes.append("background: %s" % command)
        elif name == "end":
            store.end(rest)
        elif name == "sleep":
            time.sleep(float(rest))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
