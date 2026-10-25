import unittest

from focus import attention as A
from focus.state import new_record


class ClientLabelsTest(unittest.TestCase):
    def labels(self, record=None, watches=0, seq: int | None = 7):
        return A.client_state_labels(record or new_record("k"), seq, watches, 1000,
                                     {"watch": "@", "pin": "^"})

    def test_native_done_is_distinct_from_server_idle(self):
        labels = self.labels()
        self.assertEqual(labels["done"], "Done")
        self.assertEqual(labels["idle"], "Idle")
        self.assertEqual(labels["working"], "Working")
        self.assertEqual(labels["blocked"], "Needs you")
        self.assertEqual(labels["unknown"], "Unknown")

    def test_watch_survives_client_done_and_disappears_on_completion(self):
        self.assertEqual(self.labels(watches=1)["done"], "Done · @ 1")
        self.assertEqual(self.labels(watches=1)["idle"], "@ Watching")
        self.assertEqual(self.labels(watches=0)["done"], "Done")

    def test_explicit_acknowledgement_is_sequence_bound(self):
        record = new_record("k")
        record["ack_seq"] = 7
        self.assertEqual(self.labels(record)["done"], "Idle")
        self.assertEqual(self.labels(record, seq=8)["done"], "Done")
        self.assertEqual(self.labels(seq=None)["done"], "Done")

    def test_manual_unread_and_parking_still_win(self):
        record = new_record("k")
        record.update(unread=True, unread_reason="manual")
        self.assertEqual(self.labels(record)["done"], "Unread")
        record.update(unread=False, unread_reason=None, settled=True)
        self.assertEqual(self.labels(record)["done"], "Settled")
        self.assertEqual(self.labels(record)["working"], "Working")
        self.assertEqual(self.labels(record)["blocked"], "Needs you")
        record.update(settled=False, snoozed_until=4600)
        self.assertEqual(self.labels(record)["done"], "Snoozed 1h")

    def test_native_labels_ignore_only_automatic_finished_without_mutating_triage(self):
        import copy
        from focus import triage

        record = new_record("k")
        triage.note_status(record, "idle", "working", 1000, focused=False)
        before = copy.deepcopy(record)
        labels = self.labels(record)
        self.assertEqual(labels["idle"], "Idle")
        self.assertEqual(labels["unknown"], "Unknown")
        self.assertEqual(labels["done"], "Done")
        self.assertEqual(record, before)
        # Automatic completion remains server-scoped in legacy tokens and menus.
        self.assertEqual(A.category("idle", record, 0), A.REVIEW)
        self.assertEqual(self.labels(record, watches=1)["idle"], "@ Watching")
        record["ack_seq"] = 7
        self.assertEqual(self.labels(record)["done"], "Idle")
        self.assertEqual(self.labels(record, seq=8)["done"], "Done")
        for reason, expected in (("manual", "Unread"), ("woke", "Woke")):
            with self.subTest(reason=reason):
                record["unread_reason"] = reason
                self.assertEqual(self.labels(record)["idle"], expected)
                self.assertEqual(self.labels(record)["done"], expected)

    def test_parked_native_labels_keep_continuing_watches(self):
        from focus import triage

        for parking in ("settled", "snoozed"):
            for watches, manual, badge in ((1, False, "@ 1"),
                                           (2, False, "@ 2"),
                                           (0, True, "@ manual")):
                with self.subTest(parking=parking, watches=watches, manual=manual):
                    record = new_record("k")
                    triage.mark_read(record, A.REVIEW, 7)
                    record["manual_watch"] = manual
                    if parking == "settled":
                        triage.settle(record, A.REVIEW, 7, 1000)
                        expected = "Settled"
                    else:
                        triage.snooze(record, A.MONITORING, 7, 4600)
                        expected = "Snoozed 1h"
                    labels = self.labels(record, watches=watches)
                    for status in ("idle", "done", "unknown"):
                        self.assertEqual(labels[status], expected + " · " + badge)
                    self.assertEqual(labels["working"], "Working · " + badge)
                    self.assertEqual(labels["blocked"], "Needs you · " + badge)
                    record["manual_watch"] = False
                    self.assertEqual(self.labels(record, watches=0)["done"], expected)
                    triage.unsettle(record, A.MONITORING, 7)
                    record["manual_watch"] = manual
                    monitoring = ("@ Manual watch" if manual else
                                  "@ Watching" if watches == 1 else "@ Watching 2")
                    self.assertEqual(self.labels(record, watches=watches)["done"], monitoring)


if __name__ == "__main__":
    unittest.main()
