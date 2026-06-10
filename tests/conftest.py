"""Shared fixtures. Every test runs without a live LLM or GitHub API (NFR5)."""

from __future__ import annotations

import json

import pytest

from pr_sentinel.config import SentinelConfig
from pr_sentinel.models import ChangedFile, PRMetadata
from pr_sentinel.provider import CompletionResult


class MockProvider:
    """Canned-response LLM provider. Responses are matched by a substring of
    the system prompt (each agent's prompt names the agent), so one mock can
    serve all five agents in an integration test."""

    def __init__(self, responses: dict[str, str] | None = None, default: str = "[]") -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[dict] = []

    async def complete(self, system, user, *, max_tokens, temperature=0.1):
        self.calls.append(
            {"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature}
        )
        text = self.default
        for needle, response in self.responses.items():
            if needle in system:
                text = response
                break
        return CompletionResult(text=text, prompt_tokens=100, completion_tokens=50)


class FailingProvider:
    async def complete(self, system, user, *, max_tokens, temperature=0.1):
        from pr_sentinel.provider import ProviderError

        raise ProviderError("simulated failure")


class SequenceProvider:
    """Returns scripted responses in order; repeats the last one when exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def complete(self, system, user, *, max_tokens, temperature=0.1):
        index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append({"system": system, "user": user})
        return CompletionResult(
            text=self.responses[index], prompt_tokens=100, completion_tokens=50
        )


@pytest.fixture
def config() -> SentinelConfig:
    return SentinelConfig()


@pytest.fixture
def pr() -> PRMetadata:
    return PRMetadata(repo="octo/demo", number=7, title="Add user lookup", base_ref="main")


def make_file(path: str = "app.py", patch: str | None = "@@ -1,2 +1,4 @@\n+x = 1\n") -> ChangedFile:
    return ChangedFile(path=path, status="modified", additions=2, deletions=0, patch=patch)


def finding_json(**overrides) -> str:
    base = {
        "file": "app.py",
        "line_start": 3,
        "line_end": 3,
        "severity": "high",
        "category": "sql-injection",
        "message": "Query built from user input.",
        "suggestion": "Use parameterized queries.",
    }
    base.update(overrides)
    return json.dumps([base])
