import os
import tempfile
import tomllib
import unittest

from focus import sidebar

USER = """onboarding = false

[ui]
accent = "blue"

[keys]
prefix = "ctrl+space"
detach = "prefix+d"

# herdr-peek
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "peek.pick"
"""


class SidebarConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.toml")
        with open(self.path, "w") as handle:
            handle.write(USER)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with open(self.path) as handle:
            return handle.read()

    def test_install_is_valid_idempotent_and_backed_up(self):
        _, backup, notes = sidebar.install(self.path)
        self.assertTrue(backup and os.path.exists(backup))
        self.assertEqual(notes, [])
        data = tomllib.loads(self.read())
        rows = data["ui"]["sidebar"]["agents"]["rows"]
        self.assertEqual(rows[0][0], "state_icon")
        commands = [c["command"] for c in data["keys"]["command"]]
        self.assertIn("peek.pick", commands)
        self.assertIn("focus.menu", commands)
        first = self.read()
        _, backup2, _ = sidebar.install(self.path)
        self.assertIsNone(backup2)
        self.assertEqual(self.read(), first)

    def test_uninstall_restores_user_text(self):
        sidebar.install(self.path)
        _, changed = sidebar.uninstall(self.path)
        self.assertTrue(changed)
        self.assertEqual(self.read(), USER)
        self.assertFalse(sidebar.uninstall(self.path)[1])

    def test_user_sidebar_and_bound_keys_are_left_alone(self):
        with open(self.path, "a") as handle:
            handle.write('\n[ui.sidebar.agents]\nrows = [["state_icon", "agent"]]\n\n'
                         '[[keys.command]]\nkey = "prefix+a"\ntype = "plugin_action"\ncommand = "x.y"\n')
        _, _, notes = sidebar.install(self.path)
        data = tomllib.loads(self.read())
        self.assertEqual(data["ui"]["sidebar"]["agents"]["rows"], [["state_icon", "agent"]])
        self.assertTrue(any("sidebar" in note for note in notes))
        self.assertTrue(any("prefix+a" in note for note in notes))
        keys = [c["key"] for c in data["keys"]["command"]]
        self.assertEqual(keys.count("prefix+a"), 1)

    def test_documented_block_matches_what_install_writes(self):
        docs = os.path.join(os.path.dirname(__file__), "..", "docs", "sidebar.toml")
        with open(docs) as handle:
            text = handle.read()
        self.assertTrue(text.endswith(sidebar.SIDEBAR))
        tomllib.loads(text)

    def test_default_herdr_keys_are_not_taken(self):
        keys = {key for key, _, _ in sidebar.KEYS}
        self.assertFalse(keys & sidebar.DEFAULT_PREFIX_KEYS)


if __name__ == "__main__":
    unittest.main()
