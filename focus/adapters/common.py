"""Shared helpers for adapters: incremental JSONL tails and read-only SQLite lookups."""

import json
import os
import sqlite3
import urllib.parse


def query(path, sql, args=()):
    """Run one read-only query; any SQLite problem (locked, missing, schema change) -> []."""
    if not path or not os.path.exists(path):
        return []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True, timeout=1)
    except sqlite3.Error:
        return []
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()


def text_of(content):
    """Plain text from a string or a list of {type: text|input_text, text} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") in ("text", "input_text"))
    return ""


def first_texts(texts, limit_chars, max_messages):
    out, total = [], 0
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        out.append(text[: limit_chars - total])
        total += len(out[-1])
        if total >= limit_chars or len(out) >= max_messages:
            break
    return out


def jsonl(path):
    """Yield records of a JSONL file, skipping lines that don't parse."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


class Tail:
    """Reads a growing JSONL file from where it left off; starts over if it shrinks or is replaced."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.inode = None
        self._partial = b""

    def read(self):
        """Returns (records, reset, mtime). reset means earlier records no longer apply."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return [], True, None
        reset = False
        if self.inode != stat.st_ino or stat.st_size < self.offset:
            reset = self.inode is not None
            self.inode, self.offset, self._partial = stat.st_ino, 0, b""
        if stat.st_size == self.offset:
            return [], reset, stat.st_mtime
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = self._partial + handle.read()
            self.offset = handle.tell()
        lines = data.split(b"\n")
        self._partial = lines.pop()
        records = []
        for raw in lines:
            if raw.strip():
                try:
                    records.append(json.loads(raw))
                except ValueError:
                    continue
        return records, reset, stat.st_mtime


def timestamp(record):
    stamp = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
