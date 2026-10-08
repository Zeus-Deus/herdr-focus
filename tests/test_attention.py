import unittest

from focus import attention as A
from focus.state import new_record

GLYPHS = {"watch": "@", "pin": "^"}
NOW = 1_000_000.0


def rec(**fields):
    record = new_record("k")
    record.update(fields)
    return record


class CategoryTest(unittest.TestCase):
    def test_native_status_wins_over_triage(self):
        self.assertEqual(A.category("blocked", rec(unread=True), 2), A.NEEDS)
        self.assertEqual(A.category("working", rec(unread=True), 2), A.WORKING)
        self.assertEqual(A.category("done", rec(), 0), A.REVIEW)

    def test_finished_unread_reads_as_review_and_manual_as_unread(self):
        self.assertEqual(A.category("idle", rec(unread=True, unread_reason="finished"), 0), A.REVIEW)
        self.assertEqual(A.category("idle", rec(unread=True, unread_reason="manual"), 0), A.UNREAD)

    def test_monitoring_only_when_nothing_else_needs_you(self):
        self.assertEqual(A.category("idle", rec(), 1), A.MONITORING)
        self.assertEqual(A.category("idle", rec(manual_watch=True), 0), A.MONITORING)
        self.assertEqual(A.category("idle", rec(unread=True, unread_reason="finished"), 1), A.REVIEW)
        self.assertEqual(A.category("idle", rec(), 0), A.IDLE)
        self.assertEqual(A.category("unknown", rec(), 0), A.UNKNOWN)


class ProjectionTest(unittest.TestCase):
    def project(self, status, record=None, watches=0, focused=False, layout="stable"):
        return A.project(record or rec(), status, focused, watches, "Fix login", NOW, GLYPHS, layout)

    def test_exactly_one_title_slot(self):
        for status in ("blocked", "working", "done", "idle", "unknown"):
            _, tokens = self.project(status)
            filled = [k for k in ("ft_hot", "ft", "ft_quiet") if tokens[k]]
            self.assertEqual(len(filled), 1, status)

    def test_working_recedes_but_status_stays_readable(self):
        _, tokens = self.project("working", rec(status_since=NOW - 180))
        self.assertEqual(tokens["ft_quiet"], "Fix login")
        self.assertEqual(tokens["fstatus"], "Working 3m")

    def test_needs_you_and_done_pop_out(self):
        self.assertEqual(self.project("blocked")[1]["ft_hot"], "Fix login")
        self.assertEqual(self.project("done")[1]["ft_hot"], "Fix login")
        self.assertEqual(self.project("done")[1]["fstatus"], "Done")

    def test_focused_row_never_recedes(self):
        _, tokens = self.project("working", focused=True)
        self.assertEqual(tokens["ft"], "Fix login")
        self.assertIsNone(tokens["ft_quiet"])

    def test_eye_for_watches_alone_and_beside_a_result(self):
        _, alone = self.project("idle", watches=1)
        self.assertEqual(alone["fstatus"], "@ Watching")
        self.assertIsNone(alone["fwatch"])
        _, two = self.project("idle", watches=2)
        self.assertEqual(two["fstatus"], "@ Watching 2")
        _, beside = self.project("idle", rec(unread=True, unread_reason="finished"), watches=2)
        self.assertEqual(beside["fstatus"], "Done")
        self.assertEqual(beside["fwatch"], "@ 2")
        _, manual = self.project("idle", rec(manual_watch=True))
        self.assertEqual(manual["fstatus"], "@ Manual watch")

    def test_no_fabricated_elapsed_time(self):
        _, tokens = self.project("working", rec(status_since=None))
        self.assertEqual(tokens["fstatus"], "Working")

    def test_parked_rows_hide_but_needs_you_never_does(self):
        self.assertEqual(self.project("idle", rec(settled=True))[1]["fhide"], "1")
        self.assertEqual(self.project("idle", rec(snoozed_until=NOW + 60))[1]["fhide"], "1")
        self.assertIsNone(self.project("idle", rec(snoozed_until=NOW - 1))[1]["fhide"])
        self.assertIsNone(self.project("blocked", rec(settled=True))[1]["fhide"])

    def test_parked_label_wins_over_review(self):
        record = rec(unread=True, unread_reason="manual", snoozed_until=NOW + 3600)
        self.assertEqual(self.project("idle", record)[1]["fstatus"], "Snoozed 1h")
        self.assertEqual(self.project("idle", record)[1]["ft_quiet"], "Fix login")

    def test_snoozed_and_settled_labels(self):
        self.assertEqual(self.project("idle", rec(settled=True))[1]["fstatus"], "Settled")
        self.assertEqual(self.project("idle", rec(snoozed_until=NOW + 7200))[1]["fstatus"], "Snoozed 2h")

    def test_pin_glyph_and_rank(self):
        _, tokens = self.project("idle", rec(pinned=True))
        self.assertEqual(tokens["ft"], "^ Fix login")
        self.assertEqual(tokens["frank"], "0")
        self.assertEqual(self.project("idle")[1]["frank"], "1")

    def test_layouts(self):
        self.assertEqual(self.project("blocked", layout="attention")[1]["frank"], "1")
        self.assertEqual(self.project("working", layout="attention")[1]["frank"], "5")
        self.assertEqual(self.project("working", layout="shelf")[1]["frank"], "2")
        self.assertEqual(self.project("done", layout="shelf")[1]["frank"], "1")


class RollupTest(unittest.TestCase):
    def test_blocked_and_watch_coexist(self):
        tokens = A.rollup([(A.NEEDS, 0, False), (A.MONITORING, 1, False)], GLYPHS)
        self.assertEqual(tokens, {"fws": "Needs you", "fwswatch": "@ 1"})

    def test_review_counts_and_empty(self):
        tokens = A.rollup([(A.REVIEW, 0, False), (A.UNREAD, 2, False)], GLYPHS)
        self.assertEqual(tokens, {"fws": "2 done", "fwswatch": "@ 2"})
        self.assertEqual(A.rollup([(A.IDLE, 0, False)], GLYPHS), {"fws": None, "fwswatch": None})
        self.assertEqual(A.rollup([(A.NEEDS, 0, False)] * 2, GLYPHS)["fws"], "2 need you")


class ElapsedTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(A.elapsed(30), "")
        self.assertEqual(A.elapsed(90), "1m")
        self.assertEqual(A.elapsed(3 * 3600), "3h")
        self.assertEqual(A.elapsed(3 * 86400), "3d")


if __name__ == "__main__":
    unittest.main()
