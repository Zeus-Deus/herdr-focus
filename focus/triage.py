"""Human triage transitions. None of them stop processes, close panes or touch agent state."""

import time

from . import attention as A


class Refused(Exception):
    pass


def _guard_park(cat, verb):
    if not A.can_park(cat):
        raise Refused("%s refused: the agent is %s" % (
            verb, "waiting for your input" if cat == A.NEEDS else "still working"))


def mark_unread(record, cat, seq):
    record["unread"] = True
    record["unread_reason"] = "manual"
    return "Marked unread"


def mark_read(record, cat, seq):
    record["unread"] = False
    record["unread_reason"] = None
    record["woke_at"] = None
    if cat == A.REVIEW:
        # Acknowledge this finished result without asking Herdr to focus the pane.
        record["ack_seq"] = seq
    return "Marked read"


def toggle_unread(record, cat, seq):
    if cat in (A.REVIEW, A.UNREAD):
        return mark_read(record, cat, seq)
    return mark_unread(record, cat, seq)


def settle(record, cat, seq, now=None):
    _guard_park(cat, "Settle")
    record["settled"] = True
    record["settled_at"] = now or time.time()
    record["snoozed_until"] = None
    record["keep_active"] = False
    # Settling means you are done with the result.
    record["unread"] = False
    record["unread_reason"] = None
    record["woke_at"] = None
    if cat == A.REVIEW:
        record["ack_seq"] = seq
    return "Settled"


def unsettle(record, cat, seq):
    record["settled"] = False
    record["snoozed_until"] = None
    record["keep_active"] = True
    return "Back in the list"


def toggle_settle(record, cat, seq, now=None):
    if record.get("settled") or A.is_snoozed(record, now or time.time()):
        return unsettle(record, cat, seq)
    return settle(record, cat, seq, now)


def snooze(record, cat, seq, until):
    _guard_park(cat, "Snooze")
    record["snoozed_until"] = until
    record["settled"] = False
    record["keep_active"] = False
    return "Snoozed"


def wake(record, cat, seq, reason="user", now=None):
    record["snoozed_until"] = None
    if reason == "user":
        record["keep_active"] = True
    elif reason == "timer":
        record["unread"] = True
        record["unread_reason"] = "woke"
        record["woke_at"] = now or time.time()
    return "Awake"


def toggle_pin(record, cat, seq):
    record["pinned"] = not record.get("pinned")
    return "Pinned" if record["pinned"] else "Unpinned"


def toggle_watch(record, cat, seq):
    record["manual_watch"] = not record.get("manual_watch")
    return "Manual watch on" if record["manual_watch"] else "Manual watch off"


def note_status(record, status, previous, now, focused=False):
    """Native status changed. Real activity brings parked rows back (CodeMux 'activity').

    A turn that finishes while you look at another pane becomes a result to review.
    Herdr's own "done" is tracked per client, so the server-side flag alone is not enough.
    """
    if previous == "working" and status in ("idle", "done") and not focused:
        if not record.get("unread"):
            record["unread"] = True
            record["unread_reason"] = "finished"
    if status in ("working", "blocked") and previous != status:
        if record.get("settled") or A.is_snoozed(record, now):
            record["settled"] = False
            record["snoozed_until"] = None
        record["keep_active"] = False
        if status == "working":
            # A new turn supersedes an older unread marker.
            record["unread"] = False
            record["unread_reason"] = None
            record["woke_at"] = None


def note_focus(record):
    """You looked at the pane: a finished result counts as seen. Manual unread stays."""
    if record.get("unread") and record.get("unread_reason") == "finished":
        record["unread"] = False
        record["unread_reason"] = None
        return True
    return False


def due_wake(record, now):
    until = record.get("snoozed_until")
    return bool(until and until <= now)


def auto_settle_due(record, cat, watches, now, days):
    if not days or cat != A.IDLE or watches or record.get("manual_watch"):
        return False
    if record.get("settled") or record.get("pinned") or record.get("keep_active"):
        return False
    since = record.get("status_since")
    return bool(since and now - since > days * 86400)


def snooze_presets(now):
    """(label, epoch) presets matching CodeMux's snooze menu."""
    local = time.localtime(now)
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    evening = midnight + 18 * 3600
    if evening <= now + 1800:
        evening = None
    tomorrow = midnight + 86400 + 9 * 3600
    days_to_monday = (7 - local.tm_wday) % 7 or 7
    next_week = midnight + days_to_monday * 86400 + 9 * 3600
    presets = [("In 1 hour", now + 3600)]
    if evening:
        presets.append(("This evening (18:00)", evening))
    presets += [("Tomorrow (09:00)", tomorrow), ("Next week (Mon 09:00)", next_week)]
    return presets
