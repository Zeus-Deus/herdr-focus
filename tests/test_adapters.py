"""Per-agent adapters against fixtures shaped like each agent's real on-disk records."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from focus import config, tasks, titles
from focus.adapters import claude, codex, copilot, cursor, hermes, opencode, pi

NOW = time.time()


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class EnvCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.saved = {k: os.environ.get(k) for k in self.env}
        for key, sub in self.env.items():
            os.environ[key] = os.path.join(self.root, sub)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class ClaudeTitleTest(EnvCase):
    def test_own_title_and_rename(self):
        path = os.path.join(self.root, "s.jsonl")
        write_jsonl(path, [{"type": "user", "message": {"content": "fix the login loop"}},
                           {"type": "ai-title", "aiTitle": "Fix login redirect loop", "sessionId": "s"}])
        reader = claude.TaskReader(path)
        reader.poll(NOW)
        self.assertEqual(reader.title, "Fix login redirect loop")
        write_jsonl(path, [{"type": "custom-title", "customTitle": "Login work", "sessionId": "s"}])
        self.assertTrue(reader.poll(NOW))
        self.assertEqual(reader.title, "Login work")
        self.assertEqual(claude.title(path), "Login work")


class CodexTest(EnvCase):
    env = {"CODEX_HOME": "codex"}

    def rollout(self, records, thread="0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"):
        path = os.path.join(self.root, "codex", "sessions", "2026", "10", "08",
                            "rollout-2026-10-08T10-00-00-%s.jsonl" % thread)
        write_jsonl(path, records)
        return path

    @staticmethod
    def call(call_id, name, arguments):
        return {"type": "response_item", "payload": {"type": "function_call", "name": name,
                                                     "arguments": json.dumps(arguments), "call_id": call_id}}

    @staticmethod
    def output(call_id, text, kind="function_call_output"):
        return {"type": "response_item", "payload": {"type": kind, "call_id": call_id, "output": text}}

    @staticmethod
    def ended(process_id):
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "process_id": str(process_id), "status": "completed", "exit_code": 0}}}

    def test_background_process_lives_until_its_exit_record(self):
        path = self.rollout([
            self.call("c1", "exec_command", {"cmd": "gh pr checks 412 --watch", "yield_time_ms": 1000}),
            self.output("c1", "Chunk ID: ab12\nWall time: 1.0 seconds\nProcess running with session ID 76500\nOutput:\n"),
            self.call("c2", "exec_command", {"cmd": "ls"}),
            self.output("c2", "Chunk ID: cd34\nProcess exited with code 0\nOutput:\nREADME.md"),
        ])
        watches = tasks.Watches(config.DEFAULTS)
        self.assertTrue(watches.bind("k", "codex", path))
        watches.poll("k", NOW)
        self.assertEqual(watches.describe("k", NOW), [("76500", "monitor", "gh pr checks 412 --watch")])
        write_jsonl(path, [self.ended(76500)])
        watches.poll("k", NOW)
        self.assertEqual(watches.count("k", NOW), 0)

    def test_code_mode_and_legacy_write_stdin_exit(self):
        path = self.rollout([
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "x1",
                                                  "input": "text(await tools.exec_command({cmd: \"npm run dev\"}))"}},
            self.output("x1", [{"text": "Script completed"}, {"text": "{\"chunk_id\":\"e3\",\"session_id\":47239,\"output\":\"\"}"}],
                        kind="custom_tool_call_output"),
            self.call("c2", "exec_command", {"cmd": "until gh run view 9; do sleep 30; done"}),
            self.output("c2", "Process running with session ID 501"),
            self.call("c3", "write_stdin", {"session_id": 501, "chars": ""}),
            self.output("c3", "Process exited with code 0"),
        ])
        reader = codex.TaskReader(path)
        reader.poll(NOW)
        self.assertEqual(set(reader.live(NOW, 0)), {"47239"})
        watches = tasks.Watches(config.DEFAULTS)
        watches.bind("k", "codex", path)
        watches.poll("k", NOW)
        self.assertEqual(watches.count("k", NOW), 0)  # npm run dev is a service, not a watch

    def test_typed_prompts_and_thread_name(self):
        thread = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
        path = self.rollout([
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions"}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "/fast off"}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "review the webhook retries"}]}}},
        ], thread)
        self.assertEqual(codex.user_messages(path), ["review the webhook retries"])
        self.assertEqual(codex.locate({"kind": "id", "value": thread}), path)
        self.assertIsNone(codex.title(path))
        write_jsonl(os.path.join(self.root, "codex", "session_index.jsonl"),
                    [{"id": thread, "thread_name": "Webhook retries", "updated_at": "x"},
                     {"id": thread, "thread_name": "Webhook retry review", "updated_at": "y"}])
        self.assertEqual(codex.title(path), "Webhook retry review")

    def test_thread_name_from_state_db(self):
        thread = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
        path = self.rollout([], thread)
        db = sqlite3.connect(os.path.join(self.root, "codex", "state_5.sqlite"))
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?, ?)", (thread, path, "Merge the clean PR"))
        db.commit()
        db.close()
        self.assertEqual(codex.title(path), "Merge the clean PR")
        self.assertEqual(codex.locate({"kind": "id", "value": thread}), path)


class OpenCodeTest(EnvCase):
    env = {"XDG_DATA_HOME": "data"}

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "data", "opencode"))
        self.db = sqlite3.connect(opencode.db_path())
        self.db.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, time_created INTEGER);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT);
        """)
        self.db.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def add(self, sql, *args):
        self.db.execute(sql, args)
        self.db.commit()

    def test_title_prompt_and_background_subagent(self):
        root = "ses_root00000000aaaa"
        self.add("INSERT INTO session VALUES (?, NULL, ?, ?)", root, "New session - 2026-10-08T10:00:00", 1)
        self.add("INSERT INTO message VALUES ('m1', ?, 1, ?)", root, json.dumps({"role": "user"}))
        self.add("INSERT INTO part VALUES ('p0', 'm1', ?, ?)", root, json.dumps({"type": "text", "text": "ctx", "synthetic": True}))
        self.add("INSERT INTO part VALUES ('p1', 'm1', ?, ?)", root, json.dumps({"type": "text", "text": "fix flaky tests"}))
        self.assertEqual(opencode.locate({"kind": "id", "value": root}), root)
        self.assertIsNone(opencode.title(root))  # placeholder until OpenCode names it
        self.assertEqual(opencode.user_messages(root), ["fix flaky tests"])
        self.add("UPDATE session SET title = 'Fix flaky tests' WHERE id = ?", root)
        self.assertEqual(opencode.title(root), "Fix flaky tests")

        child = "ses_child0000000bbbb"
        self.add("INSERT INTO session VALUES (?, ?, 'Watch CI', ?)", child, root, int(NOW * 1000))
        self.add("INSERT INTO message VALUES ('m2', ?, 2, ?)", child, json.dumps({"role": "assistant", "time": {"created": 1}}))
        watches = tasks.Watches(config.DEFAULTS)
        self.assertTrue(watches.bind("k", "opencode", root))
        watches.poll("k", NOW)
        self.assertEqual(watches.describe("k", NOW), [(child, "monitor", "Watch CI")])
        self.add("UPDATE message SET data = ? WHERE id = 'm2'", json.dumps({"role": "assistant", "time": {"created": 1, "completed": 2}}))
        watches.poll("k", NOW)
        self.assertEqual(watches.count("k", NOW), 0)

    def test_missing_database_is_quiet(self):
        os.remove(opencode.db_path())
        self.assertIsNone(opencode.title("ses_root00000000aaaa"))
        self.assertEqual(opencode.user_messages("ses_root00000000aaaa"), [])


