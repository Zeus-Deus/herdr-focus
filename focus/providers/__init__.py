"""Title backends. A backend turns a prompt into one short line of text, or raises."""

from . import command, openai_compatible


def build(config):
    titles = config["titles"]
    backend = titles.get("backend", "none")
    if backend == "command":
        return command.CommandProvider(titles["command"], titles["timeout_seconds"])
    if backend == "openai":
        return openai_compatible.OpenAIProvider(titles["openai"], titles["timeout_seconds"])
    return None
