"""Evidence anchoring (V2 A2): the hallucination killer."""

import asyncio

from pr_sentinel.config import SentinelConfig
from pr_sentinel.models import ChangedFile, Finding
from pr_sentinel.provider import CompletionResult
from pr_sentinel.verification import anchor_finding, anchor_findings

PATCH = (
    "@@ -1,3 +1,5 @@\n"
    " import os\n"
    "+conn = get_connection()\n"
    "+query = f\"SELECT * FROM users WHERE name = {name}\"\n"
    "+rows = conn.execute(query).fetchall()\n"
)


def make_finding(**kw):
    base = dict(
        agent="security", file="api.py", line_start=3, line_end=3,
        severity="high", category="sql-injection", message="injection",
        evidence='query = f"SELECT * FROM users WHERE name = {name}"',
    )
    base.update(kw)
    return Finding(**base)


def line_map():
    from pr_sentinel.diffmap import line_text_map
    return line_text_map(PATCH)


class TestAnchorFinding:
    def test_exact_evidence_kept(self):
        anchored = anchor_finding(make_finding(), line_map())
        assert anchored is not None
        assert anchored.line_start == 3

    def test_wrong_line_reanchored_to_real_location(self):
        # Evidence is real but the model put the wrong line number.
        anchored = anchor_finding(make_finding(line_start=99, line_end=99), line_map())
        assert anchored is not None
        assert anchored.line_start == 3  # snapped to where the evidence lives

    def test_hallucinated_evidence_dropped(self):
        f = make_finding(evidence="os.system(user_input)  # not in the diff")
        assert anchor_finding(f, line_map()) is None

    def test_empty_evidence_dropped(self):
        assert anchor_finding(make_finding(evidence=None), line_map()) is None
        assert anchor_finding(make_finding(evidence="ab"), line_map()) is None

    def test_whitespace_insensitive_match(self):
        f = make_finding(evidence='query=f"SELECT * FROM users WHERE name = {name}"')
        assert anchor_finding(f, line_map()) is not None


class TestAnchorFindings:
    def _files(self):
        return [ChangedFile(path="api.py", status="modified", patch=PATCH)]

    def test_keeps_real_drops_fake(self):
        findings = [
            make_finding(),
            make_finding(category="made-up", evidence="this line does not exist anywhere"),
        ]
        kept, dropped = anchor_findings(findings, self._files())
        assert len(kept) == 1 and dropped == 1

    def test_finding_for_unknown_file_dropped(self):
        kept, dropped = anchor_findings([make_finding(file="ghost.py")], self._files())
        assert kept == [] and dropped == 1

    def test_finding_for_skipped_file_dropped(self):
        files = self._files()
        files[0].skipped = True
        kept, dropped = anchor_findings([make_finding()], files)
        assert kept == [] and dropped == 1


class _AspectProvider:
    """One aspect pass (the GROUNDING rubric) rejects finding id 0; every other
    pass confirms both — so any-reject MAV must drop finding 0 and keep finding 1."""

    async def complete(self, system, user, *, max_tokens, temperature=0.1,
                       model=None, json_mode=False, thinking=None, reasoning_effort=None):
        if "GROUNDING" in user:
            text = '{"verdicts":[{"id":0,"verdict":"reject"},{"id":1,"verdict":"confirm"}]}'
        else:
            text = '{"verdicts":[{"id":0,"verdict":"confirm"},{"id":1,"verdict":"confirm"}]}'
        return CompletionResult(text=text, prompt_tokens=10, completion_tokens=5)


class TestMAVVerifier:
    """MAV (D45): multiple aspect passes, any-reject combine."""

    def _setup(self, aspects: int):
        from pr_sentinel.agents import run_verifier

        files = [ChangedFile(path="api.py", status="modified", patch=PATCH)]
        findings = [
            make_finding(category="sql-injection"),
            make_finding(category="other", line_start=4, line_end=4,
                         evidence="rows = conn.execute(query).fetchall()"),
        ]
        cfg = SentinelConfig()
        cfg.accuracy.verifier_aspects = aspects
        kept, _usage, err = asyncio.run(
            run_verifier(_AspectProvider(), "pr", findings, files, cfg)
        )
        return kept, err

    def test_any_reject_drops_finding_refuted_by_one_aspect(self):
        kept, err = self._setup(aspects=3)
        assert err is None
        cats = {f.category for f in kept}
        assert "sql-injection" not in cats  # GROUNDING pass rejected id 0
        assert "other" in cats              # id 1 confirmed by all → survives

    def test_single_aspect_unchanged_keeps_both(self):
        # aspects=1 runs only the base rubric (no GROUNDING pass) → nothing rejected.
        kept, err = self._setup(aspects=1)
        assert err is None
        assert len(kept) == 2
