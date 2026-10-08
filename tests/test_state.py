import tempfile
import unittest

from focus import state


class IdentityTest(unittest.TestCase):
    def test_session_beats_terminal_and_pane_ids_are_never_keys(self):
        pane = {"pane_id": "w1:p1", "terminal_id": "term_1", "agent": "claude"}
        self.assertEqual(state.record_key(pane), "terminal:term_1")
        pane["agent_session"] = {"agent": "claude", "kind": "id", "value": "abc"}
        self.assertEqual(state.record_key(pane), "session:claude:abc")

    def test_scope_separates_servers(self):
        self.assertNotEqual(state.scope_id("/a/herdr.sock"), state.scope_id("/b/herdr.sock"))


class RekeyTest(unittest.TestCase):
    def test_terminal_record_moves_to_session_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(tmp)
            store.get("terminal:t")["pinned"] = True
            store.rekey("terminal:t", "session:claude:s")
            self.assertNotIn("terminal:t", store.records)
            self.assertTrue(store.records["session:claude:s"]["pinned"])
            self.assertEqual(store.records["session:claude:s"]["key"], "session:claude:s")

    def test_existing_session_record_keeps_its_state_and_gains_new_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(tmp)
            target = store.get("session:claude:s")
            target["settled"] = True
            temp = store.get("terminal:t")
            temp.update(pinned=True, title="Mine", title_owner="manual")
            store.rekey("terminal:t", "session:claude:s")
            merged = store.records["session:claude:s"]
            self.assertTrue(merged["settled"] and merged["pinned"])
            self.assertEqual((merged["title"], merged["title_owner"]), ("Mine", "manual"))


class PersistenceTest(unittest.TestCase):
    def test_round_trip_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(tmp)
            store.get("old")["last_live"] = 0
            pinned = store.get("pinned")
            pinned.update(last_live=0, pinned=True)
            store.get("live")["last_live"] = 0
            store.prune({"live"}, now=state.PRUNE_AFTER + 10)
            store.save()
            reloaded = state.Store(tmp)
            self.assertEqual(sorted(reloaded.records), ["live", "pinned"])

    def test_corrupt_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(tmp + "/state.json", "w") as handle:
                handle.write("{not json")
            self.assertEqual(state.Store(tmp).records, {})


if __name__ == "__main__":
    unittest.main()
