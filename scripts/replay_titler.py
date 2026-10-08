#!/usr/bin/env python3
"""Test double for the `command` title backend: replays titles recorded from a real model run.

Reads the plugin's prompt on stdin like any backend, finds the user message, and prints the
title recorded in demo_titles.json for it (after a short delay, like a model call). Unknown
prompts exit non-zero so the plugin keeps its fallback title.
"""

import json
import os
import sys
import time

prompt = sys.stdin.read()
message = prompt.split("User message:\n", 1)[-1].strip()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_titles.json")) as handle:
    recorded = json.load(handle)["titles"]
time.sleep(float(os.environ.get("FOCUS_REPLAY_DELAY", "1.5")))
if message not in recorded:
    sys.exit(1)
print(recorded[message])
