"""Entry points: startup hook, manifest actions, the menu popup and maintenance commands."""

import json
import os
import socket
import subprocess
import sys
import time

from . import sidebar, state, transport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Manifest action id -> daemon action.
ACTIONS = {
    "unread": "toggle-unread",
    "settle": "toggle-settle",
    "pin": "pin",
    "watch": "watch",
    "snooze": "snooze",
    "wake": "wake",
    "next": "next",
    "undo": "undo",
    "title-regenerate": "title-regenerate",
    "title-reset": "title-reset",
    "show-settled": "show-hidden",
    "settle-idle": "settle-idle",
    "reload": "reload",
}


def state_root():
    """Herdr's per-plugin state dir; outside Herdr, the same default location."""
    if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return os.environ["HERDR_PLUGIN_STATE_DIR"]
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "herdr", "plugins", os.environ.get("HERDR_PLUGIN_ID", "focus"))


def control_path():
    scope = state.scope_id(transport.socket_path())
    return os.path.join(state_root(), scope, "control.sock")


def call(payload, timeout=5.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(control_path())
    try:
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return json.loads(data or b"{}")


def running():
    try:
        return call({"op": "status"}, timeout=1.0).get("ok", False)
    except (OSError, ValueError):
        return False


def ensure_daemon(wait=4.0):
    if running():
        return True
    log_dir = os.path.join(state_root(), state.scope_id(transport.socket_path()))
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "daemon.out"), "ab") as out:
        subprocess.Popen([sys.executable, "-m", "focus", "daemon"], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if running():
            return True
        time.sleep(0.1)
    return False


def context():
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def target_pane(args):
    if "--pane" in args:
        return args[args.index("--pane") + 1]
    ctx = context()
    return ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")


def notify(title, body=None):
    params = {"title": title}
    if body:
        params["body"] = body
    try:
        transport.request("notification.show", params, timeout=2.0)
    except (OSError, transport.HerdrError):
        pass


def cmd_act(action, args):
    if not ensure_daemon():
        notify("Herdr Focus is not running", "see the plugin log")
        return 1
    payload = {"op": "act", "action": action, "pane_id": target_pane(args)}
    ctx = context()
    if action == "settle-idle":
        payload["workspace_id"] = ctx.get("workspace_id") if "--workspace" in args else None
    if action == "rename":
        payload["title"] = args[args.index("--title") + 1] if "--title" in args else ""
    reply = call(payload)
    message = reply.get("message", "")
    if not reply.get("ok"):
        notify("Focus: " + message)
        print(message, file=sys.stderr)
        return 1
    if action in ("undo", "show-hidden", "settle-idle", "reload"):
        notify("Focus: " + message)
    print(message)
    return 0


def cmd_menu_open():
    """Action: open the popup menu over the current pane."""
    ensure_daemon()
    params = {"plugin_id": os.environ.get("HERDR_PLUGIN_ID", "focus"), "entrypoint": "menu",
              "placement": "popup", "width": "84%", "height": "70%",
              "env": {"FOCUS_TARGET_PANE": target_pane([]) or ""}}
    try:
        transport.request("plugin.pane.open", params)
    except (OSError, transport.HerdrError) as exc:
        notify("Focus menu unavailable", str(exc))
        return 1
    return 0


def cmd_status():
    if not running():
        print("herdr-focus daemon: not running")
        return 1
    print(json.dumps(call({"op": "status"})["status"], indent=2, ensure_ascii=False))
    return 0


def lock_held():
    import fcntl
    path = os.path.join(os.path.dirname(control_path()), "daemon.lock")
    try:
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def cmd_stop(clear):
    if running():
        call({"op": "stop", "clear": clear}, timeout=15.0)
        # Wait until it has really exited, so a restart never races the old daemon's lock.
        deadline = time.time() + 5
        while lock_held() and time.time() < deadline:
            time.sleep(0.05)
        return 0
    if clear:
        # No daemon: clear its leftovers directly so uninstall always leaves a clean sidebar.
        from .daemon import clear_tokens

        def request(method, params):
            try:
                return transport.request(method, params)
            except (OSError, transport.HerdrError):
                return None
        clear_tokens(request, "plugin:" + os.environ.get("HERDR_PLUGIN_ID", "focus"))
    return 0


def cmd_install_config():
    path, backup, notes = sidebar.install()
    print("checked %s" % path)
    if backup:
        print("backup: %s" % backup)
    for note in notes:
        print("note: " + note)
    try:
        result = transport.request("server.reload_config", {})
        print("Herdr config reloaded: %s" % result.get("status", "ok"))
        for diagnostic in result.get("diagnostics") or []:
            print("herdr: %s" % (diagnostic.get("message") if isinstance(diagnostic, dict) else diagnostic))
    except (OSError, transport.HerdrError) as exc:
        print("reload Herdr config yourself (prefix+q in the default keys): %s" % exc)
    return 0


def cmd_uninstall_config():
    path, changed = sidebar.uninstall()
    print(("removed the herdr-focus block from %s" if changed else "no herdr-focus block in %s") % path)
    if changed:
        try:
            transport.request("server.reload_config", {})
        except (OSError, transport.HerdrError):
            pass
    return 0


USAGE = """usage: python3 -m focus COMMAND
  start                 make sure the daemon runs (startup hook)
  daemon                run the daemon in the foreground
  act ACTION [--pane ID] [--title TEXT]
  menu-open             open the popup menu (manifest action)
  menu                  the popup menu itself (manifest pane)
  status                daemon status
  stop [--clear]        stop the daemon; --clear removes its tokens and Agents view
  install-config        add the managed sidebar/key block to Herdr's config.toml
  uninstall-config      remove that block
  uninstall             stop --clear, then uninstall-config
"""


def main(argv):
    if not argv:
        print(USAGE)
        return 2
    command, args = argv[0], argv[1:]
    if command == "start":
        return 0 if ensure_daemon() else 1
    if command == "daemon":
        from . import daemon
        return daemon.main()
    if command == "act":
        return cmd_act(args[0], args[1:]) if args else 2
    if command == "action":
        # Manifest actions: HERDR_PLUGIN_ACTION_ID names which one.
        action_id = (os.environ.get("HERDR_PLUGIN_ACTION_ID") or (args[0] if args else "")).split(".")[-1]
        if action_id == "menu":
            return cmd_menu_open()
        if action_id == "restart":
            cmd_stop(False)
            return 0 if ensure_daemon() else 1
        if action_id not in ACTIONS:
            print("unknown action %r" % action_id, file=sys.stderr)
            return 2
        return cmd_act(ACTIONS[action_id], args)
    if command == "menu":
        from . import menu
        return menu.main()
    if command == "status":
        return cmd_status()
    if command == "stop":
        return cmd_stop("--clear" in args)
    if command == "install-config":
        return cmd_install_config()
    if command == "uninstall-config":
        return cmd_uninstall_config()
    if command == "uninstall":
        cmd_stop(True)
        return cmd_uninstall_config()
    print(USAGE)
    return 2
