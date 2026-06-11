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

# 0.0 for single-sample analysts: structured extraction, not prose — every
# tenth of temperature is measurable run-to-run variance in the eval harness.
# With self-consistency sampling (V2) the OPPOSITE applies: samples must be
# diverse for the vote to correct anything, so K>1 uses a higher temperature
# and the vote + evidence anchoring absorb the noise.
ANALYST_TEMPERATURE = 0.0
ENSEMBLE_TEMPERATURE = 0.6
REVIEWER_TEMPERATURE = 0.2
VERIFIER_TEMPERATURE = 0.0
# One re-ask when a model reply contains no parseable JSON at all. Findings
# silently lost to a formatting hiccup cost more than one extra cheap call.
PARSE_RETRIES = 1

_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _iter_json_structures(text: str):
    """Yield every top-level balanced [...] or {...} substring, in order.

    A balanced scan (counting brackets, skipping string literals) avoids the
    greedy-regex trap where a stray `[` in the model's reasoning prose makes a
    `\\[.*\\]` match span unparseable junk to the final bracket.
    """
    openers = {"[": "]", "{": "}"}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in openers:
            depth, j, in_str, esc = 0, i, False, False
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c in openers:
                    depth += 1
                elif c in ("]", "}"):
                    depth -= 1
                    if depth == 0:
                        yield text[i : j + 1]
                        break
                j += 1
            i = j + 1
        else:
            i += 1


def _coerce_to_dicts(parsed) -> list[dict] | None:
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        inner = parsed.get("findings")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if any(k in parsed for k in ("file", "message", "category")):
            return [parsed]
        return []
    return None


