"""GitHub Action entrypoint.

The one rule that dominates this file (NFR1): PR Sentinel must NEVER break the
user's CI. Every failure path degrades to "post a short comment if possible,
log the reason, exit 0". The only secrets handled here are read from env and
passed straight into client constructors — they never reach prompts, state, or
logs (logging is scrubbed as a last line of defense).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from .config import CONFIG_FILENAME, SentinelConfig, load_config
from .formatter import format_failure
from .github_client import GitHubClient, GitHubError, pr_metadata_from_event
from .graph import build_graph
from .provider import OpenAICompatProvider
from .security import scrub_secrets

logger = logging.getLogger("pr_sentinel")


class _ScrubbingFormatter(logging.Formatter):
    """Last line of defense: no log record can carry a key-shaped string."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_secrets(super().format(record))


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_ScrubbingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _read_event() -> dict | None:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.exists(path):
        logger.error("GITHUB_EVENT_PATH missing — not running inside GitHub Actions?")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read event payload: %s", type(exc).__name__)
        return None


def _resolve_api_key(config: SentinelConfig) -> str:
    """Action input first, then the env var named in config."""
    return (
        os.environ.get("INPUT_API_KEY", "")
        or os.environ.get(config.provider.api_key_env, "")
    ).strip()


def _resolve_github_token() -> str:
    return (
        os.environ.get("INPUT_GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    ).strip()


async def run() -> int:
    """Outer wrapper enforcing NFR1: no exception may produce a non-zero exit."""
    try:
        return await _run()
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected failure: %s", type(exc).__name__)
        return 0


async def _run() -> int:
    event = _read_event()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if event is None or not repo:
        return 0  # not a usable Actions context; nothing to do, nothing to break

    if "pull_request" not in event:
        logger.info("Event is not a pull_request; skipping.")
        return 0

    pr = pr_metadata_from_event(event, repo)
    if pr.number <= 0:
        logger.error("Could not determine PR number from the event payload.")
        return 0

    token = _resolve_github_token()
    if not token:
        logger.error("No GitHub token available; cannot read the PR or post a comment.")
        return 0

    github = GitHubClient(token, repo)

    # Config comes from the BASE branch — a hostile PR must not be able to
    # rewrite the rules that review it (threat model, Threat 2.6).
    raw_config = None
    try:
        raw_config = await github.get_file_from_ref(CONFIG_FILENAME, pr.base_ref or pr.base_sha)
    except GitHubError:
        logger.warning("Could not fetch %s from base branch; using defaults.", CONFIG_FILENAME)
    config = load_config(raw_config)

    api_key = _resolve_api_key(config)
    if not api_key:
        # Fork PRs don't get secrets under the `pull_request` trigger — by
        # GitHub design. Skipping is correct behavior, not a failure (SECURITY.md).
        logger.info(
            "No API key available (fork PR? secrets are unavailable to fork PRs "
            "by GitHub design — see SECURITY.md). Skipping review."
        )
        return 0

    provider = OpenAICompatProvider(
        api_key,
        base_url=config.provider.base_url,
        model=config.provider.model,
        max_concurrent=config.limits.max_concurrent_requests,
        timeout_seconds=config.limits.agent_timeout_seconds,
    )

    graph = build_graph(provider, github, known_secrets=[api_key, token])
    try:
        result = await graph.ainvoke({"config": config, "pr": pr})
        logger.info(
            "Review complete: %d finding(s) posted to PR #%d.",
            len(result.get("merged_findings", [])),
            pr.number,
        )
    except Exception as exc:  # noqa: BLE001 — anything here must degrade, not crash CI
        reason = scrub_secrets(f"{type(exc).__name__}", [api_key, token])
        logger.error("Review failed: %s", reason)
        try:
            await github.upsert_comment(pr.number, format_failure(reason))
        except GitHubError:
            logger.error("Could not post the failure comment either; see logs above.")
    return 0


def cli() -> None:
    _setup_logging()
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    cli()
