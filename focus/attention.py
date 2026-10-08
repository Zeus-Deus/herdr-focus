"""Pure attention rules: runtime facts + human triage -> what a sidebar row should say.

Native status always keeps its meaning. Monitoring, unread and parked state are layered
next to it, never written back as a fake semantic status.
"""

NEEDS, WORKING, REVIEW, UNREAD, MONITORING, UNKNOWN, IDLE = (
    "needs", "working", "review", "unread", "monitoring", "unknown", "idle")

# Rows that should pop out of the list.
HOT = {NEEDS, REVIEW, UNREAD}
# Rows that recede while they get on with it (CodeMux: working / monitoring / no status).
QUIET = {WORKING, MONITORING, UNKNOWN}

ATTENTION_RANK = {NEEDS: "1", REVIEW: "2", UNREAD: "2", IDLE: "3", UNKNOWN: "3",
                  MONITORING: "4", WORKING: "5"}


def is_snoozed(record, now):
    until = record.get("snoozed_until")
    return bool(until and until > now)


def is_parked(record, now):
    return bool(record.get("settled")) or is_snoozed(record, now)


def category(status, record, watches):
    """status: Herdr's effective status (idle/working/blocked/done/unknown)."""
    if status == "blocked":
        return NEEDS
    if status == "working":
        return WORKING
    if status == "done":
        return REVIEW
    if record.get("unread"):
        # A finished turn nobody has looked at yet reads like Herdr's own "done".
        return REVIEW if record.get("unread_reason") == "finished" else UNREAD
    if watches or record.get("manual_watch"):
        return MONITORING
    if status == "unknown":
        return UNKNOWN
    return IDLE


def elapsed(seconds):
    if seconds is None or seconds < 60:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%dm" % minutes
    hours = minutes // 60
    if hours < 48:
        return "%dh" % hours
    return "%dd" % (hours // 24)


def _with_time(label, since, now, show):
    if not show or not since:
        return label
    text = elapsed(now - since)
    return "%s %s" % (label, text) if text else label


def status_label(cat, record, watches, now, glyphs, show_elapsed=True):
    since = record.get("status_since")
    if cat == NEEDS:
        return _with_time("Needs you", since, now, show_elapsed)
    if cat == WORKING:
        return _with_time("Working", since, now, show_elapsed)
    # Parked rows say so, whatever else is true of them (visible with "show settled").
    if is_snoozed(record, now):
        left = elapsed(record["snoozed_until"] - now)
        return "Snoozed %s" % left if left else "Snoozed"
    if record.get("settled"):
        return "Settled"
    if cat == REVIEW:
        return "Done"
    if cat == UNREAD:
        return "Woke" if record.get("unread_reason") == "woke" else "Unread"
    if cat == MONITORING:
        if watches:
            label = "Watching" if watches == 1 else "Watching %d" % watches
        else:
            label = "Manual watch"
        return "%s %s" % (glyphs["watch"], label)
    if cat == UNKNOWN:
        return "Unknown"
    return _with_time("Idle", since, now, show_elapsed)


def watch_badge(cat, record, watches, glyphs):
    """Eye shown beside another status, e.g. a finished result whose PR watch still runs."""
    if cat == MONITORING:
        return None
    if watches:
        return "%s %d" % (glyphs["watch"], watches)
    if record.get("manual_watch"):
        return "%s manual" % glyphs["watch"]
    return None


def title_slot(cat, record, focused, now):
    """Which of the three styled title tokens carries the title."""
    parked = is_parked(record, now)
    if cat == NEEDS or (cat in HOT and not parked):
        return "hot"
    if focused:
        # The pane you are looking at never recedes (CodeMux exempts the active row).
        return "plain"
    if cat in QUIET or parked:
        return "quiet"
    return "plain"


def rank(cat, record, layout):
    if record.get("pinned"):
        return "0"
    if layout == "attention":
        return ATTENTION_RANK.get(cat, "3")
    if layout == "shelf":
        return "2" if cat in (WORKING, MONITORING) else "1"
    return "1"


def can_park(cat):
    """Settle/snooze only finished or quiet work; live turns and open prompts stay visible."""
    return cat not in (NEEDS, WORKING)


def project(record, status, focused, watches, title, now, glyphs, layout="stable",
            show_elapsed=True):
    """Return the pane token map (None clears a token)."""
    cat = category(status, record, watches)
    slot = title_slot(cat, record, focused, now)
    text = title
    if record.get("pinned") and text:
        text = "%s %s" % (glyphs["pin"], text)
    tokens = {
        "ft_hot": text if slot == "hot" else None,
        "ft": text if slot == "plain" else None,
        "ft_quiet": text if slot == "quiet" else None,
        "fstatus": status_label(cat, record, watches, now, glyphs, show_elapsed),
        "fwatch": watch_badge(cat, record, watches, glyphs),
        "frank": rank(cat, record, layout),
        "fhide": "1" if (is_parked(record, now) and cat != NEEDS) else None,
    }
    return cat, tokens


def rollup(entries, glyphs):
    """entries: [(category, watches, manual_watch)] for one workspace -> workspace tokens."""
    needs = sum(1 for cat, _, _ in entries if cat == NEEDS)
    review = sum(1 for cat, _, _ in entries if cat in (REVIEW, UNREAD))
    working = sum(1 for cat, _, _ in entries if cat == WORKING)
    watches = sum(w for _, w, _ in entries)
    manual = any(m for _, _, m in entries)
    if needs:
        status = "Needs you" if needs == 1 else "%d need you" % needs
    elif review:
        status = "%d done" % review
    elif working:
        status = "Working" if working == 1 else "%d working" % working
    else:
        status = None
    if watches:
        watch = "%s %d" % (glyphs["watch"], watches)
    elif manual:
        watch = glyphs["watch"]
    else:
        watch = None
    return {"fws": status, "fwswatch": watch}