def _extract_finding_dicts(raw: str) -> list[dict] | None:
    """Pull a list of finding-shaped dicts out of a model reply, tolerating the
    formats real OpenAI-compatible models actually emit: a bare JSON array, a
    ```json fenced block, a `{"findings": [...]}` wrapper, a single finding
    object, or any of those buried in reasoning prose. Returns None only when
    nothing JSON-shaped parses.

    This widens *extraction*, not *acceptance*: every dict still has to validate
    against Finding downstream, so the structured-output security boundary holds.
    """
    search_spaces: list[str] = []
    fenced = _CODE_FENCE.search(raw)
    if fenced:
        search_spaces.append(fenced.group(1))
    search_spaces.append(raw)

    found_any = False
    for text in search_spaces:
        for candidate in _iter_json_structures(text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            dicts = _coerce_to_dicts(parsed)
            if dicts is not None:
                found_any = True
                if dicts:
                    return dicts  # first non-empty structure wins
    # An empty-but-valid structure (e.g. "[]") means "clean", not "no JSON".
    return [] if found_any else None


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
    items = _extract_finding_dicts(raw)
    if items is None:
        if raw.strip() in ("[]", ""):
            return []
        logger.warning("%s agent returned non-JSON output; discarded.", agent.value)
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
    """Run one analyst over every chunk, K self-consistency samples per chunk
    (V2 A3). Per-call failures degrade to a partial result; only a fully-failed
    agent is reported as an error (D3)."""
    from .merge import vote_findings  # local import avoids a module cycle

    system = analyst_system_prompt(agent, config.review.language_hint)
    usage = UsageStats()
    samples_per_chunk = config.accuracy.samples
    temperature = ANALYST_TEMPERATURE if samples_per_chunk == 1 else ENSEMBLE_TEMPERATURE
    failures = 0
    total_calls = 0

    async def one_sample(chunk) -> list[Finding]:
        nonlocal failures
        user = f"{pr_map}\n\n<diff>\n{chunk.text}\n</diff>"
        for attempt in range(1 + PARSE_RETRIES):
            try:
                result = await asyncio.wait_for(
                    provider.complete(
                        system,
                        user,
                        max_tokens=config.limits.max_output_tokens_per_agent,
                        temperature=temperature,
                        model=config.provider.resolved_analyst_model,
                        json_mode=True,
                    ),
                    timeout=config.limits.agent_timeout_seconds,
                )
            except (ProviderError, asyncio.TimeoutError) as exc:
                failures += 1
                logger.warning("%s agent call failed: %s", agent.value, exc)
                return []
            usage.add(
                agent.value, result.prompt_tokens, result.completion_tokens,
                cached=result.cached_tokens,
            )
            # Distinguish "reply contained no JSON at all" (worth one re-ask —
            # findings are being lost to a formatting hiccup) from a valid
            # empty/clean result or items dropped by schema validation.
            if _extract_finding_dicts(result.text) is None and result.text.strip() not in ("[]", ""):
                if attempt < PARSE_RETRIES:
                    logger.warning(
                        "%s agent returned non-JSON output; re-asking once.", agent.value
                    )
                    continue
            return parse_findings(result.text, agent)
        return []

    async def review_chunk(chunk) -> list[Finding]:
        sample_results = await asyncio.gather(
            *(one_sample(chunk) for _ in range(samples_per_chunk))
        )
        return vote_findings(
            list(sample_results), min_support=config.accuracy.min_support
        )

    total_calls = len(chunks) * samples_per_chunk
    results = await asyncio.gather(*(review_chunk(c) for c in chunks))
    findings: list[Finding] = [f for chunk_findings in results for f in chunk_findings]

    error: AgentError | None = None
    if total_calls and failures == total_calls:
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
    for attempt in range(1 + PARSE_RETRIES):
        try:
            result = await asyncio.wait_for(
                provider.complete(
                    system,
                    user,
                    max_tokens=config.limits.max_output_tokens_per_agent * 2,
                    temperature=REVIEWER_TEMPERATURE,
                    model=config.provider.resolved_review_model,
                    json_mode=True,
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
        usage.add(
            "reviewer", result.prompt_tokens, result.completion_tokens,
            cached=result.cached_tokens,
        )
        verdict, findings = _parse_reviewer_output(result.text)
        if findings is None and attempt < PARSE_RETRIES:
            logger.warning("Reviewer returned an unexpected shape; re-asking once.")
            continue
        break

    if findings is None:  # unparseable twice -> deterministic fallback
        return (
            "",
            [f for cluster in clusters for f in cluster],
            usage,
            AgentError(agent="reviewer", message="output did not match the expected schema"),
        )
    return verdict, findings, usage, None


def _parse_reviewer_output(raw: str) -> tuple[str, list[Finding] | None]:
    fenced = _CODE_FENCE.search(raw)
    data = None
    for text in ([fenced.group(1)] if fenced else []) + [raw]:
        for candidate in _iter_json_structures(text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("verdict" in parsed or "findings" in parsed):
                data = parsed
                break
        if data is not None:
            break
    if data is None:
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


# --------------------------------------------------------------------------
# Verifier (V2 A4): the adjudication pass between merge and reviewer.
# --------------------------------------------------------------------------

def _excerpt_for(finding: Finding, line_map: dict[int, str], radius: int = 4) -> str:
    lo = max(1, finding.line_start - radius)
    hi = finding.line_end + radius
    lines = [f"{n:>5} | {line_map[n]}" for n in range(lo, hi + 1) if n in line_map]
    return "\n".join(lines)


async def run_verifier(
    provider: LLMProvider,
    pr_map: str,
    findings: list[Finding],
    files: list,
    config: SentinelConfig,
) -> tuple[list[Finding], UsageStats, AgentError | None]:
    """One batched call that confirms/rejects/downgrades each finding against
    the numbered diff. Fail-open: any failure returns the findings untouched —
    a missing adjudication beats a missing review."""
    from .diffmap import line_text_map  # local import keeps module deps one-way

    usage = UsageStats()
    if not findings:
        return findings, usage, None

    maps = {f.path: line_text_map(f.patch or "") for f in files if not f.skipped}
    payload = {
        "pr": pr_map,
        "findings": [
            {"id": i, **f.model_dump(mode="json", exclude={"also_flagged_by", "support"})}
            for i, f in enumerate(findings)
        ],
        "excerpts": {
            str(i): _excerpt_for(f, maps.get(f.file, {}))
            for i, f in enumerate(findings)
        },
    }
    user = "<verification_input>\n" + json.dumps(payload, indent=1) + "\n</verification_input>"
    verdicts = None
    for attempt in range(1 + PARSE_RETRIES):
        try:
            result = await asyncio.wait_for(
                provider.complete(
                    load_prompt("verifier"),
                    user,
                    max_tokens=config.limits.max_output_tokens_per_agent,
                    temperature=VERIFIER_TEMPERATURE,
                    model=config.provider.resolved_review_model,
                    json_mode=True,
                ),
                timeout=config.limits.agent_timeout_seconds,
            )
        except (ProviderError, asyncio.TimeoutError) as exc:
            logger.warning("Verifier failed (%s); findings pass through unadjudicated.", exc)
            return findings, usage, AgentError(agent="verifier", message=str(exc))
        usage.add("verifier", result.prompt_tokens, result.completion_tokens,
                  cached=result.cached_tokens)
        verdicts = _parse_verifier_output(result.text)
        if verdicts is None and attempt < PARSE_RETRIES:
            logger.warning("Verifier output unparseable; re-asking once.")
            continue
        break

    if verdicts is None:
        logger.warning("Verifier output unparseable; findings pass through unadjudicated.")
        return findings, usage, AgentError(
            agent="verifier", message="output did not match the expected schema"
        )

    kept: list[Finding] = []
    rejected = 0
    for i, finding in enumerate(findings):
        verdict, severity = verdicts.get(i, ("confirm", None))
        if verdict == "reject":
            rejected += 1
            logger.info("Verifier rejected %s finding at %s:%d.",
                        finding.agent.value, finding.file, finding.line_start)
            continue
        if verdict == "downgrade" and severity is not None:
            finding = finding.model_copy(update={"severity": severity})
        kept.append(finding)
    if rejected:
        logger.info("Verifier rejected %d of %d findings.", rejected, len(findings))
    return kept, usage, None


def _parse_verifier_output(raw: str) -> dict[int, tuple[str, Severity | None]] | None:
    fenced = _CODE_FENCE.search(raw)
    data = None
    for text in ([fenced.group(1)] if fenced else []) + [raw]:
        for candidate in _iter_json_structures(text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "verdicts" in parsed:
                data = parsed
                break
            if isinstance(parsed, list):  # tolerate a bare verdicts array
                data = {"verdicts": parsed}
                break
        if data is not None:
            break
    if data is None:
        return None

    verdicts: dict[int, tuple[str, Severity | None]] = {}
    for item in data.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        try:
            finding_id = int(item["id"])
            verdict = str(item.get("verdict", "confirm")).lower()
        except (KeyError, TypeError, ValueError):
            continue
        if verdict not in ("confirm", "reject", "downgrade"):
            verdict = "confirm"
        severity: Severity | None = None
        if verdict == "downgrade":
            try:
                severity = Severity(str(item.get("severity", "")).lower())
            except ValueError:
                verdict = "confirm"  # downgrade without a valid severity = keep as-is
        verdicts[finding_id] = (verdict, severity)
    return verdicts


# --------------------------------------------------------------------------
# Describe (V2 B4) and Ask (V2 B2) — single-call tools.
# --------------------------------------------------------------------------

async def run_describe(
    provider: LLMProvider, pr_map: str, chunks: list, config: SentinelConfig
) -> tuple[dict | None, UsageStats]:
    """Generate {summary, type, walkthrough} for the PR body. None on failure."""
    usage = UsageStats()
    diff_text = "\n\n".join(c.text for c in chunks)[:60_000]
    user = f"{pr_map}\n\n<diff>\n{diff_text}\n</diff>"
    try:
        result = await asyncio.wait_for(
            provider.complete(
                load_prompt("describe"),
                user,
                max_tokens=config.limits.max_output_tokens_per_agent,
                temperature=REVIEWER_TEMPERATURE,
                model=config.provider.resolved_review_model,
                json_mode=True,
            ),
            timeout=config.limits.agent_timeout_seconds,
        )
    except (ProviderError, asyncio.TimeoutError) as exc:
        logger.warning("Describe failed: %s", exc)
        return None, usage
    usage.add("describe", result.prompt_tokens, result.completion_tokens,
              cached=result.cached_tokens)

    fenced = _CODE_FENCE.search(result.text)
    for text in ([fenced.group(1)] if fenced else []) + [result.text]:
        for candidate in _iter_json_structures(text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "summary" in parsed:
                return parsed, usage
    return None, usage


async def run_ask(
    provider: LLMProvider, pr_map: str, chunks: list, question: str,
    config: SentinelConfig,
) -> tuple[str | None, UsageStats]:
    """Answer a maintainer's question about the diff. None on failure."""
    from .security import sanitize_for_prompt

    usage = UsageStats()
    diff_text = "\n\n".join(c.text for c in chunks)[:60_000]
    user = (
        f"<question>{sanitize_for_prompt(question)[:1000]}</question>\n\n"
        f"{pr_map}\n\n<diff>\n{diff_text}\n</diff>"
    )
    try:
        result = await asyncio.wait_for(
            provider.complete(
                load_prompt("ask"),
                user,
                max_tokens=config.limits.max_output_tokens_per_agent,
                temperature=REVIEWER_TEMPERATURE,
                model=config.provider.resolved_review_model,
            ),
            timeout=config.limits.agent_timeout_seconds,
        )
    except (ProviderError, asyncio.TimeoutError) as exc:
        logger.warning("Ask failed: %s", exc)
        return None, usage
    usage.add("ask", result.prompt_tokens, result.completion_tokens,
              cached=result.cached_tokens)
    return result.text.strip() or None, usage
