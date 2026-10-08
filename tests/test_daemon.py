import json
import os
import socket
import tempfile
import threading
import time
import unittest

from focus import daemon as daemonmod
from focus import titles
from tests.fake_herdr import FakeHerdr


def wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("timed out waiting")


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.prompts = []

    def describe(self):
        return "fake"

    def generate(self, prompt):
        self.prompts.append(prompt)
        return "Generated title"


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.config_dir = os.path.join(root, "config")
        os.makedirs(self.config_dir)
        with open(os.path.join(self.config_dir, "config.toml"), "w") as handle:
            handle.write('[glyphs]\nwatch = "@"\npin = "^"\n')
        self.env = {"HERDR_PLUGIN_CONFIG_DIR": self.config_dir}
        os.environ.update(self.env)
        self.herdr = FakeHerdr(os.path.join(root, "herdr.sock"))
        self.herdr.add_agent("w1:p1", "working", "term_1", session="s-1")
        self.herdr.add_agent("w1:p2", "blocked", "term_2", agent="codex", session="s-2")
        self.daemon = daemonmod.Daemon(self.herdr.path, os.path.join(root, "state"), "focus")
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus"))

    def tearDown(self):
        self.daemon.running = False
        self.daemon.poke()
        self.thread.join(3)
        self.herdr.stop()
        for key in self.env:
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def act(self, action, pane_id=None, **extra):
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(self.daemon.control_path)
        payload = {"op": "act", "action": action, "pane_id": pane_id}
        payload.update(extra)
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            data += sock.recv(65536)
        sock.close()
        return json.loads(data)

    def test_initial_projection_view_and_rollup(self):
        p1, p2 = self.herdr.tokens("w1:p1"), self.herdr.tokens("w1:p2")
        self.assertEqual(p1["fstatus"], "Working")
        self.assertIn("ft_quiet", p1)
        self.assertEqual(p2["fstatus"], "Needs you")
        self.assertIn("ft_hot", p2)
        wait(lambda: self.herdr.workspace_tokens("w1").get("fws") == "Needs you")
        self.assertEqual(self.herdr.view["source"], "plugin:focus")
        self.assertNotIn("filter", self.herdr.view)  # settled rows show, sorted last

    def test_finished_turn_review_then_focus_acknowledges(self):
        self.herdr.set_status("w1:p1", "idle")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Done")
        self.assertIn("ft_hot", self.herdr.tokens("w1:p1"))
        self.herdr.focus("w1:p1")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Idle")

    def test_manual_unread_survives_focus(self):
        self.herdr.set_status("w1:p1", "idle")
        self.herdr.focus("w1:p1")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Idle")
        self.assertTrue(self.act("toggle-unread", "w1:p1")["ok"])
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Unread")
        self.herdr.focus("w1:p2")
        self.herdr.focus("w1:p1")
        time.sleep(0.4)
        self.assertEqual(self.herdr.tokens("w1:p1")["fstatus"], "Unread")

    def test_guardrails_settle_and_undo(self):
        reply = self.act("toggle-settle", "w1:p2")
        self.assertFalse(reply["ok"])
        self.assertIn("waiting for your input", reply["message"])
        self.assertFalse(self.act("toggle-settle", "w1:p1")["ok"])  # still working
        self.herdr.set_status("w1:p1", "idle")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Done")
        self.assertTrue(self.act("toggle-settle", "w1:p1")["ok"])
        wait(lambda: self.herdr.tokens("w1:p1").get("fhide") == "1")
        self.assertEqual(self.herdr.tokens("w1:p1")["fstatus"], "Settled")
        self.assertTrue(self.act("undo")["ok"])
        wait(lambda: "fhide" not in self.herdr.tokens("w1:p1"))
        self.assertEqual(self.herdr.tokens("w1:p1")["fstatus"], "Done")

    def test_new_work_unsettles(self):
        self.herdr.set_status("w1:p1", "idle")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Done")
        self.act("toggle-settle", "w1:p1")
        wait(lambda: self.herdr.tokens("w1:p1").get("fhide") == "1")
        self.herdr.set_status("w1:p1", "working")
        wait(lambda: "fhide" not in self.herdr.tokens("w1:p1"))

    def test_target_identity_is_checked(self):
        reply = self.act("pin", "w1:p1", key="session:claude:someone-else")
        self.assertFalse(reply["ok"])
        self.assertIn("different conversation", reply["message"])
        self.assertFalse(self.act("pin", "w9:p9")["ok"])

    def test_pin_survives_a_pane_move(self):
        self.act("pin", "w1:p1")
        wait(lambda: self.herdr.tokens("w1:p1").get("frank") == "0")
        self.herdr.move("w1:p1", "w1:p7")
        wait(lambda: self.herdr.tokens("w1:p7").get("frank") == "0")
        self.assertTrue(self.herdr.tokens("w1:p7")["ft_quiet"].startswith("^ "))

    def test_manual_title_beats_a_late_model_result(self):
        provider = FakeProvider()
        self.daemon.worker = titles.Worker(provider, 32, 60, self.daemon.poke)
        reply = self.act("rename", "w1:p1", title="  My   own title ")
        self.assertTrue(reply["ok"])
        wait(lambda: self.herdr.tokens("w1:p1").get("ft_quiet") == "My own title")
        record = self.daemon.store.records["session:claude:s-1"]
        stale = record["title_generation"] - 1
        self.daemon.worker.results.put(("session:claude:s-1", stale, "digest", "Late model title", None))
        self.daemon.poke()
        time.sleep(0.4)
        self.assertEqual(self.herdr.tokens("w1:p1")["ft_quiet"], "My own title")
        self.assertTrue(self.act("title-reset", "w1:p1")["ok"])
        wait(lambda: self.herdr.tokens("w1:p1").get("ft_quiet") != "My own title")

    def test_initial_title_once_from_the_first_prompt(self):
        transcript = os.path.join(self.tmp.name, "claude-title.jsonl")
        with open(transcript, "w") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": "fix the login loop"}}) + "\n")
        provider = FakeProvider()
        self.daemon.worker = titles.Worker(provider, 32, 60, self.daemon.poke)
        self.daemon.config["titles"]["agent_titles"] = False  # name Claude with the model
        from focus.adapters import claude
        original = claude.locate
        claude.locate = lambda session, pane=None: transcript if session else None
        try:
            self.daemon.intents.clear()  # forget the startup lookups that found no transcript
            self.daemon.sources.clear()
            self.herdr.emit("pane.updated", {"pane": {"pane_id": "w1:p1"}})
            wait(lambda: self.herdr.tokens("w1:p1").get("ft_quiet") == "Generated title")
            with open(transcript, "a") as handle:
                handle.write(json.dumps({"type": "user", "message": {"content": "also add tests"}}) + "\n")
            self.daemon.intents.clear()
            self.herdr.emit("pane.updated", {"pane": {"pane_id": "w1:p1"}})
            time.sleep(0.5)
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("fix the login loop", provider.prompts[0])
            self.assertTrue(self.act("title-regenerate", "w1:p1")["ok"])
            wait(lambda: len(provider.prompts) == 2)
            self.assertIn("also add tests", provider.prompts[1])
        finally:
            claude.locate = original

    def test_manual_watch_shows_the_eye(self):
        self.herdr.set_status("w1:p1", "idle")
        self.herdr.focus("w1:p1")
        self.act("watch", "w1:p1")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "@ Manual watch")
        wait(lambda: self.herdr.workspace_tokens("w1").get("fwswatch") == "@")

    def test_settled_rows_sink_and_collapse(self):
        self.herdr.set_status("w1:p1", "idle")
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Done")
        self.assertTrue(self.act("toggle-settle", "w1:p1")["ok"])
        wait(lambda: self.herdr.tokens("w1:p1").get("frank") == "8")
        self.assertTrue(self.act("show-hidden")["ok"])
        wait(lambda: self.herdr.view and "filter" in self.herdr.view
             and self.herdr.view["label"] == "focus · 1 settled")
        self.act("toggle-settle", "w1:p1")
        wait(lambda: self.herdr.view["label"] == "focus")
        self.act("show-hidden")
        wait(lambda: "filter" not in self.herdr.view)

    def test_restart_republishes_tokens(self):
        self.herdr.restart()
        wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Working", timeout=8)
        wait(lambda: self.herdr.view is not None)

    def test_relaunch_in_a_new_terminal_drops_old_watches(self):
        transcript = os.path.join(self.tmp.name, "claude-s-1.jsonl")
        with open(transcript, "w") as handle:
            for record in (
                {"type": "user", "message": {"content": "watch PR 1"}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1",
                    "name": "Monitor", "input": {"command": "gh pr checks 1 --watch"}}]}},
                {"type": "user", "toolUseResult": {"taskId": "bm1"}, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "started"}]}},
            ):
                handle.write(json.dumps(record) + "\n")
        from focus.adapters import claude
        original = claude.locate
        claude.locate = lambda session, pane=None: transcript if session else None
        try:
            self.daemon.sources.clear()
            self.herdr.set_status("w1:p1", "idle")
            self.herdr.focus("w1:p1")
            wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "@ Watching")
            time.sleep(0.05)  # the relaunch happens after the task started
            with self.herdr.lock:
                self.herdr.panes["w1:p1"]["terminal_id"] = "term_relaunched"
            self.herdr.emit("pane.updated", {"pane": {"pane_id": "w1:p1"}})
            wait(lambda: self.herdr.tokens("w1:p1").get("fstatus") == "Idle")
        finally:
            claude.locate = original

    def test_disable_clears_everything_and_exits(self):
        self.herdr.enabled = False
        self.daemon.next_plugin_check = 0
        self.daemon.poke()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual({k for k in self.herdr.tokens("w1:p1") if k.startswith("f")}, set())
        self.assertIsNone(self.herdr.view)

    def test_unchanged_projection_sends_no_reports(self):
        time.sleep(0.3)
        before = len([c for c in self.herdr.calls if c[0] == "pane.report_metadata"])
        self.herdr.emit("pane.updated", {"pane": {"pane_id": "w1:p1"}})
        time.sleep(0.4)
        after = len([c for c in self.herdr.calls if c[0] == "pane.report_metadata"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
