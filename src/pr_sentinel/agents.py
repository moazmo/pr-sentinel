"""Analyst and Reviewer agent runtime.

Prompts live as readable .md files in prompts/ (the transparency wedge).
PR-controlled text reaches the model only inside delimited data blocks in the
user message; analyst output is accepted only if it parses against the Finding
schema — both are injection defenses, not style choices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from importlib import resources

from .config import SentinelConfig
from .models import AgentError, AgentName, Finding, Severity, UsageStats
from .provider import LLMProvider, ProviderError

logger = logging.getLogger(__name__)

ANALYST_TEMPERATURE = 0.1
REVIEWER_TEMPERATURE = 0.2

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def load_prompt(name: str) -> str:
    base = resources.files("pr_sentinel.prompts")
    return (base / f"{name}.md").read_text(encoding="utf-8")


def analyst_system_prompt(agent: AgentName, language_hint: str = "") -> str:
    prompt = load_prompt(agent.value) + "\n\n" + load_prompt("_shared_rules")
    if language_hint:
        # The hint comes from the BASE-branch config, not the PR — safe to append.
        prompt += f"\n\nThe repository's primary language is {language_hint}."
    return prompt


def parse_findings(raw: str, agent: AgentName) -> list[Finding]:
    """Parse an analyst's response into validated Findings.

    This is the structured-output security boundary: anything that is not a
    valid finding object is dropped, never propagated. A model reply of `[]`
    (clean code) is success, not failure.
    """
    match = _JSON_ARRAY.search(raw)
    if match is None:
        if raw.strip() in ("[]", ""):
            return []
        logger.warning("%s agent returned non-JSON output; discarded.", agent.value)
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("%s agent returned unparseable JSON; discarded.", agent.value)
        return []
    if not isinstance(items, list):
        return []

    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item["agent"] = agent.value
        item.setdefault("line_start", 0)
        item.setdefault("line_end", item["line_start"])
        item.pop("also_flagged_by", None)  # analysts may not assign credit
        try:
            findings.append(Finding(**item))
        except Exception:
            logger.warning("Dropped malformed finding from %s agent.", agent.value)
    return findings


async def run_analyst(
    agent: AgentName,
    provider: LLMProvider,
    pr_map: str,
    chunks: list,
    config: SentinelConfig,
) -> tuple[list[Finding], UsageStats, AgentError | None]:
    """Run one analyst over every chunk. Per-chunk failures degrade to a
    partial result; only a fully-failed agent is reported as an error (D3)."""
    system = analyst_system_prompt(agent, config.review.language_hint)
    usage = UsageStats()
    findings: list[Finding] = []
    failures = 0

    async def review_chunk(chunk) -> list[Finding]:
        nonlocal failures
        user = f"{pr_map}\n\n<diff>\n{chunk.text}\n</diff>"
        try:
            result = await asyncio.wait_for(
                provider.complete(
                    system,
                    user,
                    max_tokens=config.limits.max_output_tokens_per_agent,
                    temperature=ANALYST_TEMPERATURE,
                ),
                timeout=config.limits.agent_timeout_seconds,
            )
        except (ProviderError, asyncio.TimeoutError) as exc:
            failures += 1
            logger.warning("%s agent chunk failed: %s", agent.value, exc)
            return []
        usage.add(agent.value, result.prompt_tokens, result.completion_tokens)
        return parse_findings(result.text, agent)

    results = await asyncio.gather(*(review_chunk(c) for c in chunks))
    for chunk_findings in results:
        findings.extend(chunk_findings)

    error: AgentError | None = None
    if chunks and failures == len(chunks):
        error = AgentError(agent=agent.value, message="all calls failed (provider error/timeout)")
    return findings, usage, error


async def run_reviewer(
    provider: LLMProvider,
    pr_map: str,
    clusters: list[list[Finding]],
    config: SentinelConfig,
) -> tuple[str, list[Finding], UsageStats, AgentError | None]:
    """The aggregator LLM pass (D5 stage 2). Returns (verdict, findings, usage, error).

    On failure we fall back to the deterministic merge output — a partial
    review beats no review (NFR1)."""
    system = load_prompt("reviewer")
    payload = {
        "pr": pr_map,
        "clusters": [
            [f.model_dump(mode="json", exclude={"also_flagged_by"}) for f in cluster]
            for cluster in clusters
        ],
    }
    user = "<findings_input>\n" + json.dumps(payload, indent=1) + "\n</findings_input>"
    usage = UsageStats()
    try:
        result = await asyncio.wait_for(
            provider.complete(
                system,
                user,
                max_tokens=config.limits.max_output_tokens_per_agent * 2,
                temperature=REVIEWER_TEMPERATURE,
            ),
            timeout=config.limits.agent_timeout_seconds,
        )
    except (ProviderError, asyncio.TimeoutError) as exc:
        logger.warning("Reviewer agent failed: %s", exc)
        return (
            "",
            [f for cluster in clusters for f in cluster],
            usage,
            AgentError(agent="reviewer", message=str(exc)),
        )
    usage.add("reviewer", result.prompt_tokens, result.completion_tokens)

    verdict, findings = _parse_reviewer_output(result.text)
    if findings is None:  # unparseable -> deterministic fallback
        return (
            "",
            [f for cluster in clusters for f in cluster],
            usage,
            AgentError(agent="reviewer", message="output did not match the expected schema"),
        )
    return verdict, findings, usage, None


def _parse_reviewer_output(raw: str) -> tuple[str, list[Finding] | None]:
    match = _JSON_OBJECT.search(raw)
    if match is None:
        return "", None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", None
    if not isinstance(data, dict):
        return "", None

    verdict = str(data.get("verdict") or "")
    findings: list[Finding] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item.setdefault("agent", AgentName.REVIEWER.value)
        item.setdefault("line_start", 0)
        item.setdefault("line_end", item["line_start"])
        if item.get("suggestion") in ("null", ""):
            item["suggestion"] = None
        try:
            findings.append(Finding(**item))
        except Exception:
            logger.warning("Dropped malformed finding from reviewer output.")
    return verdict, findings


def severity_at_least(finding: Finding, threshold: Severity) -> bool:
    return finding.severity.rank <= threshold.rank
