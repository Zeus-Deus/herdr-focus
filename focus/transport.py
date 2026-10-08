"""Newline-delimited JSON over Herdr's local socket."""

import itertools
import json
import os
import socket

_ids = itertools.count(1)


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def socket_path():
    path = os.environ.get("HERDR_SOCKET_PATH")
    if path:
        return path
    session = os.environ.get("HERDR_SESSION")
    root = os.path.join(os.path.expanduser("~"), ".config", "herdr")
    if session and session != "default":
        return os.path.join(root, "sessions", session, "herdr.sock")
    return os.path.join(root, "herdr.sock")


def _connect(path, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        raise
    return sock


def request(method, params=None, path=None, timeout=5.0):
    """Send one request and return its result, raising HerdrError on an error reply."""
    req_id = "focus:%d" % next(_ids)
    sock = _connect(path or socket_path(), timeout)
    try:
        payload = {"id": req_id, "method": method, "params": params or {}}
        sock.sendall((json.dumps(payload) + "\n").encode())
        with sock.makefile("rb") as reader:
            line = reader.readline()
    finally:
        sock.close()
    if not line:
        raise HerdrError("disconnected", "no reply to %s" % method)
    reply = json.loads(line)
    if "error" in reply:
        err = reply["error"] or {}
        raise HerdrError(err.get("code", "error"), err.get("message", ""))
    return reply.get("result", {})


class Subscription:
    """A long-lived events.subscribe connection, read without blocking by the daemon."""

    def __init__(self, subscriptions, path=None):
        self.sock = _connect(path or socket_path(), 5.0)
        payload = {
            "id": "focus:sub:%d" % next(_ids),
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        self._buf = b""
        ack = self._read_line_blocking()
        if ack is None or "error" in ack:
            self.close()
            err = (ack or {}).get("error") or {}
            raise HerdrError(err.get("code", "disconnected"), err.get("message", "subscribe failed"))
        self.sock.setblocking(False)

    def _read_line_blocking(self):
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def fileno(self):
        return self.sock.fileno()

    def read_events(self):
        """Drain readable data. Returns (events, alive)."""
        alive = True
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    alive = False
                    break
                self._buf += chunk
        except BlockingIOError:
            pass
        except OSError:
            alive = False
        events = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events, alive

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
