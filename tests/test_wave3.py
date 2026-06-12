"""Wave 3 moat: risk labels (P8), adaptive sampling (P12), cross-file (P13),
confidence display (P14)."""

import json

from pr_sentinel.config import SentinelConfig
from pr_sentinel.formatter import _confidence, format_inline_body
from pr_sentinel.graph import _risk_labels, build_graph
from pr_sentinel.models import AgentName, Finding, PRMetadata
from tests.conftest import MockProvider, finding_json, make_file


def f(agent="security", sev="high", support=1):
    return Finding(agent=agent, file="a.py", line_start=1, line_end=1,
                   severity=sev, category="c", message="m", support=support)


class TestRiskLabels:
    def test_clean_label(self):
        assert _risk_labels([]) == ["pr-sentinel:clean"]

    def test_security_and_attention(self):
        labels = _risk_labels([f(agent="security", sev="critical")])
        assert "security" in labels and "pr-sentinel:needs-attention" in labels

    def test_test_agent_needs_tests(self):
        assert "needs-tests" in _risk_labels([f(agent="test", sev="medium")])


class TestConfidence:
    def test_support_shown_when_multiple_agents(self):
        assert "2 agents agreed" in _confidence(f(support=2))

    def test_no_badge_for_single_support(self):
        assert _confidence(f(support=1)) == ""

    def test_inline_body_shows_confidence(self):
        body = format_inline_body(f(support=3))
        assert "3 agents agreed" in body


class TestAdaptiveSampling:
    async def test_clean_chunk_uses_one_sample(self):
        # Default config: samples=3, adaptive=True. A chunk the analyst finds
        # clean should cost ONE call, not three.
        config = SentinelConfig()
        from pr_sentinel.agents import run_analyst
        from pr_sentinel.chunking import apply_skip_rules, build_chunks, build_pr_map

        files = apply_skip_rules([make_file()], config)
        chunks, pr_map = build_chunks(files, config), build_pr_map("t", files)
        provider = MockProvider()  # returns [] -> clean
        _, _, _ = await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        assert len(provider.calls) == 1  # adaptive stopped after the clean sample

    async def test_finding_chunk_draws_full_samples(self):
        config = SentinelConfig()
        from pr_sentinel.agents import run_analyst
        from pr_sentinel.chunking import apply_skip_rules, build_chunks, build_pr_map

        files = apply_skip_rules([make_file()], config)
        chunks, pr_map = build_chunks(files, config), build_pr_map("t", files)
        provider = MockProvider({"Security agent": finding_json()})
        await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        assert len(provider.calls) == 3  # found something -> full vote


class TestCrossFileNode:
    async def test_cross_file_disabled_by_default(self):
        provider = MockProvider()
        graph = build_graph(provider, github=None)
        await graph.ainvoke({
            "config": SentinelConfig(),
            "pr": PRMetadata(repo="o/r", number=1, title="t"),
            "files": [make_file(path="a.py"), make_file(path="b.py")],
        })
        assert not any("Cross-file agent" in c["system"] for c in provider.calls)

    async def test_cross_file_runs_when_enabled(self):
        config = SentinelConfig()
        config.accuracy.cross_file = True
        cross = json.dumps({"findings": [{
            "file": "a.py", "line_start": 1, "line_end": 1, "severity": "high",
            "category": "stale-caller", "message": "caller not updated",
            "evidence": "x = 1"}]})
        provider = MockProvider({"Cross-file agent": cross,
                                 "Reviewer agent": json.dumps({"verdict": "", "findings": []})})
        graph = build_graph(provider, github=None)
        await graph.ainvoke({
            "config": config,
            "pr": PRMetadata(repo="o/r", number=1, title="t"),
            "files": [make_file(path="a.py"), make_file(path="b.py")],
        })
        assert any("Cross-file agent" in c["system"] for c in provider.calls)
