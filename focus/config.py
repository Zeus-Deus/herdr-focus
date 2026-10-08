"""Plugin configuration: defaults merged with config.toml from HERDR_PLUGIN_CONFIG_DIR."""

import copy
import os
import tomllib

DEFAULTS = {
    "sidebar": {
        # stable: pinned rows first, then Herdr's own workspace/tab/pane order.
        # attention: rows that need you rise to the top.
        # shelf: like stable, but working and monitoring rows sink to the bottom.
        "layout": "stable",
        # Own the Agents view filter/sort (hides settled and snoozed rows).
        "agent_view": True,
        # Show elapsed time next to Working / Needs you / Monitoring / Idle.
        "elapsed": True,
    },
    "glyphs": {
        # "auto" picks Nerd Font glyphs when fc-list reports a Nerd Font,
        # otherwise plain Unicode that every monospace font has.
        "watch": "auto",
        "pin": "auto",
    },
    "triage": {
        # 0 disables automatic settling. Live, blocked and unread work is never settled.
        "auto_settle_days": 0,
        # Focusing a pane acknowledges its finished result (CodeMux-style). Herdr's focus
        # event has no client identity, so any attached client's focus counts.
        # Manual "unread" is never cleared by focus.
        "ack_on_focus": True,
        "undo_depth": 20,
    },
    "titles": {
        "enabled": True,
        # none: local fallback titles only (nothing leaves the machine).
        # command: run an argv one-shot (prompt on stdin, title on stdout).
        # openai: POST to an OpenAI-compatible /chat/completions endpoint.
        "backend": "none",
        "command": ["claude", "-p", "--model", "haiku", "--tools", "",
                    "--no-session-persistence", "--strict-mcp-config"],
        "max_chars": 32,
        "context_chars": 2000,
        "timeout_seconds": 45,
        "max_per_minute": 4,
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-nano",
            "api_key_env": "OPENAI_API_KEY",
        },
    },
    "monitoring": {
        # Background commands matching these never count as watches.
        "service_patterns": [
            r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|preview)\b",
            r"\b(vite|next\s+dev|nuxt\s+dev|astro\s+dev|webpack\s+serve)\b",
            r"\bcargo\s+(run|watch|leptos\s+watch)\b",
            r"\bpython3?\s+-m\s+http\.server\b",
            r"\b(uvicorn|gunicorn|flask\s+run|rails\s+s(erver)?|php\s+-S)\b",
            r"\bdocker(-compose|\s+compose)\s+up\b",
            r"\b(tauri|electron)\s+dev\b",
        ],
        # Evidence older than this is treated as stale, even without an end record.
        "max_task_hours": 6,
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def config_path():
    root = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return os.path.join(root, "config.toml") if root else None


def load(path=None):
    """Return (config, error). A broken file keeps the defaults and reports why."""
    config = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    if not path or not os.path.exists(path):
        return config, None
    try:
        with open(path, "rb") as handle:
            _merge(config, tomllib.load(handle))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return copy.deepcopy(DEFAULTS), "%s: %s" % (path, exc)
    if config["sidebar"]["layout"] not in ("stable", "attention", "shelf"):
        config["sidebar"]["layout"] = "stable"
    return config, None
