"""Provider adapters: where an agent's conversation lives, its own title, and background tasks.

Each adapter exposes:
    locate(session, pane) -> source or None   from Herdr's session reference (or the pane)
    title(source) -> str or None               the agent's own name for the conversation
    user_messages(source, limit_chars) -> [str] first real prompts, no injected/tool text
    SUPPORTS_TASKS, TaskReader(source)          live background-task evidence, when recorded
"""

from . import claude, codex, copilot, cursor, hermes, opencode, pi

ADAPTERS = {"claude": claude, "codex": codex, "hermes": hermes, "opencode": opencode,
            "pi": pi, "cursor": cursor, "copilot": copilot}


def for_agent(agent):
    return ADAPTERS.get(agent or "")
