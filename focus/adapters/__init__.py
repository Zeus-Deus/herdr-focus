"""Provider adapters: where an agent's conversation lives and what it says about background tasks.

Each adapter exposes:
    transcript(session) -> path or None
    user_messages(path, limit_chars) -> [str]   first real user prompts, no system/tool text
    TaskReader(path) or None                      incremental background-task evidence
"""

from . import claude, codex, hermes

ADAPTERS = {"claude": claude, "codex": codex, "hermes": hermes}


def for_agent(agent):
    return ADAPTERS.get(agent or "")
