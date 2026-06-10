"""Output formatting (D8): one sticky comment — verdict header, severity-grouped
findings with agent attribution, collapsed detail, usage footer.

Enforces GitHub's 65,536-char comment cap by collapsing lowest-severity
sections into <details> first, then truncating with a pointer to the logs.
The final string is secret-scrubbed by the caller before posting.
"""

from __future__ import annotations

from .github_client import COMMENT_MARKER, MAX_COMMENT_CHARS
from .models import AgentError, ChangedFile, Finding, Severity, UsageStats
from .provider import estimate_cost_usd

_SEVERITY_HEADER = {
    Severity.CRITICAL: "🔴 Critical",
    Severity.HIGH: "🟠 High",
    Severity.MEDIUM: "🟡 Medium",
    Severity.LOW: "🔵 Low",
    Severity.NIT: "⚪ Nit",
}

_AGENT_LABEL = {
    "architect": "Architect",
    "security": "Security",
    "performance": "Performance",
    "test": "Test",
    "reviewer": "Reviewer",
}


def _attribution(finding: Finding) -> str:
    agents = [finding.agent.value] + [a.value for a in finding.also_flagged_by]
    return " + ".join(_AGENT_LABEL.get(a, a) for a in dict.fromkeys(agents))


def _location(finding: Finding) -> str:
    if finding.line_start <= 0:
        return f"`{finding.file}`"
    if finding.line_end > finding.line_start:
        return f"`{finding.file}:{finding.line_start}-{finding.line_end}`"
    return f"`{finding.file}:{finding.line_start}`"


def _finding_line(finding: Finding) -> str:
    line = f"- **{_location(finding)}** — [{_attribution(finding)}] {finding.message}"
    if finding.suggestion:
        line += (
            f"\n  <details><summary>Suggested fix</summary>\n\n"
            f"  {finding.suggestion}\n\n  </details>"
        )
    return line


def _severity_section(severity: Severity, findings: list[Finding], collapsed: bool) -> str:
    body = "\n".join(_finding_line(f) for f in findings)
    header = f"{_SEVERITY_HEADER[severity]} ({len(findings)})"
    if collapsed:
        return f"<details><summary><b>{header}</b></summary>\n\n{body}\n\n</details>"
    return f"### {header}\n\n{body}"


def _skipped_section(files: list[ChangedFile]) -> str:
    noted = [f for f in files if f.skipped or f.truncated]
    if not noted:
        return ""
    lines = []
    for f in noted:
        note = f.skip_reason if f.skipped else f.truncation_note
        lines.append(f"- `{f.path}` — {note}")
    return (
        f"<details><summary>ℹ️ Skipped / partial files ({len(noted)})</summary>\n\n"
        + "\n".join(lines)
        + "\n\n</details>"
    )


def _footer(usage: UsageStats, model: str, agents_run: int) -> str:
    total_in, total_out = usage.total_prompt, usage.total_completion
    cost, exact = estimate_cost_usd(model, total_in, total_out)
    approx = "" if exact else "~rate "
    cost_part = (
        f" · ~{(total_in + total_out) / 1000:.1f}k tokens ({approx}≈${cost:.3f} on `{model}`)"
        if (total_in + total_out)
        else ""
    )
    return (
        f"<sub>{agents_run} agents{cost_part} · "
        f"[PR Sentinel](https://github.com/moazmo/pr-sentinel) · "
        f"tune via `.pr-sentinel.yml`</sub>"
    )


