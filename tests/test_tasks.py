import json
import os
import tempfile
import time
import unittest

from focus import config, tasks
from focus.adapters import claude, codex

NOW = time.time() + 5  # tasks read from the backlog date from the file mtime


def tool_use(tool_id, name, args):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": args}]}}


def tool_result(tool_id, result, text="Command running in background with ID: x."):
    return {"type": "user", "toolUseResult": result, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def notification(task_id, status):
    content = ("<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n"
               "<summary>x</summary>\n</task-notification>" % (task_id, status))
    return {"type": "user", "origin": {"kind": "task-notification"}, "message": {"content": content}}


class TranscriptCase(unittest.TestCase):
    def setUp(self):
        global NOW
        NOW = time.time() + 5
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.jsonl")
        open(self.path, "w").close()
        self.watches = tasks.Watches(config.DEFAULTS)
        self.assertTrue(self.watches.bind("k", "claude", self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, *records, partial=None):
        with open(self.path, "a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            if partial:
                handle.write(partial)

    def poll(self):
        return self.watches.poll("k", NOW)

    def test_monitor_and_background_watch_then_independent_ends(self):
        self.append(tool_use("t1", "Monitor", {"command": "gh pr checks 1 --watch", "timeout_ms": 0}),
                    tool_result("t1", {"taskId": "bm1"}),
                    tool_use("t2", "Bash", {"command": "until gh run view; do sleep 30; done",
                                            "run_in_background": True}),
                    tool_result("t2", {"backgroundTaskId": "bw2"}))
        self.assertTrue(self.poll())
        self.assertEqual(self.watches.count("k", NOW), 2)
        self.append(notification("bm1", "completed"))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 1)
        self.append(notification("bw2", "killed"))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)

    def test_dev_server_is_not_a_watch(self):
        self.append(tool_use("t1", "Bash", {"command": "npm run dev", "run_in_background": True}),
                    tool_result("t1", {"backgroundTaskId": "bd1"}))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)
        self.assertEqual(self.watches.describe("k", NOW)[0][1], "service")

    def test_task_stop_ends_a_watch(self):
        self.append(tool_use("t1", "Monitor", {"command": "tail -f log"}),
                    tool_result("t1", {"taskId": "bm1"}),
                    tool_use("t2", "TaskStop", {"task_id": "bm1"}),
                    tool_result("t2", {"message": "stopped"}, "Successfully stopped task"))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)

    def test_foreground_bash_and_failed_launches_are_ignored(self):
        self.append(tool_use("t1", "Bash", {"command": "ls"}), tool_result("t1", {}),
                    tool_use("t2", "Monitor", {"command": "x"}))
        bad = tool_result("t2", {"taskId": "bm9"})
        bad["message"]["content"][0]["is_error"] = True
        self.append(bad)
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)

    def test_partial_lines_wait_for_the_rest(self):
        record = json.dumps(tool_result("t1", {"taskId": "bm1"}))
        self.append(tool_use("t1", "Monitor", {"command": "x"}), partial=record[:20])
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)
        with open(self.path, "a") as handle:
            handle.write(record[20:] + "\n")
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 1)

    def test_stale_evidence_expires(self):
        self.append(tool_use("t1", "Monitor", {"command": "x", "timeout_ms": 60000}),
                    tool_result("t1", {"taskId": "bm1"}))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 1)
        self.assertEqual(self.watches.count("k", NOW + 3600), 0)

    def test_dropping_the_session_clears_evidence(self):
        self.append(tool_use("t1", "Monitor", {"command": "x"}), tool_result("t1", {"taskId": "bm1"}))
        self.poll()
        self.assertTrue(self.watches.drop("k"))
        self.assertEqual(self.watches.count("k", NOW), 0)

    def test_relaunch_ignores_older_tasks(self):
        self.append(tool_use("t1", "Monitor", {"command": "x"}), tool_result("t1", {"taskId": "bm1"}))
        self.poll()
        self.assertEqual(self.watches.count("k", NOW, since=None), 1)
        self.assertEqual(self.watches.count("k", NOW, since=NOW + 1), 0)

    def test_truncated_transcript_resets(self):
        self.append(tool_use("t1", "Monitor", {"command": "x"}), tool_result("t1", {"taskId": "bm1"}))
        self.poll()
        open(self.path, "w").close()
        self.poll()
        self.assertEqual(self.watches.count("k", NOW), 0)


class UnsupportedProviderTest(unittest.TestCase):
    def test_codex_and_hermes_get_no_automatic_watches(self):
        watches = tasks.Watches(config.DEFAULTS)
        self.assertFalse(watches.bind("k", "codex", "/tmp/x"))
        self.assertFalse(watches.bind("k", "hermes", "/tmp/x"))
        self.assertEqual(watches.count("k", NOW), 0)


class UserMessageTest(unittest.TestCase):
    def test_claude_skips_meta_commands_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w") as handle:
                for record in (
                    {"type": "user", "isMeta": True, "message": {"content": "Caveat: meta"}},
                    {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
                    notification("b1", "completed"),
                    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
                    {"type": "user", "message": {"content": [{"type": "text", "text": "fix the login bug"}]}},
                    {"type": "user", "message": {"content": "and add a test"}},
                ):
                    handle.write(json.dumps(record) + "\n")
            self.assertEqual(claude.user_messages(path), ["fix the login bug", "and add a test"])

    def test_codex_skips_injected_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.jsonl")
            with open(path, "w") as handle:
                for text in ("# AGENTS.md instructions", "<environment_context>x</environment_context>",
                             "review the webhook retries"):
                    handle.write(json.dumps({"type": "response_item", "payload": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}) + "\n")
            self.assertEqual(codex.user_messages(path), ["review the webhook retries"])

    def test_transcript_lookup_rejects_odd_ids(self):
        self.assertIsNone(claude.transcript({"kind": "id", "value": "../../etc/passwd"}))
        self.assertIsNone(claude.transcript(None))


if __name__ == "__main__":
    unittest.main()