def start_ticks(pid):
    with open("/proc/%d/stat" % pid) as handle:
        return handle.read().rsplit(")", 1)[1].split()[19]


@unittest.skipUnless(os.path.exists("/proc/self/stat"), "needs /proc")
class HermesTest(EnvCase):
    env = {"HERMES_HOME": "hermes"}
    session = "20261008_101500_a1b2c3"

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.root, "hermes", "profiles", "coder")
        os.makedirs(os.path.join(self.home, "runtime"))
        os.makedirs(os.path.join(self.root, "hermes"), exist_ok=True)
        for home in (os.path.join(self.root, "hermes"), self.home):
            db = sqlite3.connect(os.path.join(home, "state.db"))
            db.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                                       display_kind TEXT, _compressed_summary INTEGER DEFAULT 0);
            """)
            db.commit()
            db.close()
        db = sqlite3.connect(os.path.join(self.home, "state.db"))
        db.execute("INSERT INTO sessions VALUES (?, 'Release notes draft')", (self.session,))
        db.executemany("INSERT INTO messages (session_id, role, content, display_kind) VALUES (?, ?, ?, ?)", [
            (self.session, "user", "[IMPORTANT: Background process proc_1 completed]", "process_complete"),
            (self.session, "user", "draft the 0.9 release notes", None),
        ])
        db.commit()
        db.close()
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                      env=dict(os.environ, HERDR_PANE_ID="w9:p1"),
                                      stdin=subprocess.DEVNULL)
        # Popen can return before the kernel has set up the new program's environment.
        deadline = time.time() + 5
        while b"HERDR_PANE_ID" not in self.environ() and time.time() < deadline:
            time.sleep(0.01)

    def environ(self):
        try:
            with open("/proc/%d/environ" % self.child.pid, "rb") as handle:
                return handle.read()
        except OSError:
            return b""

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        super().tearDown()

    def test_session_title_and_prompt(self):
        source = hermes.locate({"kind": "id", "value": self.session})
        self.assertEqual(source, "%s|%s" % (self.home, self.session))
        self.assertEqual(hermes.title(source), "Release notes draft")
        self.assertEqual(hermes.user_messages(source), ["draft the 0.9 release notes"])

    def test_session_found_from_the_pane_without_herdr_integration(self):
        with open(os.path.join(self.home, "runtime", "active_sessions.json"), "w") as handle:
            json.dump({"entries": [{"session_id": "20261008_000000_ffffff", "surface": "cli",
                                    "pid": self.child.pid}]}, handle)
        os.makedirs(os.path.join(self.home, "terminal-sessions"))
        with open(os.path.join(self.home, "terminal-sessions", "tty-dev-null"), "w") as handle:
            json.dump({"session_id": self.session}, handle)  # the CLI moved on with /new
        source = hermes.locate(None, {"pane_id": "w9:p1"})
        self.assertEqual(source, "%s|%s" % (self.home, self.session))
        self.assertIsNone(hermes.locate(None, {"pane_id": "w9:p2"}))

    def test_live_processes_only(self):
        entries = [
            {"session_id": "proc_live", "command": "gh pr checks 3 --watch", "pid": self.child.pid,
             "host_start_time": int(start_ticks(self.child.pid)), "session_key": self.session,
             "started_at": NOW, "notify_on_complete": True},
            {"session_id": "proc_dead", "command": "sleep 1", "pid": 999999999, "host_start_time": 1,
             "session_key": self.session, "started_at": NOW},
            {"session_id": "proc_reused", "command": "x", "pid": self.child.pid, "host_start_time": 1,
             "session_key": self.session, "started_at": NOW},
            {"session_id": "proc_other", "command": "y", "pid": self.child.pid,
             "host_start_time": int(start_ticks(self.child.pid)), "session_key": "other", "started_at": NOW},
        ]
        with open(os.path.join(self.home, "processes.json"), "w") as handle:
            json.dump(entries, handle)
        watches = tasks.Watches(config.DEFAULTS)
        self.assertTrue(watches.bind("k", "hermes", "%s|%s" % (self.home, self.session)))
        watches.poll("k", NOW)
        self.assertEqual(watches.describe("k", NOW), [("proc_live", "monitor", "gh pr checks 3 --watch")])
        self.child.kill()  # exited but not reaped: a zombie still has a /proc entry
        time.sleep(0.2)
        watches.poll("k", NOW)
        self.assertEqual(watches.count("k", NOW), 0)


class SmallAdaptersTest(EnvCase):
    env = {"PI_CODING_AGENT_DIR": "pi"}

    def test_pi_name_and_prompt(self):
        sid = "019f866c-8736-7d71-a245-8c3264d03344"
        path = os.path.join(self.root, "pi", "sessions", "--home-x--", "2026-10-08T10-00-00-000Z_%s.jsonl" % sid)
        write_jsonl(path, [
            {"type": "session", "version": 3, "id": sid, "cwd": "/x"},
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "explore the parser"}]}},
            {"type": "session_info", "name": "Parser cleanup"},
        ])
        self.assertEqual(pi.locate({"kind": "id", "value": sid}), path)
        self.assertEqual(pi.locate({"kind": "path", "value": path}), path)
        self.assertEqual(pi.title(path), "Parser cleanup")
        self.assertEqual(pi.user_messages(path), ["explore the parser"])

    def test_cursor_prompt(self):
        path = os.path.join(self.root, "t.jsonl")
        write_jsonl(path, [{"role": "user", "message": {"content": [{"type": "text", "text": "<user_info>x"}]}},
                           {"role": "user", "message": {"content": [{"type": "text", "text": "add dark mode"}]}}])
        self.assertEqual(cursor.user_messages(path), ["add dark mode"])

    def test_copilot_summary(self):
        folder = os.path.join(self.root, "copilot")
        os.makedirs(folder)
        with open(os.path.join(folder, "workspace.yaml"), "w") as handle:
            handle.write("id: abc\nsummary: Fix the CI cache\ncwd: /x\n")
        self.assertEqual(copilot.title(folder), "Fix the CI cache")


class TerminalTitleTest(unittest.TestCase):
    def test_agent_title_formats(self):
        def fallback(title, agent):
            return titles.fallback({"terminal_title_stripped": title, "agent": agent}, [], 40)
        self.assertEqual(fallback("OC | Fix flaky tests", "opencode"), "Fix flaky tests")
        self.assertEqual(fallback("OpenCode", "opencode"), "opencode")
        self.assertEqual(fallback("π - Parser cleanup - herdr", "pi"), "Parser cleanup")
        self.assertEqual(fallback("π - /home/x/herdr", "pi"), "pi")


if __name__ == "__main__":
    unittest.main()
