#!/usr/bin/env python3
"""Build the same fictional multi-agent session for before/after screenshots and e2e runs.

Creates four workspaces backed by throwaway Git repos under ~/Work/focus-demo and starts
scripts/demo_agent.py in them. Every project, prompt and agent here is fictional.
"""

import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "demo_agent.py")
BASE = os.path.expanduser("~/Work/focus-demo")
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"

SCENE = [
    ("shop", "fix/checkout-redirect", [
        ("claude", "can you fix the checkout redirect loop that happens right after login?",
         ["working:4", "blocked"], "Fix checkout redirect loop"),
        ("codex", "review the payment webhook retries and tighten idempotency keys",
         ["working:5", "idle"], "Webhook retry idempotency"),
    ]),
    ("api", "feat/rate-limit", [
        ("opencode", "please watch the CI checks on PR 412 and tell me when they go green",
         ["working:4", "subagent:ci412:Watch PR 412 checks", "idle"], "Watch PR 412 CI"),
        ("claude", "add rate limiting to the public search endpoint", ["working"]),
    ]),
    ("tooling", "main", [
        ("hermes", "let's explore a cleanup of the config parser module",
         ["working:4", "task:watch7:until gh run view 7 --exit-status; do sleep 30; done", "idle"],
         "Config parser cleanup"),
    ]),
    ("docs", "release/0.9", [
        ("codex", "draft the 0.9 release notes from the merged pull requests",
         ["working:4", "task:dev:npm run dev", "task:pr9:gh pr checks 9 --watch", "idle"],
         "Draft 0.9 release notes"),
    ]),
]


def herdr(*args):
    out = subprocess.run([HERDR] + list(args), stdout=subprocess.PIPE, check=True).stdout
    return json.loads(out) if out.strip().startswith(b"{") else out


def repo(name, branch):
    path = os.path.join(BASE, name)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(path, exist_ok=True)
        run = lambda *a: subprocess.run(["git", "-C", path] + list(a), check=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run("init", "-q", "-b", "main")
        with open(os.path.join(path, "README.md"), "w") as handle:
            handle.write("# %s\n\nFictional demo project.\n" % name)
        run("add", ".")
        run("-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "commit", "-qm", "init")
        if branch != "main":
            run("checkout", "-qb", branch)
    return path


def main():
    for name, branch, agents in SCENE:
        cwd = repo(name, branch)
        created = herdr("workspace", "create", "--cwd", cwd, "--label", name, "--no-focus")
        pane = created["result"]["root_pane"]["pane_id"]
        for index, (agent, prompt, steps, *title) in enumerate(agents):
            if index:
                split = herdr("pane", "split", pane, "--direction", "right", "--no-focus", "--cwd", cwd)
                pane = split["result"]["pane"]["pane_id"]
            cmd = [sys.executable, AGENT, "--agent", agent, "--prompt", prompt]
            if title:
                cmd += ["--title", title[0]]
            cmd += ["--steps"] + steps
            herdr("pane", "run", pane, " ".join(shlex.quote(c) for c in cmd))
            time.sleep(0.3)
    # Close the initial empty workspace so only the scene shows.
    if "--keep-first" not in sys.argv:
        for ws in herdr("workspace", "list")["result"]["workspaces"]:
            if ws["label"] not in [s[0] for s in SCENE]:
                subprocess.run([HERDR, "workspace", "close", ws["workspace_id"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
