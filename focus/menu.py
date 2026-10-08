"""Popup action menu: every agent row with its triage actions, one key each.

This is the fallback for native Agent-row context menus, which Herdr 0.9.1 does not let
plugins extend. Each action names the selected row's pane and record key, so a row that
changed conversation underneath the menu is refused rather than acted on.
"""

import curses
import os
import time

from . import cli, transport

HELP = [
    ("enter", "focus"), ("u", "unread/read"), ("s", "settle"), ("z", "snooze"), ("p", "pin"),
    ("w", "watch"), ("r", "rename"), ("g", "new title"), ("t", "reset title"),
    ("n", "next"), ("U", "undo"), ("h", "collapse settled"), ("A", "settle idle"), ("q", "close"),
]

SECTION = {"needs": "NEEDS YOU", "review": "TO REVIEW", "unread": "TO REVIEW"}


class Menu:
    def __init__(self, screen):
        self.screen = screen
        self.rows = []
        self.presets = []
        self.collapsed = False
        self.index = 0
        self.message = ""
        # The pane under the popup when it was opened: that row starts selected.
        self.target = os.environ.get("FOCUS_TARGET_PANE") or cli.context().get("focused_pane_id")

    def load(self):
        reply = cli.call({"op": "rows"})
        current = self.rows[self.index]["key"] if self.rows else None
        self.rows = reply.get("rows", [])
        self.presets = reply.get("presets", [])
        self.collapsed = reply.get("collapse_settled", False)
        keys = [r["key"] for r in self.rows]
        if current in keys:
            self.index = keys.index(current)
        elif self.target:
            panes = [r["pane_id"] for r in self.rows]
            self.index = panes.index(self.target) if self.target in panes else 0
            self.target = None
        self.index = max(0, min(self.index, len(self.rows) - 1))

    def colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for pair, color in ((1, curses.COLOR_RED), (2, curses.COLOR_GREEN), (3, curses.COLOR_YELLOW),
                            (4, curses.COLOR_CYAN), (5, curses.COLOR_BLUE)):
            curses.init_pair(pair, color, bg)

    def status_attr(self, row):
        cat = row.get("category")
        if row.get("hidden"):
            return curses.A_DIM  # settled / snoozed, like the sidebar
        if cat == "needs":
            return curses.color_pair(1) | curses.A_BOLD
        if cat in ("review", "unread"):
            return curses.color_pair(2) | curses.A_BOLD
        if cat == "working":
            return curses.color_pair(3)
        if cat == "monitoring":
            return curses.color_pair(4)
        return curses.A_DIM

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        limit = (width if width is not None else w - x)
        limit = min(limit, w - x - (1 if y == h - 1 else 0))
        if limit <= 0:
            return
        try:
            self.screen.addnstr(y, x, text, limit, attr)
        except curses.error:
            pass

    def draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        needs = sum(1 for r in self.rows if r["category"] == "needs")
        review = sum(1 for r in self.rows if r["category"] in ("review", "unread"))
        self.put(0, 1, "Focus", curses.A_BOLD)
        summary = "  %d agents" % len(self.rows)
        if needs:
            summary += " · %d need you" % needs
        if review:
            summary += " · %d done" % review
        if self.collapsed:
            summary += " · settled collapsed in the sidebar"
        self.put(0, 6, summary, curses.A_DIM)
        self.put(1, 0, "─" * w, curses.A_DIM)
        top = 2
        body = h - top - 4
        start = max(0, self.index - body + 1)
        status_w = 18
        meta_w = max(10, min(28, w // 4))
        title_w = max(10, w - status_w - meta_w - 8)
        for offset, row in enumerate(self.rows[start:start + body]):
            i = start + offset
            y = top + offset
            selected = i == self.index
            base = curses.A_REVERSE if selected else 0
            if selected:
                self.put(y, 0, " " * w, base)
            marker = ">" if selected else " "
            title = row["title"] or "agent"
            if row.get("pinned"):
                title = "pinned · " + title
            title_attr = curses.A_BOLD if row["category"] in ("needs", "review", "unread") else 0
            if row["category"] in ("working", "monitoring") or row.get("hidden"):
                title_attr = curses.A_DIM
            self.put(y, 1, marker, base)
            self.put(y, 3, title, base | title_attr, title_w)
            status = row.get("status") or ""
            if row.get("watch"):
                status += "  " + row["watch"]
            status_attr = curses.A_BOLD if selected else self.status_attr(row)
            self.put(y, 4 + title_w, status, base | status_attr, status_w)
            meta = " · ".join(x for x in (row.get("workspace"), row.get("agent")) if x)
            self.put(y, 6 + title_w + status_w, meta, base | curses.A_DIM, meta_w)
        if not self.rows:
            self.put(top, 3, "No agents yet.", curses.A_DIM)
        detail = self.rows[self.index] if self.rows else None
        if detail and detail.get("tasks"):
            text = "watches: " + ", ".join("%s (%s)" % (label or tid, kind)
                                           for tid, kind, label in detail["tasks"])
            self.put(h - 4, 1, text, curses.color_pair(4))
        self.put(h - 3, 0, "─" * w, curses.A_DIM)
        x = 1
        line = h - 2
        for key, label in HELP:
            chunk = "%s %s  " % (key, label)
            if x + len(chunk) >= w:
                line += 1
                x = 1
                if line >= h:
                    break
            self.put(line, x, key, curses.A_BOLD)
            self.put(line, x + len(key) + 1, label, curses.A_DIM)
            x += len(chunk)
        if self.message:
            self.put(0, max(1, w - len(self.message) - 2), self.message, curses.color_pair(5))
        self.screen.refresh()

    def act(self, action, **extra):
        if action not in ("undo", "show-hidden", "settle-idle", "next") and not self.rows:
            return
        row = self.rows[self.index] if self.rows else {}
        payload = {"op": "act", "action": action, "pane_id": row.get("pane_id"), "key": row.get("key")}
        payload.update(extra)
        reply = cli.call(payload)
        self.message = reply.get("message", "")
        time.sleep(0.15)  # let the daemon republish before re-reading rows
        self.load()
        return reply.get("ok")

    def prompt(self, label, initial=""):
        h, w = self.screen.getmaxyx()
        curses.curs_set(1)
        text = initial
        while True:
            self.put(h - 4, 0, " " * w)
            self.put(h - 4, 1, label + text, curses.A_BOLD)
            self.screen.refresh()
            ch = self.screen.get_wch()
            if ch in ("\n", "\r", curses.KEY_ENTER):
                break
            if ch == "\x1b":
                text = None
                break
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                text = text[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                text += ch
        curses.curs_set(0)
        return text

    def snooze(self):
        h, w = self.screen.getmaxyx()
        options = "  ".join("%d %s" % (i + 1, label) for i, (label, _) in enumerate(self.presets))
        self.put(h - 4, 0, " " * w)
        self.put(h - 4, 1, "Snooze: " + options + "  (esc cancels)", curses.A_BOLD)
        self.screen.refresh()
        ch = self.screen.get_wch()
        if isinstance(ch, str) and ch.isdigit() and 0 < int(ch) <= len(self.presets):
            self.act("snooze", until=self.presets[int(ch) - 1][1])

    def focus(self):
        if not self.rows:
            return False
        try:
            transport.request("agent.focus", {"target": self.rows[self.index]["pane_id"]})
        except (OSError, transport.HerdrError) as exc:
            self.message = str(exc)
            return False
        return True

    def run(self):
        curses.curs_set(0)
        self.colors()
        self.screen.keypad(True)
        self.load()
        keys = {"u": "toggle-unread", "s": "toggle-settle", "p": "pin", "w": "watch",
                "g": "title-regenerate", "t": "title-reset", "U": "undo", "h": "show-hidden",
                "A": "settle-idle"}
        while True:
            self.draw()
            ch = self.screen.get_wch()
            if ch in ("q", "\x1b"):
                return 0
            if ch in (curses.KEY_DOWN, "j"):
                self.index = min(self.index + 1, len(self.rows) - 1)
            elif ch in (curses.KEY_UP, "k"):
                self.index = max(self.index - 1, 0)
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                if self.focus():
                    return 0
            elif ch == "n":
                row = self.rows[self.index] if self.rows else {}
                reply = cli.call({"op": "act", "action": "next", "pane_id": row.get("pane_id")})
                self.message = reply.get("message", "")
                if reply.get("ok") and reply["message"].startswith("Focused"):
                    return 0
            elif ch == "z":
                self.snooze()
            elif ch == "r" and self.rows:
                title = self.prompt("Rename: ", self.rows[self.index]["title"] or "")
                if title:
                    self.act("rename", title=title)
            elif isinstance(ch, str) and ch in keys:
                self.act(keys[ch])


def main():
    if not cli.ensure_daemon():
        print("Herdr Focus daemon is not running; see `python3 -m focus status`.")
        time.sleep(2)
        return 1
    return curses.wrapper(lambda screen: Menu(screen).run())
