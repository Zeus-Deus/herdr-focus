import os
import tempfile
import unittest

from focus import attention as A
from focus import triage
from focus.state import Store, new_record

NOW = 1_000_000.0


class GuardrailTest(unittest.TestCase):
    def test_live_and_blocked_work_cannot_be_parked(self):
        for cat in (A.WORKING, A.NEEDS):
            record = new_record("k")
            with self.assertRaises(triage.Refused):
                triage.settle(record, cat, 1, NOW)
            with self.assertRaises(triage.Refused):
                triage.snooze(record, cat, 1, NOW + 60)
            self.assertFalse(record["settled"])

    def test_monitor_can_be_parked_without_touching_it(self):
        record = new_record("k")
        triage.settle(record, A.MONITORING, 1, NOW)
        self.assertTrue(record["settled"])
        self.assertFalse(record["manual_watch"])


class TransitionTest(unittest.TestCase):
    def test_settle_acknowledges_a_review(self):
        record = new_record("k")
        record.update(unread=True, unread_reason="finished")
        triage.settle(record, A.REVIEW, 7, NOW)
        self.assertFalse(record["unread"])
        self.assertEqual(record["ack_seq"], 7)

    def test_toggle_unread_round_trip(self):
        record = new_record("k")
        triage.toggle_unread(record, A.IDLE, 1)
        self.assertEqual((record["unread"], record["unread_reason"]), (True, "manual"))
        triage.toggle_unread(record, A.UNREAD, 1)
        self.assertFalse(record["unread"])

    def test_unsettle_keeps_row_active_until_new_activity(self):
        record = new_record("k")
        triage.settle(record, A.IDLE, 1, NOW)
        triage.toggle_settle(record, A.IDLE, 1, NOW)
        self.assertFalse(record["settled"])
        self.assertTrue(record["keep_active"])
        triage.note_status(record, "working", "idle", NOW)
        self.assertFalse(record["keep_active"])

    def test_activity_brings_parked_rows_back(self):
        record = new_record("k")
        triage.snooze(record, A.IDLE, 1, NOW + 3600)
        triage.note_status(record, "blocked", "idle", NOW)
        self.assertIsNone(record["snoozed_until"])

    def test_timer_wake_marks_woke(self):
        record = new_record("k")
        triage.snooze(record, A.IDLE, 1, NOW - 1)
        self.assertTrue(triage.due_wake(record, NOW))
        triage.wake(record, A.IDLE, 1, "timer", NOW)
        self.assertEqual((record["unread"], record["unread_reason"]), (True, "woke"))
        self.assertEqual(A.category("idle", record, 0), A.UNREAD)

    def test_finished_turn_becomes_review_unless_you_watched_it(self):
        record = new_record("k")
        triage.note_status(record, "idle", "working", NOW, focused=False)
        self.assertEqual(record["unread_reason"], "finished")
        watched = new_record("k")
        triage.note_status(watched, "idle", "working", NOW, focused=True)
        self.assertFalse(watched["unread"])

    def test_focus_acknowledges_finished_but_never_manual_unread(self):
        finished = new_record("k")
        finished.update(unread=True, unread_reason="finished")
        self.assertTrue(triage.note_focus(finished))
        self.assertFalse(finished["unread"])
        manual = new_record("k")
        manual.update(unread=True, unread_reason="manual")
        self.assertFalse(triage.note_focus(manual))
        self.assertTrue(manual["unread"])

    def test_new_turn_clears_unread(self):
        record = new_record("k")
        record.update(unread=True, unread_reason="manual")
        triage.note_status(record, "working", "idle", NOW)
        self.assertFalse(record["unread"])

    def test_auto_settle_skips_live_unread_pinned_and_watched(self):
        record = new_record("k")
        record["status_since"] = NOW - 4 * 86400
        self.assertTrue(triage.auto_settle_due(record, A.IDLE, 0, NOW, 3))
        self.assertFalse(triage.auto_settle_due(record, A.IDLE, 0, NOW, 0))
        self.assertFalse(triage.auto_settle_due(record, A.IDLE, 1, NOW, 3))
        self.assertFalse(triage.auto_settle_due(record, A.REVIEW, 0, NOW, 3))
        self.assertFalse(triage.auto_settle_due(record, A.WORKING, 0, NOW, 3))
        record["pinned"] = True
        self.assertFalse(triage.auto_settle_due(record, A.IDLE, 0, NOW, 3))

    def test_snooze_presets_are_in_the_future_and_ordered(self):
        presets = triage.snooze_presets(NOW)
        stamps = [stamp for _, stamp in presets]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(stamp > NOW for stamp in stamps))


class UndoTest(unittest.TestCase):
    def test_undo_restores_triage_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            record = store.get("k")
            before = store.snapshot(["k"])
            triage.settle(record, A.IDLE, 1, NOW)
            store.push_undo("Settled", before)
            store.save()
            reloaded = Store(tmp)
            self.assertTrue(reloaded.records["k"]["settled"])
            self.assertEqual(reloaded.pop_undo(), "Settled")
            self.assertFalse(reloaded.records["k"]["settled"])
            self.assertIsNone(reloaded.pop_undo())
            self.assertTrue(os.path.exists(os.path.join(tmp, "state.json")))


if __name__ == "__main__":
    unittest.main()
