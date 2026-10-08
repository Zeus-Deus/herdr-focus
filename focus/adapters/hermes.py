"""Hermes: no documented transcript or task events are exposed to Herdr yet.

Hermes panes keep native status, fall back to the terminal title for naming and offer the
manual Watch toggle. Nothing here guesses at background work.
"""

SUPPORTS_TASKS = False
TaskReader = None


def transcript(session):
    return None


def user_messages(path, limit_chars=2000, max_messages=3):
    return []
