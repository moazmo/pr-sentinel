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
from .models import PRMetadata
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


# Author associations allowed to trigger comment commands (V2 B2). Drive-by
# commenters must not be able to spend the repo owner's API budget.
_TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
_COMMAND_PREFIX = "@pr-sentinel"


def parse_command(comment_body: str) -> tuple[str, str] | None:
    """Parse `@pr-sentinel <review|describe|ask ...>` from a comment body.
    Returns (command, argument) or None when the comment is not for us."""
    stripped = comment_body.strip()
    if not stripped.lower().startswith(_COMMAND_PREFIX):
        return None
    rest = stripped[len(_COMMAND_PREFIX):].strip()
    if not rest:
        return None
    word, _, argument = rest.partition(" ")
    word = word.lower()
    if word in ("review", "describe"):
        return word, ""
    if word == "ask" and argument.strip():
        return "ask", argument.strip()
    return None


def _command_from_event(event: dict) -> tuple[str, str, int] | None:
    """Validate an issue_comment event: PR comment, trusted author, known
    command. Returns (command, argument, pr_number) or None."""
    comment = event.get("comment") or {}
    issue = event.get("issue") or {}
    if "pull_request" not in issue:
        return None
    if event.get("action") not in (None, "created"):
        return None
    association = str(comment.get("author_association") or "").upper()
    if association not in _TRUSTED_ASSOCIATIONS:
        logger.info("Ignoring command from untrusted association %r.", association)
        return None
    parsed = parse_command(str(comment.get("body") or ""))
    if parsed is None:
        return None
    number = int(issue.get("number") or 0)
    if number <= 0:
        return None
    return parsed[0], parsed[1], number


async def _pr_from_api(github: GitHubClient, repo: str, number: int) -> PRMetadata:
    """issue_comment payloads carry no PR details; fetch them."""
    data = await github.get_pr(number)
    return pr_metadata_from_event({"pull_request": data}, repo)


async def _run() -> int:
    event = _read_event()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if event is None or not repo:
        return 0  # not a usable Actions context; nothing to do, nothing to break

    command: tuple[str, str, int] | None = None
    if "pull_request" in event:
        pr = pr_metadata_from_event(event, repo)
    elif "comment" in event:
        command = _command_from_event(event)
        if command is None:
            logger.info("Comment is not an actionable PR Sentinel command; skipping.")
            return 0
        pr = None  # fetched below, once we have a client
    else:
        logger.info("Event is neither a pull_request nor a PR comment; skipping.")
        return 0

    token = _resolve_github_token()
    if not token:
        logger.error("No GitHub token available; cannot read the PR or post a comment.")
        return 0

    github = GitHubClient(token, repo)

    if command is not None:
        try:
            pr = await _pr_from_api(github, repo, command[2])
        except GitHubError:
            logger.error("Could not fetch PR #%d for the command.", command[2])
            return 0
    assert pr is not None
    if pr.number <= 0:
        logger.error("Could not determine PR number from the event payload.")
        return 0

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

    if config.provider.kind == "anthropic":
        from .provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key,
            base_url=config.provider.base_url,
            model=config.provider.model,
            max_concurrent=config.limits.max_concurrent_requests,
            timeout_seconds=config.limits.agent_timeout_seconds,
        )
    else:
        provider = OpenAICompatProvider(
            api_key,
            base_url=config.provider.base_url,
            model=config.provider.model,
            max_concurrent=config.limits.max_concurrent_requests,
            timeout_seconds=config.limits.agent_timeout_seconds,
        )

    try:
        if command is not None and command[0] == "ask":
            await _handle_ask(provider, github, pr, config, command[1], [api_key, token])
        elif command is not None and command[0] == "describe":
            await _handle_describe(provider, github, pr, config, [api_key, token])
        else:  # pull_request event, or `@pr-sentinel review`
            graph = build_graph(provider, github, known_secrets=[api_key, token])
            try:
                result = await graph.ainvoke({"config": config, "pr": pr})
                logger.info(
                    "Review complete: %d finding(s) posted to PR #%d.",
                    len(result.get("merged_findings", [])),
                    pr.number,
                )
            except Exception as exc:  # noqa: BLE001 — must degrade, not crash CI
                reason = scrub_secrets(f"{type(exc).__name__}", [api_key, token])
                logger.error("Review failed: %s", reason)
                try:
                    await github.upsert_comment(pr.number, format_failure(reason))
                except GitHubError:
                    logger.error("Could not post the failure comment either; see logs above.")
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()
    return 0


async def _ingest_for_command(github: GitHubClient, pr, config):
    """Files -> skip rules -> chunks + PR map, for single-call commands."""
    from .chunking import apply_skip_rules, build_chunks, build_pr_map

    files = apply_skip_rules(await github.list_pr_files(pr.number), config)
    return build_chunks(files, config), build_pr_map(pr.title, files)


async def _handle_ask(provider, github, pr, config, question: str, secrets: list[str]) -> None:
    from .agents import run_ask

    chunks, pr_map = await _ingest_for_command(github, pr, config)
    answer, _ = await run_ask(provider, pr_map, chunks, question, config)
    if answer is None:
        answer = "Sorry — the question could not be answered (provider error). See the Action logs."
    body = f"### 🛡️ PR Sentinel — answer\n\n{scrub_secrets(answer, secrets)}"
    await github.post_comment(pr.number, body)


async def _handle_describe(provider, github, pr, config, secrets: list[str]) -> None:
    from .agents import run_describe
    from .formatter import format_description

    chunks, pr_map = await _ingest_for_command(github, pr, config)
    description, _ = await run_describe(provider, pr_map, chunks, config)
    if description is None:
        logger.error("Describe could not generate a description.")
        return
    await github.update_pr_description(
        pr.number, scrub_secrets(format_description(description), secrets)
    )


def cli() -> None:
    _setup_logging()
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    cli()
