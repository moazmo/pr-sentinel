"""The deterministic dedup pre-pass (D5 stage 1) — pure functions, exhaustively tested."""

from pr_sentinel.merge import (
    cluster_by_proximity,
    collapse_duplicates,
    filter_by_severity,
    merge_findings,
    order_and_cap,
)
from pr_sentinel.models import AgentName, Finding, Severity


def make(agent="security", file="app.py", start=10, end=10, severity="high",
         category="sql-injection", message="msg", suggestion=None):
    return Finding(
        agent=agent, file=file, line_start=start, line_end=end,
        severity=severity, category=category, message=message, suggestion=suggestion,
    )


class TestCollapseDuplicates:
    def test_same_line_same_category_merges(self):
        merged = collapse_duplicates([make(agent="security"), make(agent="architect")])
        assert len(merged) == 1
        assert merged[0].agent == AgentName.SECURITY
        assert merged[0].also_flagged_by == [AgentName.ARCHITECT]

    def test_higher_severity_wins(self):
        merged = collapse_duplicates(
            [make(severity="medium", message="weak"), make(agent="architect",
             severity="critical", message="strong")]
        )
        assert merged[0].severity == Severity.CRITICAL
        assert merged[0].message == "strong"

    def test_different_category_same_line_not_merged(self):
        merged = collapse_duplicates([make(category="sql-injection"), make(category="n-plus-one")])
        assert len(merged) == 2

    def test_different_file_not_merged(self):
        merged = collapse_duplicates([make(file="a.py"), make(file="b.py")])
        assert len(merged) == 2

    def test_non_overlapping_lines_not_merged(self):
        merged = collapse_duplicates([make(start=1, end=3), make(start=50, end=52)])
        assert len(merged) == 2

    def test_overlapping_ranges_expand(self):
        merged = collapse_duplicates([make(start=5, end=12), make(start=10, end=20)])
        assert len(merged) == 1
        assert (merged[0].line_start, merged[0].line_end) == (5, 20)

    def test_same_agent_duplicate_not_double_credited(self):
        merged = collapse_duplicates([make(), make()])
        assert len(merged) == 1
        assert merged[0].also_flagged_by == []


class TestFilterAndCap:
    def test_threshold_filters_below(self):
        findings = [make(severity=s.value, start=i, category=f"c{i}")
                    for i, s in enumerate(Severity)]
        kept = filter_by_severity(findings, Severity.MEDIUM)
        assert {f.severity for f in kept} == {Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM}

    def test_order_is_severity_first(self):
        ordered = order_and_cap([make(severity="low", start=1, category="a"),
                                 make(severity="critical", start=99, category="b")])
        assert ordered[0].severity == Severity.CRITICAL

    def test_cap_keeps_most_severe(self):
        findings = [make(severity="nit", start=i * 20, category=f"n{i}") for i in range(50)]
        findings.append(make(severity="critical", start=2000, category="boom"))
        capped = order_and_cap(findings, cap=10)
        assert len(capped) == 10
        assert capped[0].severity == Severity.CRITICAL


class TestClustering:
    def test_nearby_findings_cluster(self):
        clusters = cluster_by_proximity(
            [make(start=10, category="a"), make(start=13, category="b")]
        )
        assert len(clusters) == 1 and len(clusters[0]) == 2

    def test_distant_findings_do_not_cluster(self):
        clusters = cluster_by_proximity(
            [make(start=10, category="a"), make(start=100, category="b")]
        )
        assert len(clusters) == 2

    def test_different_files_do_not_cluster(self):
        clusters = cluster_by_proximity(
            [make(file="a.py", category="a"), make(file="b.py", category="b")]
        )
        assert len(clusters) == 2


def test_full_merge_pipeline():
    findings = [
        make(agent="security", severity="high"),
        make(agent="architect", severity="medium"),       # duplicate of above
        make(severity="nit", start=200, category="style"),  # filtered by threshold
        make(file="other.py", severity="critical", category="auth-bypass"),
    ]
    merged, clusters = merge_findings(findings, Severity.MEDIUM)
    assert len(merged) == 2
    assert merged[0].severity == Severity.CRITICAL
    assert merged[1].also_flagged_by == [AgentName.ARCHITECT]
    assert len(clusters) == 2


def test_empty_input():
    merged, clusters = merge_findings([], Severity.MEDIUM)
    assert merged == [] and clusters == []
