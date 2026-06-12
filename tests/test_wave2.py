"""Wave 2: custom instructions (P5), review event (P7), score line (P10)."""

from pr_sentinel.agents import analyst_system_prompt
from pr_sentinel.config import load_config
from pr_sentinel.formatter import readiness_score, review_effort
from pr_sentinel.models import AgentName, ChangedFile, Finding


class TestCustomInstructions:
    def test_global_guidance_appended(self):
        p = analyst_system_prompt(AgentName.SECURITY, guidance="This is a Django project.")
        assert "Repository-specific guidance" in p
        assert "Django" in p

    def test_per_agent_instruction_appended(self):
        p = analyst_system_prompt(AgentName.ARCHITECT, agent_instructions="We use hexagonal arch.")
        assert "hexagonal" in p

    def test_no_guidance_no_section(self):
        p = analyst_system_prompt(AgentName.TEST)
        assert "Repository-specific guidance" not in p

    def test_config_parses_instructions(self):
        c = load_config(
            "agents:\n  guidance: ignore TODOs\n"
            "  instructions:\n    security: focus on authz\n"
        )
        assert c.agents.guidance == "ignore TODOs"
        assert c.agents.instructions["security"] == "focus on authz"


class TestScore:
    def _f(self, sev):
        return Finding(agent="security", file="a.py", line_start=1, line_end=1,
                       severity=sev, category="c", message="m")

    def test_clean_is_100(self):
        assert readiness_score([]) == 100

    def test_critical_drops_hard(self):
        assert readiness_score([self._f("critical")]) == 60

    def test_floor_at_zero(self):
        assert readiness_score([self._f("critical")] * 5) == 0

    def test_effort_scales_with_size(self):
        small = [ChangedFile(path="a.py", status="modified", additions=2)]
        big = [ChangedFile(path=f"f{i}.py", status="modified", additions=200)
               for i in range(10)]
        assert review_effort([], small) < review_effort([], big)


class TestScoreInReview:
    def test_summary_shows_readiness(self):
        from pr_sentinel.formatter import format_review
        from pr_sentinel.models import UsageStats

        f = Finding(agent="security", file="a.py", line_start=1, line_end=1,
                    severity="high", category="c", message="m")
        out = format_review([f], [], UsageStats(), "gpt-5-mini")
        assert "Merge readiness:" in out and "/100" in out
