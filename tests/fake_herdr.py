"""A minimal in-process Herdr socket server for daemon tests: snapshot, events, metadata."""

import copy
import json
import os
import selectors
import socket
import threading


class FakeHerdr:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.workspaces = [{"workspace_id": "w1", "label": "shop"}]
        self.panes = {}
        self.focused = None
        self.calls = []
        self.subscribers = []
        self.enabled = True
        self.view = None
        self._start()

    # -- scene -----------------------------------------------------------------------------

    def add_agent(self, pane_id, status, terminal_id, agent="claude", session=None, **extra):
        with self.lock:
            pane = {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "tab_id": pane_id + "t",
                    "terminal_id": terminal_id, "agent": agent, "agent_status": status,
                    "state_change_seq": 1, "tokens": {}}
            if session:
                pane["agent_session"] = {"agent": agent, "kind": "id", "value": session,
                                         "source": "herdr:" + agent}
            pane.update(extra)
            self.panes[pane_id] = pane

    def set_status(self, pane_id, status):
        with self.lock:
            pane = self.panes[pane_id]
            pane["agent_status"] = status
            pane["state_change_seq"] += 1
        self.emit("pane.agent_status_changed", {"pane_id": pane_id, "agent_status": status})

    def focus(self, pane_id):
        with self.lock:
            self.focused = pane_id
        self.emit("pane.focused", {"pane_id": pane_id})

    def move(self, old, new):
        with self.lock:
            pane = self.panes.pop(old)
            pane["pane_id"] = new
            self.panes[new] = pane
        self.emit("pane.moved", {"pane": {"pane_id": new}, "previous_pane_id": old})

    def tokens(self, pane_id):
        with self.lock:
            return dict(self.panes[pane_id]["tokens"])

    def workspace_tokens(self, workspace_id):
        with self.lock:
            for ws in self.workspaces:
                if ws["workspace_id"] == workspace_id:
                    return dict(ws.get("tokens", {}))
        return {}

    def restart(self):
        """Simulate a server restart: connections drop and metadata tokens are lost."""
        self.stop()
        with self.lock:
            for pane in self.panes.values():
                pane["tokens"] = {}
            for ws in self.workspaces:
                ws["tokens"] = {}
            self.view = None
        self._start()

    # -- server ------------------------------------------------------------------------------

    def _start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(16)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        with self.lock:
            for conn, _ in self.subscribers:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            self.subscribers = []
        self.thread.join(2)
        self.server.close()

    def _serve(self):
        self.server.settimeout(0.1)
        while self.running:
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        data = b""
        try:
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    conn.close()
                    return
                data += chunk
        except OSError:
            conn.close()
            return
        request = json.loads(data.split(b"\n", 1)[0])
        method, params = request["method"], request.get("params", {})
        with self.lock:
            self.calls.append((method, copy.deepcopy(params)))
        if method == "events.subscribe":
            self._reply(conn, request, {"type": "subscription_started"})
            with self.lock:
                self.subscribers.append((conn, params["subscriptions"]))
            return
        try:
            result = self._dispatch(method, params)
            self._reply(conn, request, result)
        except KeyError as exc:
            conn.sendall((json.dumps({"id": request["id"], "error": {
                "code": "not_found", "message": str(exc)}}) + "\n").encode())
        conn.close()

    def _reply(self, conn, request, result):
        conn.sendall((json.dumps({"id": request["id"], "result": result}) + "\n").encode())

    def _dispatch(self, method, params):
        with self.lock:
            if method == "session.snapshot":
                panes = [dict(p) for p in self.panes.values()]
                agents = [dict(p) for p in self.panes.values() if p.get("agent")]
                return {"type": "session_snapshot", "snapshot": {
                    "panes": panes, "agents": agents, "workspaces": copy.deepcopy(self.workspaces),
                    "focused_pane_id": self.focused}}
            if method == "pane.report_metadata":
                tokens = self.panes[params["pane_id"]]["tokens"]
                for key, value in params["tokens"].items():
                    if value is None:
                        tokens.pop(key, None)
                    else:
                        tokens[key] = value
                return {"type": "ok"}
            if method == "workspace.report_metadata":
                for ws in self.workspaces:
                    if ws["workspace_id"] == params["workspace_id"]:
                        tokens = ws.setdefault("tokens", {})
                        for key, value in params["tokens"].items():
                            if value is None:
                                tokens.pop(key, None)
                            else:
                                tokens[key] = value
                        return {"type": "ok"}
                raise KeyError(params["workspace_id"])
            if method == "agent.view.set":
                self.view = params
                return {"type": "agent_view", "active": True, "source": params["source"]}
            if method == "agent.view.clear":
                if params.get("source") in (None, (self.view or {}).get("source")):
                    self.view = None
                return {"type": "agent_view", "active": self.view is not None}
            if method == "plugin.list":
                return {"plugins": [{"plugin_id": "focus", "enabled": self.enabled}]}
            if method == "agent.focus":
                self.focused = params["target"]
                return {"type": "ok"}
            if method in ("notification.show", "server.reload_config"):
                return {"type": "ok"}
        raise KeyError(method)

    def emit(self, name, data):
        line = (json.dumps({"event": name, "data": data}) + "\n").encode()
        with self.lock:
            for conn, subs in list(self.subscribers):
                if any(s["type"] == name and s.get("pane_id") in (None, data.get("pane_id"))
                       for s in subs):
                    try:
                        conn.sendall(line)
                    except OSError:
                        pass
