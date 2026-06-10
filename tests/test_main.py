"""Entrypoint paths that must never break CI (NFR1) — including the
fork-PR graceful skip (secrets absent by GitHub design)."""

import json

import pytest

from pr_sentinel.main import run


@pytest.fixture
def actions_env(tmp_path, monkeypatch):
    """Minimal GitHub Actions environment with a pull_request event."""

    def setup(event: dict | None = None, *, api_key="k" * 20, token="t" * 20):
        event = event if event is not None else {
            "pull_request": {
                "number": 7, "title": "t", "body": "",
                "base": {"sha": "b" * 40, "ref": "main"},
                "head": {"sha": "h" * 40},
                "user": {"login": "octocat"},
            }
        }
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/demo")
        monkeypatch.delenv("INPUT_API_KEY", raising=False)
        monkeypatch.delenv("INPUT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("PR_SENTINEL_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        if api_key:
            monkeypatch.setenv("INPUT_API_KEY", api_key)
        if token:
            monkeypatch.setenv("INPUT_GITHUB_TOKEN", token)

    return setup


class TestGracefulExits:
    async def test_missing_event_path_exits_zero(self, monkeypatch):
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/demo")
        assert await run() == 0

    async def test_non_pr_event_exits_zero(self, actions_env):
        actions_env(event={"push": {"ref": "refs/heads/main"}})
        assert await run() == 0

    async def test_fork_pr_missing_key_skips_gracefully(
        self, actions_env, caplog, monkeypatch
    ):
        # On fork PRs the secret is simply absent — correct behavior is a
        # clear log line and exit 0, never an error.
        import logging

        caplog.set_level(logging.INFO)
        actions_env(api_key="")

        async def no_config(self, path, ref):
            return None

        monkeypatch.setattr(
            "pr_sentinel.github_client.GitHubClient.get_file_from_ref", no_config
        )
        assert await run() == 0
        assert any("fork" in r.message.lower() for r in caplog.records)

    async def test_missing_token_exits_zero(self, actions_env):
        actions_env(token="")
        assert await run() == 0

    async def test_malformed_event_json_exits_zero(self, tmp_path, monkeypatch):
        bad = tmp_path / "event.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(bad))
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/demo")
        assert await run() == 0

    async def test_total_github_failure_still_exits_zero(self, actions_env, monkeypatch):
        # Every GitHub call explodes; run() must still return 0 (never break CI).
        actions_env()

        async def boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr("pr_sentinel.github_client.GitHubClient._request", boom)
        assert await run() == 0