def format_review(
    findings: list[Finding],
    files: list[ChangedFile],
    usage: UsageStats,
    model: str,
    *,
    verdict: str = "",
    errors: list[AgentError] | None = None,
    warnings: list[str] | None = None,
    agents_run: int = 5,
) -> str:
    errors = errors or []
    warnings = warnings or []

    parts: list[str] = ["## 🛡️ PR Sentinel Review"]

    by_severity: dict[Severity, list[Finding]] = {}
    for f in sorted(findings, key=lambda f: (f.severity.rank, f.file, f.line_start)):
        by_severity.setdefault(f.severity, []).append(f)

    completed = agents_run - len(errors)
    if findings:
        counts = ", ".join(
            f"{len(v)} {s.value.capitalize()}" for s, v in by_severity.items()
        )
        parts.append(
            f"**{len(findings)} finding{'s' if len(findings) != 1 else ''} ({counts}) "
            f"· {completed}/{agents_run} agents completed**"
        )
        if verdict:
            parts.append(f"> {verdict}")
    elif errors:
        # Zero findings is NOT "looks clean" when agents failed — say so honestly.
        parts.append(
            f"⚠️ **No findings, but only {completed}/{agents_run} agents completed** — "
            "this may be a partial result."
        )
    else:
        parts.append(
            f"✅ **Looks clean** — {completed}/{agents_run} agents found nothing worth flagging."
        )

    # Low/nit start collapsed; everything collapses progressively if we
    # blow the comment cap.
    for severity, group in by_severity.items():
        collapsed = severity in (Severity.LOW, Severity.NIT)
        parts.append(_severity_section(severity, group, collapsed))

    if errors:
        error_lines = "\n".join(
            f"- {_AGENT_LABEL.get(e.agent, e.agent)} agent could not complete: {e.message}"
            for e in errors
        )
        parts.append(f"⚠️ **Partial review**\n\n{error_lines}")

    if warnings:
        parts.append("\n".join(f"> ⚠️ {w}" for w in warnings))

    skipped = _skipped_section(files)
    if skipped:
        parts.append(skipped)

    parts.append("---")
    parts.append(_footer(usage, model, agents_run))
    parts.append(COMMENT_MARKER)

    return _enforce_limit(parts, by_severity, files, usage, model, agents_run)


def format_failure(reason: str) -> str:
    """Comment posted when the run itself failed (NFR1: fail visibly, exit clean)."""
    return (
        "## 🛡️ PR Sentinel Review\n\n"
        f"⚠️ The review could not be completed: {reason}\n\n"
        "Your CI is unaffected — PR Sentinel never fails the build. "
        "Check the Action logs for details.\n\n"
        "---\n"
        f"<sub>[PR Sentinel](https://github.com/moazmo/pr-sentinel)</sub>\n"
        f"{COMMENT_MARKER}"
    )


def format_dry_run(
    files: list[ChangedFile], est_input_tokens: int, model: str, n_calls: int
) -> str:
    reviewable = [f for f in files if not f.skipped]
    # Output is roughly 15-20% of input for review workloads; estimate high.
    est_output = max(500, est_input_tokens // 5)
    cost, exact = estimate_cost_usd(model, est_input_tokens, est_output)
    approx = "" if exact else " (default rate)"
    body = (
        "## 🛡️ PR Sentinel — Dry Run\n\n"
        f"Would review **{len(reviewable)} file(s)** in ~{n_calls} LLM call(s) per analyst, "
        f"~{est_input_tokens / 1000:.1f}k input tokens, estimated **≈${cost:.3f}** "
        f"on `{model}`{approx}.\n\n"
        "No LLM calls were made. Set `dry_run: false` in `.pr-sentinel.yml` to enable reviews.\n"
    )
    skipped = _skipped_section(files)
    if skipped:
        body += "\n" + skipped + "\n"
    return body + f"\n---\n{COMMENT_MARKER}"


def _enforce_limit(
    parts: list[str],
    by_severity: dict[Severity, list[Finding]],
    files: list[ChangedFile],
    usage: UsageStats,
    model: str,
    agents_run: int,
) -> str:
    comment = "\n\n".join(parts)
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment

    # Pass 1: collapse every severity section.
    collapsed_parts = [parts[0], parts[1]]
    for severity, group in by_severity.items():
        collapsed_parts.append(_severity_section(severity, group, collapsed=True))
    collapsed_parts.extend(parts[2 + len(by_severity):])
    comment = "\n\n".join(collapsed_parts)
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment

    # Pass 2: hard truncate, keep marker + pointer to logs.
    suffix = (
        "\n\n*(review truncated — full output in the Action logs)*\n\n---\n"
        f"{_footer(usage, model, agents_run)}\n\n{COMMENT_MARKER}"
    )
    return comment[: MAX_COMMENT_CHARS - len(suffix)] + suffix
