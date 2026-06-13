"""V2.5 research levers (RESEARCH_SYNTHESIS_2026-06-12): debiasing, calibration,
CoT, prompt-diverse ensemble lenses, and the rubric verifier. Every lever is a
config toggle so the eval A/B is pure config; these tests pin the wiring."""

from __future__ import annotations

import json

from pr_sentinel.agents import (
    analyst_system_prompt,
    load_prompt,
    parse_findings,
    run_analyst,
)
from pr_sentinel.chunking import apply_skip_rules, build_chunks, build_pr_map
from pr_sentinel.config import SentinelConfig, load_config
from pr_sentinel.models import AgentName
from tests.conftest import MockProvider, make_file, single_sample_config


def _chunks_and_map(config):
    files = apply_skip_rules([make_file()], config)
    return build_chunks(files, config), build_pr_map("t", files)


class TestDefaults:
    def test_lever_defaults_all_off(self):
        # Measured ≈ baseline on flash, so levers ship off by default (D29).
        acc = SentinelConfig().accuracy
        assert acc.debias is False
        assert acc.calibration is False
        assert acc.cot == "off"
        assert acc.lenses is False

    def test_thorough_preset_enables_all_levers(self):
        acc = load_config("mode: thorough").accuracy
        assert acc.debias is True and acc.calibration is True
        assert acc.lenses is True and acc.cot == "brief"

    def test_fast_preset_disables_them(self):
        acc = load_config("mode: fast").accuracy
        assert acc.debias is False and acc.calibration is False
        assert acc.lenses is False and acc.cot == "off"


class TestDebias:
    def test_debias_text_present_only_when_on(self):
        on = analyst_system_prompt(AgentName.SECURITY, debias=True)
        off = analyst_system_prompt(AgentName.SECURITY, debias=False)
        assert "Judge the code, not the story" in on
        assert "Judge the code, not the story" not in off

    def test_debias_default_off_in_helper(self):
        # The helper defaults to off; run_analyst passes the config value.
        assert "Judge the code" not in analyst_system_prompt(AgentName.SECURITY)


class TestCalibration:
    def test_per_agent_anchor_present_when_on(self):
        sec = analyst_system_prompt(AgentName.SECURITY, calibration=True)
        perf = analyst_system_prompt(AgentName.PERFORMANCE, calibration=True)
        assert "Calibration (how a careful" in sec
        assert "sql-injection" in sec  # security-specific anchor
        assert "n-plus-one" in perf    # performance-specific anchor
        assert "n-plus-one" not in sec  # agents get their own anchors only

    def test_absent_when_off(self):
        assert "Calibration (how a careful" not in analyst_system_prompt(
            AgentName.SECURITY, calibration=False
        )


class TestCachePrefixOrdering:
    def test_stable_blocks_precede_variable_blocks(self):
        # L5: calibration + debias sit in the cacheable prefix, BEFORE the
        # per-repo language hint / guidance suffix.
        prompt = analyst_system_prompt(
            AgentName.SECURITY, language_hint="python", guidance="be terse",
            debias=True, calibration=True,
        )
        assert prompt.index("Calibration (how a careful") < prompt.index("python")
        assert prompt.index("Judge the code") < prompt.index("python")
        assert prompt.index("python") < prompt.index("be terse")


class TestCoT:
    def test_cot_instruction_present_when_brief(self):
        assert "analysis" in analyst_system_prompt(AgentName.TEST, cot="brief")
        assert "Think before you commit" in analyst_system_prompt(
            AgentName.TEST, cot="brief"
        )

    def test_cot_off_by_default(self):
        assert "Think before you commit" not in analyst_system_prompt(AgentName.TEST)

    def test_parser_ignores_analysis_key(self):
        # CoT emits {"analysis": "...", "findings": [...]}; only findings survive.
        raw = json.dumps(
            {
                "analysis": "This diff concatenates user input into SQL.",
                "findings": [
                    {
                        "file": "app.py", "line_start": 1, "line_end": 1,
                        "severity": "high", "category": "sql-injection",
                        "message": "Injection.", "evidence": "x = 1",
                    }
                ],
            }
        )
        findings = parse_findings(raw, AgentName.SECURITY)
        assert len(findings) == 1 and findings[0].category == "sql-injection"


