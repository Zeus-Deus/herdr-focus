import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

from focus.providers import command, openai_compatible


class CommandProviderTest(unittest.TestCase):
    def test_prompt_goes_to_stdin_outside_the_project_without_herdr_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "echo.py")
            with open(script, "w") as handle:
                handle.write("import os,sys,json\n"
                             "print(json.dumps({'stdin': sys.stdin.read(), 'argv': sys.argv[1:],"
                             " 'cwd': os.getcwd(), 'herdr': [k for k in os.environ if k.startswith('HERDR_')]}))\n")
            os.environ["HERDR_PANE_ID"] = "w1:p1"
            try:
                provider = command.CommandProvider([sys.executable, script, "--flag"], 10)
                out = json.loads(provider.generate("title this; $(rm -rf /)"))
            finally:
                del os.environ["HERDR_PANE_ID"]
        self.assertEqual(out["stdin"], "title this; $(rm -rf /)")
        self.assertEqual(out["argv"], ["--flag"])
        self.assertEqual(out["herdr"], [])
        self.assertNotEqual(out["cwd"], os.getcwd())

    def test_failure_raises(self):
        provider = command.CommandProvider([sys.executable, "-c", "import sys; sys.exit(3)"], 10)
        with self.assertRaises(RuntimeError):
            provider.generate("x")

    def test_rejects_non_argv(self):
        with self.assertRaises(ValueError):
            command.CommandProvider("claude -p", 10)


class OpenAIProviderTest(unittest.TestCase):
    def test_posts_chat_completion_with_env_key(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"choices": [{"message": {"content": "Rate limit search"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        os.environ["FOCUS_TEST_KEY"] = "secret-key"
        try:
            provider = openai_compatible.OpenAIProvider(
                {"base_url": "http://127.0.0.1:%d/v1" % server.server_port, "model": "m",
                 "api_key_env": "FOCUS_TEST_KEY"}, 5)
            self.assertEqual(provider.generate("p"), "Rate limit search")
        finally:
            del os.environ["FOCUS_TEST_KEY"]
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer secret-key")
        self.assertEqual(seen["body"]["model"], "m")

    def test_missing_key_fails_before_any_request(self):
        provider = openai_compatible.OpenAIProvider(
            {"base_url": "http://127.0.0.1:9", "model": "m", "api_key_env": "FOCUS_MISSING_KEY"}, 1)
        with self.assertRaises(RuntimeError):
            provider.generate("p")


if __name__ == "__main__":
    unittest.main()
