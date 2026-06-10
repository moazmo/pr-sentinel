"""Output formatting (D8): structure, zero-findings, comment cap, disclosure."""

from pr_sentinel.formatter import format_dry_run, format_failure, format_review
from pr_sentinel.github_client import COMMENT_MARKER, MAX_COMMENT_CHARS
from pr_sentinel.models import AgentError, Finding, UsageStats
from tests.conftest import make_file


def make(severity="high", start=10, message="Query built from user input.", **kw):
    defaults = dict(agent="security", file="api/users.py", line_start=start,
                    line_end=start, severity=severity, category="sql-injection",
                    message=message)
    defaults.update(kw)
    return Finding(**defaults)


def usage_with(tokens=1000):
    u = UsageStats()
    u.add("security", tokens, tokens // 5)
    return u


class TestFormatReview:
    def test_structure_with_findings(self):
        comment = format_review(
            [make(), make(severity="medium", start=50, category="naming",
                          agent="architect", message="Misleading name.")],
            [make_file()], usage_with(), "gpt-5-mini", verdict="Two real issues.",
        )
        assert comment.startswith("## 🛡️ PR Sentinel Review")
        assert "2 findings" in comment
        assert "🟠 High" in comment and "🟡 Medium" in comment
        assert "`api/users.py:10`" in comment
        assert "[Security]" in comment and "[Architect]" in comment
        assert "> Two real issues." in comment
        assert COMMENT_MARKER in comment

    def test_agent_attribution_includes_co_flaggers(self):
        f = make(also_flagged_by=["architect"])
        comment = format_review([f], [], usage_with(), "gpt-5-mini")
        assert "[Security + Architect]" in comment

    def test_zero_findings_posts_looks_clean(self):
        comment = format_review([], [make_file()], usage_with(), "gpt-5-mini")
        assert "Looks clean" in comment
        assert COMMENT_MARKER in comment

    def test_suggestion_rendered_collapsible(self):
        comment = format_review([make(suggestion="Use placeholders.")], [],
                                usage_with(), "gpt-5-mini")
        assert "<details><summary>Suggested fix</summary>" in comment

    def test_agent_errors_disclosed(self):
        comment = format_review([], [], usage_with(), "gpt-5-mini",
                                errors=[AgentError(agent="performance", message="timeout")])
        assert "Partial review" in comment
        assert "Performance agent could not complete" in comment
        assert "4/5 agents" in comment

    def test_skipped_and_truncated_files_disclosed(self):
        skipped = make_file(path="package-lock.json")
        skipped.skipped, skipped.skip_reason = True, "lockfile"
        partial = make_file(path="big.py")
        partial.truncated, partial.truncation_note = True, "reviewed partially (60% of hunks)"
        comment = format_review([], [skipped, partial], usage_with(), "gpt-5-mini")
        assert "Skipped / partial files (2)" in comment
        assert "package-lock.json" in comment and "60% of hunks" in comment

    def test_cost_line_in_footer(self):
        comment = format_review([], [], usage_with(10_000), "gpt-5-mini")
        assert "tokens" in comment and "$" in comment

    def test_config_warnings_surface(self):
        comment = format_review([], [], usage_with(), "gpt-5-mini",
                                warnings=["`.pr-sentinel.yml` could not be parsed"])
        assert "could not be parsed" in comment

    def test_comment_cap_enforced(self):
        findings = [make(start=i, category=f"cat-{i}", message="x" * 1500)
                    for i in range(100)]
        comment = format_review(findings, [], usage_with(), "gpt-5-mini")
        assert len(comment) <= MAX_COMMENT_CHARS
        assert COMMENT_MARKER in comment  # marker survives truncation

    def test_low_and_nit_start_collapsed(self):
        comment = format_review(
            [make(severity="nit", category="style", message="tiny")],
            [], usage_with(), "gpt-5-mini",
        )
        assert "<details><summary><b>⚪ Nit" in comment


def test_failure_comment_is_clean_and_marked():
    comment = format_failure("ProviderError")
    assert "could not be completed" in comment
    assert "never fails the build" in comment
    assert COMMENT_MARKER in comment


def test_dry_run_comment_estimates_without_llm():
    files = [make_file()]
    comment = format_dry_run(files, est_input_tokens=19_000, model="gpt-5-mini", n_calls=3)
    assert "Dry Run" in comment
    assert "19.0k input tokens" in comment
    assert "No LLM calls were made" in comment
    assert COMMENT_MARKER in comment
