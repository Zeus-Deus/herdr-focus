"""Manage the plugin-owned block in Herdr's config.toml: sidebar rows and key bindings.

Only text between our markers is ever written or removed. A backup is taken before
every change, and nothing is written if the user already defines the same tables.
"""

import os
import re
import shutil
import time
import tomllib

BEGIN = "# >>> herdr-focus (managed block: `python3 -m focus uninstall-config` removes it) >>>"
END = "# <<< herdr-focus <<<"

RED, GREEN, AMBER, CYAN = "#f16769", "#52c585", "#e9b452", "#6bcaca"

STATUS_RULES = (
    '[{ starts_with = "Needs", fg = "%(red)s", bold = true },'
    ' { contains = "need you", fg = "%(red)s", bold = true },'
    ' { starts_with = "Done", fg = "%(green)s", bold = true },'
    ' { contains = " done", fg = "%(green)s", bold = true },'
    ' { starts_with = "Unread", fg = "%(green)s", bold = true },'
    ' { starts_with = "Woke", fg = "%(green)s", bold = true },'
    ' { contains = "orking", fg = "%(amber)s" },'
    ' { contains = "atch", fg = "%(cyan)s" },'
    ' { starts_with = "Idle", dim = true },'
    ' { starts_with = "Settled", dim = true },'
    ' { starts_with = "Snoozed", dim = true },'
    ' { starts_with = "Unknown", dim = true }]'
) % {"red": RED, "green": GREEN, "amber": AMBER, "cyan": CYAN}

SIDEBAR = """[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", { token = "$ft_hot", bold = true }, "$ft", { token = "$ft_quiet", dim = true }],
  [{ token = "$fstatus", rules = %(rules)s }, { token = "$fwatch", fg = "%(cyan)s" }, { token = "workspace", dim = true }],
]

[ui.sidebar.spaces]
row_gap = 0
rows = [
  ["state_icon", "workspace", { token = "$fws", rules = %(rules)s }, { token = "$fwswatch", fg = "%(cyan)s" }],
  [{ token = "branch", dim = true }, "git_status"],
]
""" % {"rules": STATUS_RULES, "cyan": CYAN}

KEYS = [
    ("prefix+a", "focus.menu", "focus: attention menu"),
    ("prefix+u", "focus.unread", "focus: mark unread / read"),
    ("prefix+shift+s", "focus.settle", "focus: settle / unsettle"),
    ("prefix+i", "focus.next", "focus: next agent that needs you"),
    ("prefix+shift+u", "focus.undo", "focus: undo last triage"),
]

DEFAULT_PREFIX_KEYS = {
    "prefix+?", "prefix+[", "prefix+b", "prefix+c", "prefix+e", "prefix+g", "prefix+h",
    "prefix+j", "prefix+k", "prefix+l", "prefix+minus", "prefix+n", "prefix+o", "prefix+p",
    "prefix+q", "prefix+r", "prefix+s", "prefix+tab", "prefix+v", "prefix+w", "prefix+x",
    "prefix+z",
}


def herdr_config_path():
    path = os.environ.get("HERDR_CONFIG_PATH")
    if path:
        return path
    return os.path.join(os.path.expanduser("~"), ".config", "herdr", "config.toml")


def _read(path):
    with open(path) as handle:
        return handle.read()


def strip_block(text):
    pattern = re.compile(r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    return pattern.sub("\n", text).rstrip("\n") + "\n" if BEGIN in text else text


def _bound_keys(data):
    keys = set()

    def collect(value):
        if isinstance(value, str):
            keys.add(value.lower())
        elif isinstance(value, list):
            for item in value:
                collect(item)

    section = data.get("keys") or {}
    for name, value in section.items():
        if name == "command":
            for entry in value if isinstance(value, list) else []:
                collect(entry.get("key"))
        else:
            collect(value)
    return keys


def plan(text):
    """Return (block, notes) for the current config text, or raise ValueError on conflict."""
    base = tomllib.loads(strip_block(text))
    sidebar = ((base.get("ui") or {}).get("sidebar") or {})
    notes = []
    parts = []
    if "agents" in sidebar or "spaces" in sidebar:
        notes.append("left your own [ui.sidebar.agents]/[ui.sidebar.spaces] in place; "
                     "see docs/sidebar.toml to add the focus tokens by hand")
    else:
        parts.append(SIDEBAR)
    bound = _bound_keys(base) | DEFAULT_PREFIX_KEYS
    for key, command, description in KEYS:
        if key in bound:
            notes.append("skipped %s for %s: already bound" % (key, command))
            continue
        parts.append('[[keys.command]]\nkey = "%s"\ntype = "plugin_action"\ncommand = "%s"\n'
                     'description = "%s"\n' % (key, command, description))
    if not parts:
        return None, notes
    block = BEGIN + "\n" + "\n".join(parts) + END + "\n"
    return block, notes


def install(path=None):
    path = path or herdr_config_path()
    text = _read(path) if os.path.exists(path) else ""
    block, notes = plan(text)
    new = strip_block(text)
    if block:
        new = new.rstrip("\n") + "\n\n" + block
    tomllib.loads(new)  # never write a config Herdr cannot parse
    if new == text:
        return path, None, notes
    backup = None
    if text:
        backup = "%s.bak.focus-%d" % (path, int(time.time()))
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".focus-tmp"
    with open(tmp, "w") as handle:
        handle.write(new)
    os.replace(tmp, path)
    return path, backup, notes


def uninstall(path=None):
    path = path or herdr_config_path()
    if not os.path.exists(path):
        return path, False
    text = _read(path)
    if BEGIN not in text:
        return path, False
    new = strip_block(text)
    tomllib.loads(new)
    shutil.copy2(path, "%s.bak.focus-%d" % (path, int(time.time())))
    with open(path + ".focus-tmp", "w") as handle:
        handle.write(new)
    os.replace(path + ".focus-tmp", path)
    return path, True
