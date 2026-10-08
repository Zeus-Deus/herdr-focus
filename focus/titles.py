"""Short natural titles: a local fallback right away, an optional model title in the background.

Ownership rules: a manual title always wins until it is reset, and a model result is
dropped if the record's title generation moved on while the request was in flight.
"""

import collections
import hashlib
import json
import queue
import re
import threading
import time

PROMPT = """Generate a title that will help the user recognize this coding-agent conversation weeks later.
First, silently reduce the request to its subject and intended outcome. Discard incidental instructions such as which tools or models to use, testing, monitoring or formatting.
Rules:
- 3-7 words, fewer than {max_chars} characters.
- A compact noun phrase or a clear action phrase.
- Do not claim the work is complete.
- Do not copy and truncate the user's message.
- No project names, quotes, labels, filler or trailing punctuation.
Reply with the title only.

User message:
{context}
"""

_SECRET = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer|token|password|passwd|secret|api[_-]?key)\b\s*[:=]?\s*\S{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
]

_GENERIC = re.compile(
    r"^(claude( code)?|codex|hermes( agent)?|pi|gemini|opencode|oc|amp|droid|cursor|copilot|bash|zsh|fish|sh|"
    r"nvim|vim|python3?|node|herdr|new (thread|chat|session)|untitled)$", re.I)
_PROMPTY = re.compile(
    r"^(hey|hi|hello|ok(ay)?|so|please|pls|can you|could you|would you|will you|i want you to|"
    r"i need you to|i'd like you to|let'?s|go ahead and|now)\b[\s,:]*", re.I)


_DANGLING = {"a", "an", "and", "the", "to", "of", "for", "with", "in", "on", "from", "that",
             "this", "my", "our", "your", "by", "at", "or", "so", "it", "when", "right", "after"}


def redact(text):
    for pattern in _SECRET:
        text = pattern.sub("[redacted]", text)
    return text


def context_from(messages, limit):
    joined = "\n\n".join(m.strip() for m in messages if m and m.strip())
    return redact(joined)[:limit]


def input_hash(context):
    return hashlib.sha1(context.encode()).hexdigest()[:16]


def clip(text, max_chars):
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:-–—")
    return cut or text[:max_chars]


def sanitize(raw, max_chars):
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("title", ""))
        except (ValueError, AttributeError):
            pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = lines[0] if lines else ""
    text = re.sub(r"^(\*\*)?title(\*\*)?\s*:\s*", "", text, flags=re.I)
    text = text.strip("'\"`*_#> ").strip()
    text = re.sub(r"\s+", " ", text).rstrip(".!?:;,")
    if not text or _GENERIC.match(text):
        return None
    return clip(text, max_chars)


def _meaningful_terminal_title(title):
    if not title:
        return None
    # OpenCode: "OC | <title>"; Pi: "π - <name> - <dir>"; Codex: "<title> | <project>".
    if title.startswith("OC | "):
        title = title[5:]
    elif title.startswith("\u03c0 - "):
        parts = title.split(" - ")
        title = parts[1] if len(parts) >= 3 else ""
    title = title.split(" | ")[0].strip()
    # Shell prompts (user@host:~/dir), bare paths and agent names say nothing about the work.
    if re.match(r"^[\w.-]+@[\w.-]+[:\s]", title) or title.startswith(("/", "~")):
        return None
    if _GENERIC.match(title) or len(title) < 3:
        return None
    return title


def heuristic(message, max_chars):
    """Turn a first prompt into a readable fallback without a model."""
    line = next((l.strip() for l in message.splitlines() if l.strip()), "")
    line = re.sub(r"https?://\S+", "", line)
    for _ in range(3):
        line = _PROMPTY.sub("", line).strip()
    words = line.split()[:6]
    while words and words[-1].lower().strip(",.;:") in _DANGLING:
        words.pop()
    if not words:
        return None
    text = " ".join(words).rstrip(".!?:;,")
    text = text[0].upper() + text[1:]
    return clip(text, max_chars)


def fallback(pane, messages, max_chars):
    title = _meaningful_terminal_title(pane.get("terminal_title_stripped"))
    if title:
        return clip(title, max_chars)
    for message in messages:
        title = heuristic(redact(message), max_chars)
        if title:
            return title
    agent = pane.get("display_agent") or pane.get("agent") or "agent"
    return agent


class Worker:
    """One background thread, a bounded queue and a per-minute budget. Never blocks the daemon."""

    def __init__(self, provider, max_chars, max_per_minute, notify):
        self.provider = provider
        self.max_chars = max_chars
        self.max_per_minute = max(1, int(max_per_minute))
        self.notify = notify
        self.jobs = queue.Queue(maxsize=16)
        self.results = queue.Queue()
        self.inflight = set()
        self.recent = collections.deque()
        self.thread = threading.Thread(target=self._run, name="focus-titles", daemon=True)
        self.thread.start()

    def submit(self, key, generation, digest, context):
        if key in self.inflight:
            return False
        try:
            self.jobs.put_nowait((key, generation, digest, context))
        except queue.Full:
            return False
        self.inflight.add(key)
        return True

    def _run(self):
        while True:
            key, generation, digest, context = self.jobs.get()
            while len(self.recent) >= self.max_per_minute:
                wait = 60 - (time.time() - self.recent[0])
                if wait > 0:
                    time.sleep(wait)
                self.recent.popleft()
            self.recent.append(time.time())
            title, error = None, None
            try:
                raw = self.provider.generate(PROMPT.format(max_chars=self.max_chars, context=context))
                title = sanitize(raw, self.max_chars)
                if not title:
                    error = "empty title"
            except Exception as exc:  # any backend failure keeps the fallback title
                error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            self.results.put((key, generation, digest, title, error))
            self.notify()

    def drain(self):
        out = []
        while True:
            try:
                item = self.results.get_nowait()
            except queue.Empty:
                return out
            self.inflight.discard(item[0])
            out.append(item)


def env_summary(provider):
    if provider is None:
        return "local fallback titles (nothing leaves this machine)"
    return "%s backend: %s" % (provider.name, provider.describe())

