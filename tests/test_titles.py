import threading
import unittest

from focus import titles


class SanitizeTest(unittest.TestCase):
    def test_cleans_model_output(self):
        self.assertEqual(titles.sanitize('"Fix checkout redirect loop."', 40), "Fix checkout redirect loop")
        self.assertEqual(titles.sanitize('{"title": "Rate limit search"}', 40), "Rate limit search")
        self.assertEqual(titles.sanitize("Title: **Parser cleanup**\nextra", 40), "Parser cleanup")
        self.assertIsNone(titles.sanitize("   ", 40))
        self.assertIsNone(titles.sanitize("New thread", 40))

    def test_clips_on_a_word(self):
        self.assertEqual(titles.sanitize("Investigate flaky payment webhook retries", 24),
                         "Investigate flaky")


class RedactTest(unittest.TestCase):
    def test_secrets_never_reach_the_prompt(self):
        text = titles.redact("use sk-abcdefghijklmnopqrstuv and password: hunter22 ghp_" + "a" * 30)
        self.assertNotIn("sk-abc", text)
        self.assertNotIn("hunter22", text)
        self.assertNotIn("ghp_aaaa", text)


class FallbackTest(unittest.TestCase):
    def test_prefers_a_meaningful_terminal_title(self):
        pane = {"terminal_title_stripped": "Checkout redirect loop fix", "agent": "claude"}
        self.assertEqual(titles.fallback(pane, ["anything"], 40), "Checkout redirect loop fix")

    def test_ignores_shell_prompts_and_agent_names(self):
        for title in ("zeus@host:~/x", "Claude Code", "codex", "~/Projects"):
            pane = {"terminal_title_stripped": title, "agent": "claude"}
            self.assertEqual(titles.fallback(pane, ["please fix the login page"], 40), "Fix the login page")

    def test_strips_codex_project_suffix(self):
        pane = {"terminal_title_stripped": "Research workflows | codemux", "agent": "codex"}
        self.assertEqual(titles.fallback(pane, [], 40), "Research workflows")

    def test_heuristic_drops_filler_and_dangling_words(self):
        self.assertEqual(titles.heuristic("can you fix the checkout redirect loop that happens", 40),
                         "Fix the checkout redirect loop")
        self.assertEqual(titles.heuristic("draft the 0.9 release notes from the merged PRs", 40),
                         "Draft the 0.9 release notes")

    def test_agent_name_is_the_last_resort(self):
        self.assertEqual(titles.fallback({"agent": "hermes"}, [], 40), "hermes")


class FakeProvider:
    name = "fake"

    def __init__(self, reply, gate=None):
        self.reply, self.gate, self.prompts = reply, gate, []

    def describe(self):
        return "fake"

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.gate:
            self.gate.wait(5)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class WorkerTest(unittest.TestCase):
    def wait_for(self, worker, done):
        done.wait(5)
        return worker.drain()

    def test_result_and_inflight_dedupe(self):
        done = threading.Event()
        gate = threading.Event()
        provider = FakeProvider("Fix login flow", gate)
        worker = titles.Worker(provider, 40, 10, done.set)
        self.assertTrue(worker.submit("k", 1, "h", "context"))
        self.assertFalse(worker.submit("k", 1, "h", "context"))
        gate.set()
        results = self.wait_for(worker, done)
        self.assertEqual(results, [("k", 1, "h", "Fix login flow", None)])
        self.assertIn("User message:\ncontext", provider.prompts[0])

    def test_backend_failure_reports_an_error(self):
        done = threading.Event()
        worker = titles.Worker(FakeProvider(RuntimeError("boom")), 40, 10, done.set)
        worker.submit("k", 1, "h", "context")
        (key, _, _, title, error), = self.wait_for(worker, done)
        self.assertIsNone(title)
        self.assertIn("boom", error)


if __name__ == "__main__":
    unittest.main()
