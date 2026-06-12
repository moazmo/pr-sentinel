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


def _confidence(finding: Finding) -> str:
    """A small user-facing trust signal from the ensemble vote (V2 P14)."""
    if finding.support >= 2:
        return f" <sub>· {finding.support} agents agreed</sub>"
    return ""


def _finding_line(finding: Finding) -> str:
    line = (
        f"- **{_location(finding)}** — [{_attribution(finding)}] "
        f"{finding.message}{_confidence(finding)}"
    )
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
    cache_part = ""
    if usage.total_cached and total_in:
        cache_part = f", {100 * usage.total_cached // total_in}% cached"
    cost_part = (
        f" · ~{(total_in + total_out) / 1000:.1f}k tokens "
        f"({approx}≈${cost:.3f} on `{model}`{cache_part})"
        if (total_in + total_out)
        else ""
    )
    return (
        f"<sub>{agents_run} agents{cost_part} · "
        f"[PR Sentinel](https://github.com/moazmo/pr-sentinel) · "
        f"tune via `.pr-sentinel.yml`</sub>"
    )


_SEVERITY_PENALTY = {
    Severity.CRITICAL: 40, Severity.HIGH: 20, Severity.MEDIUM: 8,
    Severity.LOW: 3, Severity.NIT: 1,
}


def readiness_score(findings: list[Finding]) -> int:
    """A deterministic 0–100 merge-readiness score (V2 P10) — no LLM call.
    Starts at 100; each finding subtracts by severity."""
    score = 100 - sum(_SEVERITY_PENALTY[f.severity] for f in findings)
    return max(0, min(100, score))


def review_effort(findings: list[Finding], files: list[ChangedFile]) -> int:
    """A 1–5 review-effort estimate from change size + findings (V2 P10)."""
    reviewable = sum(1 for f in files if not f.skipped)
    churn = sum(f.additions + f.deletions for f in files if not f.skipped)
    points = reviewable + churn // 80 + len(findings)
    for threshold, effort in ((2, 1), (6, 2), (15, 3), (40, 4)):
        if points <= threshold:
            return effort
    return 5


def format_inline_body(finding: Finding, *, suggestions: bool = True) -> str:
    """The body of one inline review comment (V2 B1).

    When the finding carries a literal `fix` and suggestions are enabled, the
    fix is rendered as a GitHub ```suggestion block (V2 P1) — the author clicks
    "Commit suggestion" to apply it. Prose `suggestion` is shown otherwise.
    """
    severity = _SEVERITY_HEADER[finding.severity]
    body = f"**{severity}** · [{_attribution(finding)}] {finding.message}"
    # A suggestion block replaces the single anchored line, so only offer it for
    # single-line findings whose fix is itself one line — otherwise the apply
    # would mangle the file. Anything else falls back to prose.
    body += _confidence(finding)
    single_line = finding.line_start == finding.line_end
    fix_one_line = finding.fix and "\n" not in finding.fix.strip()
    if suggestions and single_line and fix_one_line:
        body += f"\n\n```suggestion\n{finding.fix.rstrip()}\n```"
    elif finding.suggestion:
        body += f"\n\n**Suggested fix:** {finding.suggestion}"
    elif finding.fix:
        body += f"\n\n**Suggested fix:**\n```\n{finding.fix.rstrip()}\n```"
    return body + "\n\n<sub>🛡️ PR Sentinel</sub>"


