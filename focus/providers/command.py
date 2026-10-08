"""One-shot argv backend. The prompt goes to stdin; it is never interpolated into argv."""

import os
import subprocess
import tempfile


class CommandProvider:
    name = "command"

    def __init__(self, argv, timeout):
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("titles.command must be a non-empty argv list")
        self.argv = list(argv)
        self.timeout = timeout
        # Run outside any project so the CLI picks up no repo settings, hooks or files.
        self.cwd = os.path.join(tempfile.gettempdir(), "herdr-focus-titles-%d" % os.getuid())
        os.makedirs(self.cwd, mode=0o700, exist_ok=True)

    def describe(self):
        return " ".join(self.argv[:4])

    def generate(self, prompt):
        env = dict(os.environ)
        # Never let the naming call inherit the pane's Herdr identity.
        for key in list(env):
            if key.startswith("HERDR_"):
                del env[key]
        proc = subprocess.run(self.argv, input=prompt.encode(), cwd=self.cwd, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=self.timeout, start_new_session=True)
        if proc.returncode != 0:
            raise RuntimeError("%s exited %d: %s" % (self.argv[0], proc.returncode,
                                                     proc.stderr.decode(errors="replace")[-200:]))
        return proc.stdout.decode("utf-8", "replace")