class TestLenses:
    async def test_distinct_lenses_per_sample_when_on(self):
        config = SentinelConfig()
        config.accuracy.adaptive = False  # draw all 3 so we can inspect each
        config.accuracy.lenses = True
        chunks, pr_map = _chunks_and_map(config)
        provider = MockProvider()  # all clean -> 3 calls fire
        await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        users = [c["user"] for c in provider.calls]
        assert len(users) == 3
        blob = "\n".join(users)
        assert "checklist sweep" in blob
        assert "adversarial auditor" in blob
        # The system prompt is identical across samples (lens rides the user
        # message) so the big cached prefix still hits.
        systems = {c["system"] for c in provider.calls}
        assert len(systems) == 1

    async def test_no_lens_text_when_off(self):
        config = SentinelConfig()
        config.accuracy.adaptive = False
        config.accuracy.lenses = False
        chunks, pr_map = _chunks_and_map(config)
        provider = MockProvider()
        await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        blob = "\n".join(c["user"] for c in provider.calls)
        assert "LENS FOR THIS PASS" not in blob

    async def test_lenses_inert_with_single_sample(self):
        config = SentinelConfig()
        config.accuracy.samples = 1
        config.accuracy.min_support = 1
        config.accuracy.lenses = True
        chunks, pr_map = _chunks_and_map(config)
        provider = MockProvider()
        await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        assert "LENS FOR THIS PASS" not in provider.calls[0]["user"]


class TestVerifierRubric:
    def test_rubric_prompt_loaded(self):
        prompt = load_prompt("verifier")
        assert "Argue the rejection first" in prompt
        assert '"verdicts"' in prompt  # output schema unchanged


class _CapturingClient:
    """Minimal httpx.AsyncClient stand-in that captures the JSON payload."""

    def __init__(self):
        self.payloads = []

    is_closed = False

    async def post(self, url, json, headers):
        self.payloads.append(json)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "[]"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return _Resp()


class TestReasoningControls:
    def test_config_defaults(self):
        acc = SentinelConfig().accuracy
        assert acc.analyst_thinking is None  # leave provider default (DeepSeek = on)
        assert acc.reasoning_effort == ""

    async def test_payload_omits_thinking_when_none(self):
        from pr_sentinel.provider import OpenAICompatProvider

        p = OpenAICompatProvider("k")
        p._client = _CapturingClient()
        await p.complete("s", "u", max_tokens=10, thinking=None)
        assert "thinking" not in p._client.payloads[0]  # endpoint-safe default

    async def test_payload_disables_thinking(self):
        from pr_sentinel.provider import OpenAICompatProvider

        p = OpenAICompatProvider("k")
        p._client = _CapturingClient()
        await p.complete("s", "u", max_tokens=10, thinking=False)
        assert p._client.payloads[0]["thinking"] == {"type": "disabled"}

    async def test_payload_enables_thinking_with_effort(self):
        from pr_sentinel.provider import OpenAICompatProvider

        p = OpenAICompatProvider("k")
        p._client = _CapturingClient()
        await p.complete("s", "u", max_tokens=10, thinking=True, reasoning_effort="high")
        assert p._client.payloads[0]["thinking"] == {"type": "enabled"}
        assert p._client.payloads[0]["reasoning_effort"] == "high"

    async def test_run_analyst_appends_repo_context(self):
        config = single_sample_config()
        chunks, pr_map = _chunks_and_map(config)
        provider = MockProvider()
        await run_analyst(
            AgentName.SECURITY, provider, pr_map, chunks, config,
            repo_context="<repo_context>\ndef helper():\n    pass\n</repo_context>",
        )
        assert "<repo_context>" in provider.calls[0]["user"]
        # Context sits after the diff so the diff stays the focus.
        assert provider.calls[0]["user"].index("<diff>") < provider.calls[0]["user"].index("<repo_context>")

    async def test_run_analyst_passes_thinking_from_config(self):
        config = single_sample_config()
        config.accuracy.analyst_thinking = False
        config.accuracy.reasoning_effort = "low"
        # reasoning_effort only rides along when thinking is enabled, so also
        # assert the thinking flag is forwarded verbatim.
        chunks, pr_map = _chunks_and_map(config)
        provider = MockProvider()
        await run_analyst(AgentName.SECURITY, provider, pr_map, chunks, config)
        assert provider.calls[0]["thinking"] is False
        assert provider.calls[0]["reasoning_effort"] == "low"
