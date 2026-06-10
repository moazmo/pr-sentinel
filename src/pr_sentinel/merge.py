"""Deterministic dedup/merge pre-pass (D5 stage 1). Pure functions only —
this is deliberately the most heavily unit-tested code in the repo.

Pipeline: collapse exact duplicates -> filter by severity threshold ->
severity-order and cap -> cluster by file/line proximity for the reviewer.
Running the threshold filter BEFORE the reviewer LLM means filtered findings
never cost the user reviewer tokens.
"""

from __future__ import annotations

from .models import Finding, Severity

PROXIMITY_LINES = 5
MAX_FINDINGS_FOR_REVIEWER = 40


def collapse_duplicates(findings: list[Finding]) -> list[Finding]:
    """Merge findings on the same file + overlapping lines + same category.
    Keeps the most severe copy and records every other agent as a co-source."""
    merged: list[Finding] = []
    for finding in findings:
        target = next(
            (
                m
                for m in merged
                if m.category == finding.category and m.overlaps(finding)
            ),
            None,
        )
        if target is None:
            merged.append(finding.model_copy(deep=True))
            continue
        if finding.severity.rank < target.severity.rank:
            target.severity = finding.severity
            target.message = finding.message
            if finding.suggestion:
                target.suggestion = finding.suggestion
        if finding.agent != target.agent and finding.agent not in target.also_flagged_by:
            target.also_flagged_by.append(finding.agent)
        target.line_start = min(target.line_start, finding.line_start)
        target.line_end = max(target.line_end, finding.line_end)
    return merged


def filter_by_severity(findings: list[Finding], threshold: Severity) -> list[Finding]:
    return [f for f in findings if f.severity.rank <= threshold.rank]


def order_and_cap(
    findings: list[Finding], cap: int = MAX_FINDINGS_FOR_REVIEWER
) -> list[Finding]:
    """Severity-first ordering (stable within a severity by file/line) and a
    hard cap so a pathological PR can't explode the reviewer's input."""
    ordered = sorted(
        findings, key=lambda f: (f.severity.rank, f.file, f.line_start, f.category)
    )
    return ordered[:cap]


def cluster_by_proximity(
    findings: list[Finding], slack: int = PROXIMITY_LINES
) -> list[list[Finding]]:
    """Group findings within `slack` lines in the same file so the reviewer
    sees related findings together (semantic duplicates usually live in the
    same cluster)."""
    clusters: list[list[Finding]] = []
    for finding in sorted(findings, key=lambda f: (f.file, f.line_start)):
        target = next(
            (
                c
                for c in clusters
                if c[0].file == finding.file
                and any(finding.overlaps(member, slack=slack) for member in c)
            ),
            None,
        )
        if target is None:
            clusters.append([finding])
        else:
            target.append(finding)
    return clusters


def merge_findings(
    findings: list[Finding], threshold: Severity
) -> tuple[list[Finding], list[list[Finding]]]:
    """The full deterministic pass. Returns (merged_flat, clusters)."""
    merged = collapse_duplicates(findings)
    merged = filter_by_severity(merged, threshold)
    merged = order_and_cap(merged)
    return merged, cluster_by_proximity(merged)
