import copy
import tempfile
import unittest

from focus import attention as A
from focus.daemon import Daemon, PANE_TOKENS
from focus.state import Store, new_record


class MetadataCleanupTest(unittest.TestCase):
    def setUp(self):
        self.daemon = object.__new__(Daemon)
        d = self.daemon
        d.source = "plugin:focus"
        d.published = {}
        d.published_labels = {}
        d.published_ws = {}
        d.rows = {}
        d.snapshot = {"panes": [{"pane_id": "p"}], "workspaces": []}
        d.config = {"sidebar": {"layout": "stable", "elapsed": False},
                    "triage": {"auto_settle_days": 0}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d.store = Store(self.tmp.name)
        d.glyphs = {"watch": "@", "pin": "^"}
        self.stored = {"plugin:focus": {"tokens": {}, "state_labels": {}},
                       "other:source": {"tokens": {"other": "keep"},
                                        "state_labels": {"done": "Other"}}}
        self.calls = []
        self.fail_requests = False
        d.request = self.request
        self.labels = A.client_state_labels(new_record("k"), 7, 1, 1000,
                                             d.glyphs, False)
        self.tokens: dict[str, str | None] = {key: None for key in PANE_TOKENS}
        self.tokens.update(ft="Old agent", fwatch="@ 1")
        d.report_pane("p", self.tokens, self.labels)
        self.calls.clear()

    def request(self, method, params):
        self.calls.append(copy.deepcopy((method, params)))
        if self.fail_requests:
            return None
        entry = self.stored[params["source"]]
        for key, value in params.get("tokens", {}).items():
            if value is None:
                entry["tokens"].pop(key, None)
            else:
                entry["tokens"][key] = value
        # Herdr patches tokens but replaces this source's labels on each report.
        entry["state_labels"] = dict(params.get("state_labels", {}))
        return {}  # Empty successful responses must not be treated as failure.

    def test_failed_agent_departure_cleanup_retries_until_server_metadata_is_clear(self):
        d = self.daemon
        other = copy.deepcopy(self.stored["other:source"])
        self.fail_requests = True
        d.publish({"p": {"pane_id": "p"}}, 1000)
        d.publish({"p": {"pane_id": "p"}}, 1001)
        self.fail_requests = False
        d.publish({"p": {"pane_id": "p"}}, 1002)
        self.assertEqual(self.stored[d.source], {"tokens": {}, "state_labels": {}})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertEqual(self.calls[1], self.calls[2])
        self.assertTrue(self.calls[-1][1]["clear_state_labels"])
        self.assertNotIn("p", d.published)
        self.assertNotIn("p", d.published_labels)
        self.assertEqual(self.stored["other:source"], other)
        d.publish({"p": {"pane_id": "p"}}, 1003)
        self.assertEqual(len(self.calls), 3)

    def test_failed_cleanup_keeps_last_successful_publication_for_retry(self):
        d = self.daemon
        self.fail_requests = True
        d.publish({"p": {"pane_id": "p"}}, 1000)
        self.assertEqual(d.published.get("p"), self.tokens)
        self.assertEqual(d.published_labels.get("p"), self.labels)
        self.assertEqual(self.stored[d.source]["state_labels"], self.labels)

    def test_closed_pane_can_be_forgotten_without_reporting_metadata(self):
        d = self.daemon
        self.fail_requests = True
        d.publish({}, 1000)
        self.assertEqual(self.calls, [])
        self.assertNotIn("p", d.published)
        self.assertNotIn("p", d.published_labels)

    def test_reconnect_retains_failed_departure_cleanup_until_retry_succeeds(self):
        from unittest.mock import Mock, patch

        d = self.daemon
        other = copy.deepcopy(self.stored["other:source"])
        self.fail_requests = True
        d.publish({"p": {"pane_id": "p"}}, 1000)
        d.socket_path = "unused-test.sock"
        d.lifecycle = d.status_sub = None
        d.selector = Mock()
        with patch("focus.daemon.transport.Subscription"), \
                patch.object(d, "apply_view"), patch.object(d, "log"), \
                patch.object(d, "refresh", side_effect=lambda: d.publish(
                    {"p": {"pane_id": "p"}}, 1001)):
            d.connect()
        self.fail_requests = False
        d.publish({"p": {"pane_id": "p"}}, 1002)
        self.assertEqual(self.stored[d.source], {"tokens": {}, "state_labels": {}})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1][1]["tokens"], {k: None for k in PANE_TOKENS})
        self.assertTrue(self.calls[-1][1]["clear_state_labels"])
        self.assertNotIn("p", d.published)
        self.assertNotIn("p", d.published_labels)
        self.assertEqual(self.stored["other:source"], other)
        d.publish({"p": {"pane_id": "p"}}, 1003)
        self.assertEqual(len(self.calls), 3)

    def test_config_reload_retains_failed_departure_cleanup_until_retry_succeeds(self):
        from unittest.mock import patch
        from focus import config as configmod

        d = self.daemon
        other = copy.deepcopy(self.stored["other:source"])
        self.fail_requests = True
        d.publish({"p": {"pane_id": "p"}}, 1000)
        config = copy.deepcopy(configmod.DEFAULTS)
        config["glyphs"] = dict(d.glyphs)
        config["titles"]["enabled"] = False
        with patch("focus.daemon.configmod.load", return_value=(config, None)):
            d.reload_config()
        d.publish({"p": {"pane_id": "p"}}, 1001)
        self.fail_requests = False
        d.publish({"p": {"pane_id": "p"}}, 1002)
        self.assertEqual(self.stored[d.source], {"tokens": {}, "state_labels": {}})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1][1]["tokens"], {k: None for k in PANE_TOKENS})
        self.assertTrue(self.calls[-1][1]["clear_state_labels"])
        self.assertNotIn("p", d.published)
        self.assertNotIn("p", d.published_labels)
        self.assertEqual(self.stored["other:source"], other)
        d.publish({"p": {"pane_id": "p"}}, 1003)
        self.assertEqual(len(self.calls), 3)

    def test_cache_invalidation_republishes_all_active_pane_tokens_and_labels(self):
        from unittest.mock import Mock, patch
        from focus import config as configmod, tasks

        d = self.daemon
        config = copy.deepcopy(configmod.DEFAULTS)
        config["glyphs"] = dict(d.glyphs)
        config["titles"]["enabled"] = False
        config["sidebar"]["elapsed"] = False
        d.config = config
        d.watches = tasks.Watches(config)
        d.store.records["k"] = new_record("k")
        d.rows = {"p": {"key": "k", "status": "working", "seq": 7,
                        "title": "Active agent", "pane": {"pane_id": "p"}}}
        d.socket_path = "unused-test.sock"
        d.lifecycle = d.status_sub = None
        d.selector = Mock()
        other = copy.deepcopy(self.stored["other:source"])
        for method in ("connect", "reload_config"):
            with self.subTest(method=method):
                d.publish({"p": {"pane_id": "p"}}, 1000)
                published = d.published["p"]
                assert published is not None
                tokens = dict(published)
                labels = dict(d.published_labels["p"])
                self.calls.clear()
                with patch("focus.daemon.transport.Subscription"), \
                        patch("focus.daemon.configmod.load", return_value=(config, None)), \
                        patch.object(d, "apply_view"), patch.object(d, "log"), \
                        patch.object(d, "refresh", side_effect=lambda: d.publish(
                            {"p": {"pane_id": "p"}}, 1000)):
                    getattr(d, method)()
                if method == "reload_config":
                    d.publish({"p": {"pane_id": "p"}}, 1000)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.calls[0][1]["tokens"], tokens)
                self.assertEqual(set(tokens), set(PANE_TOKENS))
                self.assertEqual(self.calls[0][1]["state_labels"], labels)
                self.assertFalse(self.calls[0][1]["clear_state_labels"])
                self.assertEqual(self.stored["other:source"], other)
                d.publish({"p": {"pane_id": "p"}}, 1000)
                self.assertEqual(len(self.calls), 1)

    def test_cache_invalidation_still_forgets_closed_panes_without_cleanup_io(self):
        from unittest.mock import Mock, patch
        from focus import config as configmod

        d = self.daemon
        config = copy.deepcopy(configmod.DEFAULTS)
        config["glyphs"] = dict(d.glyphs)
        config["titles"]["enabled"] = False
        d.socket_path = "unused-test.sock"
        d.lifecycle = d.status_sub = None
        d.selector = Mock()
        for method in ("connect", "reload_config"):
            with self.subTest(method=method):
                self.fail_requests = False
                d.report_pane("p", self.tokens, self.labels)
                self.calls.clear()
                self.fail_requests = True
                d.publish({"p": {"pane_id": "p"}}, 1000)
                self.assertEqual(len(self.calls), 1)
                with patch("focus.daemon.transport.Subscription"), \
                        patch("focus.daemon.configmod.load", return_value=(config, None)), \
                        patch.object(d, "apply_view"), patch.object(d, "log"), \
                        patch.object(d, "refresh"):
                    getattr(d, method)()
                d.publish({}, 1001)
                self.assertEqual(len(self.calls), 1)
                self.assertNotIn("p", d.published)
                self.assertNotIn("p", d.published_labels)
                d.publish({}, 1002)
                self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