def format_description(description: dict) -> str:
    """Render the Describe agent's JSON into the PR-body block (V2 B4)."""
    parts = ["### 🛡️ PR Sentinel — summary", str(description.get("summary", "")).strip()]
    change_type = str(description.get("type", "")).strip()
    if change_type:
        parts.append(f"**Type:** {change_type}")
    walkthrough = description.get("walkthrough") or []
    rows = [
        f"| `{item['file']}` | {item['change']} |"
        for item in walkthrough
        if isinstance(item, dict) and item.get("file") and item.get("change")
    ]
    if rows:
        parts.append("| File | Change |\n|---|---|\n" + "\n".join(rows))
    return "\n\n".join(p for p in parts if p)


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
    inline_findings: list[Finding] | None = None,
) -> str:
    errors = errors or []
    warnings = warnings or []
    inline_findings = inline_findings or []
    total_count = len(findings) + len(inline_findings)

    parts: list[str] = ["## 🛡️ PR Sentinel Review"]

    by_severity: dict[Severity, list[Finding]] = {}
    for f in sorted(findings, key=lambda f: (f.severity.rank, f.file, f.line_start)):
        by_severity.setdefault(f.severity, []).append(f)

    completed = agents_run - len(errors)
    if total_count:
        all_by_severity: dict[Severity, int] = {}
        for f in findings + inline_findings:
            all_by_severity[f.severity] = all_by_severity.get(f.severity, 0) + 1
        counts = ", ".join(
            f"{n} {s.value.capitalize()}"
            for s, n in sorted(all_by_severity.items(), key=lambda kv: kv[0].rank)
        )
        inline_note = f" · {len(inline_findings)} posted inline" if inline_findings else ""
        all_findings = findings + inline_findings
        score = readiness_score(all_findings)
        effort = review_effort(all_findings, files)
        parts.append(
            f"**{total_count} finding{'s' if total_count != 1 else ''} ({counts}) "
            f"· {completed}/{agents_run} agents completed{inline_note}**"
        )
        parts.append(
            f"<sub>Merge readiness: **{score}/100** · review effort: {effort}/5</sub>"
        )
        if verdict:
            parts.append(f"> {verdict}")
        if inline_findings:
            index = "\n".join(
                f"- {_location(f)} — [{_attribution(f)}] {f.category}"
                for f in sorted(inline_findings, key=lambda f: (f.severity.rank, f.file))
            )
            parts.append(
                f"<details><summary>📌 Inline comments ({len(inline_findings)})"
                f"</summary>\n\n{index}\n\n</details>"
            )
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

    comment = "\n\n".join(parts)
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment
    return _enforce_limit(
        head=parts[: _severity_start_index(parts, by_severity)],
        by_severity=by_severity,
        tail=parts[_severity_start_index(parts, by_severity) + len(by_severity):],
        usage=usage,
        model=model,
        agents_run=agents_run,
    )


def _severity_start_index(parts: list[str], by_severity: dict) -> int:
    """Index of the first severity section in `parts` — found by its `###`/
    `<details>` header, not by a fragile fixed offset (F1)."""
    if not by_severity:
        return len(parts)
    first_header = _SEVERITY_HEADER[next(iter(by_severity))]
    for i, p in enumerate(parts):
        if first_header in p and (p.startswith("###") or p.startswith("<details>")):
            return i
    return len(parts)


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
    *,
    head: list[str],
    by_severity: dict[Severity, list[Finding]],
    tail: list[str],
    usage: UsageStats,
    model: str,
    agents_run: int,
) -> str:
    """Bring an over-long comment under GitHub's 65,536-char cap by first
    collapsing every severity section into <details>, then hard-truncating.

    Rebuilt from the structured head/sections/tail (F1) rather than slicing the
    flat parts list — so an optional verdict quote or inline-comment index in
    the head can't shift the offsets and drop the wrong block.
    """
    collapsed = list(head)
    for severity, group in by_severity.items():
        collapsed.append(_severity_section(severity, group, collapsed=True))
    collapsed.extend(tail)
    comment = "\n\n".join(collapsed)
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment

    # Hard truncate, keeping marker + footer + a pointer to the logs.
    suffix = (
        "\n\n*(review truncated — full output in the Action logs)*\n\n---\n"
        f"{_footer(usage, model, agents_run)}\n\n{COMMENT_MARKER}"
    )
    return comment[: MAX_COMMENT_CHARS - len(suffix)] + suffix
