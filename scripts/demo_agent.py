#!/usr/bin/env python3
"""A stand-in coding agent for demos and end-to-end tests. Not a real agent or model.

It runs under a process named after the agent (claude/codex), so Herdr detects it the
normal way and reads its state from the screen and terminal title, exactly as for the
real CLI. It reports its session like Herdr's official integrations do and writes a
transcript in that agent's format (first prompt, background tasks, task notifications).

    demo_agent.py --agent claude --prompt "fix the checkout redirect loop" \
        --steps working:6 monitor:b1:gh-pr-checks working:4 idle
Steps: working[:secs] | blocked[:secs] | idle[:secs] | task:ID:CMD (background Bash) |
       monitor:ID:CMD (Monitor tool) | end:ID | sleep:secs
"""

import argparse
import json
import os
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
    bindir = os.path.join(os.path.expanduser("~/.cache/focus-demo/bin"))
    os.makedirs(bindir, exist_ok=True)
    link = os.path.join(bindir, agent)
    if not os.path.islink(link):
        os.symlink(os.path.abspath(__file__), link)
    os.execv(link, [link] + sys.argv[1:])


def write(path, record):
    record.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
    with open(path, "a") as handle:
        handle.write(json.dumps(record) + "\n")


class Screen:
    def __init__(self, agent, prompt, title):
        self.agent, self.prompt, self.title = agent, prompt, title
        self.notes = []
        self.color = "\033[38;2;240;160;112m" if agent == "claude" else "\033[38;2;130;170;255m"

    def draw(self, state):
        if state == "working":
            osc = "%s %s" % (SPINNER, self.title)
        else:
            osc = ("✳ %s" % self.title) if self.agent == "claude" else self.title
        out = ["\033]0;%s\007\033[2J\033[H" % osc,
               "%s%s\033[0m  \033[2m(demo stand-in, not a real model)\033[0m\n\n" % (self.color, self.agent),
               "\033[2m>\033[0m %s\n\n" % self.prompt]
        out += ["  \033[2m⎿ %s\033[0m\n" % note for note in self.notes]
        if state == "working":
            out.append("\n%s✻ Working…\033[0m (4s · esc to interrupt)\n" % self.color)
        elif state == "blocked":
            out.append("\n" + "─" * 40 + "\n Bash command\n   npm test -- checkout\n\n"
                       " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n"
                       " Esc to cancel · Enter to confirm\n")
        else:
            out.append("\n%s●\033[0m Done. Ready for the next step.\n\n" % self.color + "─" * 40 + "\n> \n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="claude")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--title", default=None, help="terminal title the agent sets")
    parser.add_argument("--steps", nargs="*", default=["idle"])
    args = parser.parse_args()
    as_agent_process(args.agent)

    pane = os.environ.get("HERDR_PANE_ID")
    session = str(uuid.uuid4())
    if args.agent == "codex":
        folder = os.path.join(os.path.expanduser("~/.codex/sessions"), time.strftime("%Y/%m/%d"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S"), session))
        write(path, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>demo</environment_context>"}]}})
        write(path, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": args.prompt}]}})
    else:
        folder = os.path.expanduser("~/.claude/projects/-focus-demo")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, session + ".jsonl")
        write(path, {"type": "user", "message": {"role": "user", "content": args.prompt}})

    screen = Screen(args.agent, args.prompt, args.title or args.agent.capitalize())
    screen.draw("idle")
    time.sleep(1.0)  # let Herdr detect the process before the session report arrives
    subprocess.run([HERDR, "pane", "report-agent-session", pane, "--source", "herdr:%s" % args.agent,
                    "--agent", args.agent, "--agent-session-id", session],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for step in args.steps:
        name, _, rest = step.partition(":")
        if name in ("working", "blocked", "idle"):
            screen.draw(name)
            if rest:
                time.sleep(float(rest))
        elif name in ("task", "monitor"):
            task_id, _, command = rest.partition(":")
            tool_id = "toolu_" + uuid.uuid4().hex[:12]
            if name == "monitor":
                use = {"type": "tool_use", "id": tool_id, "name": "Monitor",
                       "input": {"command": command, "description": command, "timeout_ms": 3600000}}
                result = {"taskId": task_id}
            else:
                use = {"type": "tool_use", "id": tool_id, "name": "Bash",
                       "input": {"command": command, "description": command, "run_in_background": True}}
                result = {"backgroundTaskId": task_id}
            write(path, {"type": "assistant", "message": {"role": "assistant", "content": [use]}})
            write(path, {"type": "user", "toolUseResult": result, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": "Command running in background with ID: %s." % task_id}]}})
            screen.notes.append("background: %s" % command)
        elif name == "end":
            write(path, {"type": "user", "origin": {"kind": "task-notification"}, "message": {
                "role": "user", "content": "<task-notification>\n<task-id>%s</task-id>\n"
                "<status>completed</status>\n<summary>Background command completed</summary>\n"
                "</task-notification>" % rest}})
        elif name == "sleep":
            time.sleep(float(rest))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
