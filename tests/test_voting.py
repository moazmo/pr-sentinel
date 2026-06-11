"""Self-consistency voting (V2 A3) and verifier adjudication (V2 A4)."""

import json


from pr_sentinel.agents import _parse_verifier_output, run_verifier
from pr_sentinel.config import SentinelConfig
from pr_sentinel.merge import vote_findings
from pr_sentinel.models import ChangedFile, Finding, Severity
from tests.conftest import FailingProvider, MockProvider


def f(category="sql-injection", start=10, severity="high", agent="security",
      evidence="line"):
    return Finding(agent=agent, file="a.py", line_start=start, line_end=start,
                   severity=severity, category=category, message="m", evidence=evidence)


class TestVote:
    def test_single_sample_passthrough(self):
        kept = vote_findings([[f(), f(category="other", start=50)]], min_support=2)
        assert len(kept) == 2  # no voting with one sample

    def test_majority_kept_singleton_dropped(self):
        samples = [
            [f(category="sqli", start=10)],
            [f(category="sqli", start=10)],
            [f(category="nit", start=80, severity="nit")],  # seen once, low severity
        ]
        kept = vote_findings(samples, min_support=2)
        cats = {x.category for x in kept}
        assert "sqli" in cats and "nit" not in cats

    def test_support_counts_distinct_samples(self):
        samples = [[f(category="sqli")], [f(category="sqli")], [f(category="sqli")]]
        kept = vote_findings(samples, min_support=2)
        assert len(kept) == 1 and kept[0].support == 3

    def test_high_severity_singleton_survives_for_verification(self):
        # A single sample spotting a critical isn't dropped by the vote —
        # evidence anchoring + verifier decide its fate.
        samples = [[f(category="rce", severity="critical")], [f(category="x", start=99)], []]
        kept = vote_findings(samples, min_support=2)
        assert any(x.category == "rce" for x in kept)

    def test_higher_severity_wins_on_merge(self):
        samples = [
            [f(category="sqli", severity="medium")],
            [f(category="sqli", severity="critical")],
        ]
        kept = vote_findings(samples, min_support=2)
        assert kept[0].severity == Severity.CRITICAL


class TestVerifier:
    def _files(self):
        patch = "@@ -1,2 +1,3 @@\n import os\n+os.system(cmd)\n"
        return [ChangedFile(path="a.py", status="modified", patch=patch)]

    def _findings(self):
        return [f(category="cmd-injection", start=2, evidence="os.system(cmd)")]

    async def test_confirm_keeps(self):
        resp = json.dumps({"verdicts": [{"id": 0, "verdict": "confirm", "reason": "real"}]})
        kept, usage, err = await run_verifier(
            MockProvider(default=resp), "map", self._findings(), self._files(), SentinelConfig()
        )
        assert len(kept) == 1 and err is None

    async def test_reject_drops(self):
        resp = json.dumps({"verdicts": [{"id": 0, "verdict": "reject", "reason": "safe"}]})
        kept, usage, err = await run_verifier(
            MockProvider(default=resp), "map", self._findings(), self._files(), SentinelConfig()
        )
        assert kept == []

    async def test_downgrade_changes_severity(self):
        resp = json.dumps({"verdicts": [
            {"id": 0, "verdict": "downgrade", "severity": "low", "reason": "minor"}
        ]})
        kept, usage, err = await run_verifier(
            MockProvider(default=resp), "map", self._findings(), self._files(), SentinelConfig()
        )
        assert kept[0].severity == Severity.LOW

    async def test_failure_passes_findings_through(self):
        kept, usage, err = await run_verifier(
            FailingProvider(), "map", self._findings(), self._files(), SentinelConfig()
        )
        assert len(kept) == 1 and err is not None  # fail-open

    async def test_unparseable_passes_through(self):
        kept, usage, err = await run_verifier(
            MockProvider(default="I won't answer in JSON"), "map",
            self._findings(), self._files(), SentinelConfig()
        )
        assert len(kept) == 1 and err is not None

    async def test_empty_findings_no_call(self):
        provider = MockProvider()
        kept, usage, err = await run_verifier(
            provider, "map", [], self._files(), SentinelConfig()
        )
        assert kept == [] and provider.calls == []


class TestParseVerifier:
    def test_bare_array_tolerated(self):
        out = _parse_verifier_output('[{"id": 0, "verdict": "confirm"}]')
        assert out == {0: ("confirm", None)}

    def test_downgrade_without_severity_becomes_confirm(self):
        out = _parse_verifier_output('{"verdicts":[{"id":1,"verdict":"downgrade"}]}')
        assert out[1][0] == "confirm"
