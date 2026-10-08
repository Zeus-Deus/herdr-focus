"""OpenAI-compatible /chat/completions backend. The key stays in the environment."""

import json
import os
import urllib.request


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings, timeout):
        self.base_url = settings["base_url"].rstrip("/")
        self.model = settings["model"]
        self.key_env = settings.get("api_key_env") or ""
        self.timeout = timeout

    def describe(self):
        return "%s @ %s" % (self.model, self.base_url)

    def generate(self, prompt):
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.key_env) if self.key_env else None
        if self.key_env and not key:
            raise RuntimeError("environment variable %s is not set" % self.key_env)
        if key:
            headers["Authorization"] = "Bearer " + key
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 40,
            "temperature": 0.2,
        }).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]
