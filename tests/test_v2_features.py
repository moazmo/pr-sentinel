"""Remaining V2 surface: file ranking, inline review client, describe formatting,
provider json-mode fallback + Anthropic, config parsing of the new blocks."""

import json

import httpx

from pr_sentinel.chunking import apply_skip_rules, file_priority
from pr_sentinel.config import SentinelConfig, load_config
from pr_sentinel.formatter import format_description, format_inline_body
from pr_sentinel.models import ChangedFile, Finding
from pr_sentinel.provider import AnthropicProvider, OpenAICompatProvider


def cf(path, adds=10):
    return ChangedFile(path=path, status="modified", additions=adds, deletions=0,
                       patch="@@ -1 +1 @@\n+x\n")


class TestFileRanking:
    def test_source_outranks_docs(self):
        assert file_priority(cf("app.py")) > file_priority(cf("README.md"))

    def test_tests_downweighted_vs_source(self):
        assert file_priority(cf("test_app.py")) < file_priority(cf("app.py", adds=10))

    def test_test_path_detection_is_not_substring(self):
        # F9: 'latest'/'contest' contain 'test' but are not test files.
        from pr_sentinel.chunking import _is_test_path

        assert _is_test_path("tests/test_app.py")
        assert _is_test_path("app/foo_test.go")
        assert _is_test_path("web/app.test.ts")
        assert not _is_test_path("services/latest_handler.py")
        assert not _is_test_path("game/contest.py")

    def test_more_churn_ranks_higher(self):
        assert file_priority(cf("a.py", adds=200)) > file_priority(cf("b.py", adds=2))

    def test_cap_keeps_highest_priority_files(self):
        config = SentinelConfig()
        config.limits.max_files = 2
        files = [cf("README.md", 5), cf("core.py", 50), cf("api.py", 80), cf("notes.txt", 3)]
        apply_skip_rules(files, config)
        kept = {f.path for f in files if not f.skipped}
        assert kept == {"core.py", "api.py"}  # source files win the 2 slots


class TestInlineBody:
    def test_contains_severity_attribution_message(self):
        finding = Finding(agent="security", file="a.py", line_start=3, line_end=3,
                          severity="critical", category="sqli", message="bad query",
                          suggestion="parameterize", evidence="q")
        body = format_inline_body(finding)
        assert "Critical" in body and "Security" in body
        assert "bad query" in body and "parameterize" in body
        assert "PR Sentinel" in body


class TestDescribeFormatting:
    def test_renders_summary_type_walkthrough(self):
        out = format_description({
            "summary": "Adds search.", "type": "feature",
            "walkthrough": [{"file": "api.py", "change": "new endpoint"}],
        })
        assert "Adds search." in out and "feature" in out
        assert "`api.py`" in out and "new endpoint" in out

    def test_tolerates_missing_walkthrough(self):
        out = format_description({"summary": "x", "type": "fix"})
        assert "x" in out


class TestConfigV2:
    def test_accuracy_defaults(self):
        c = SentinelConfig()
        assert c.accuracy.samples == 3
        assert c.accuracy.min_support == 2
        assert c.accuracy.verifier is True
        assert c.output.inline is True
        assert c.review.context_lines == 8

    def test_two_tier_model_routing_parsed(self):
        c = load_config(
            "provider:\n  model: deepseek-v4-pro\n"
            "  analyst_model: deepseek-v4-flash\n"
        )
        assert c.provider.resolved_analyst_model == "deepseek-v4-flash"
        assert c.provider.resolved_review_model == "deepseek-v4-pro"

    def test_model_defaults_when_unset(self):
        c = load_config("provider:\n  model: gpt-5-mini\n")
        assert c.provider.resolved_analyst_model == "gpt-5-mini"
        assert c.provider.resolved_review_model == "gpt-5-mini"

    def test_accuracy_block_parsed(self):
        c = load_config("accuracy:\n  samples: 1\n  verifier: false\n")
        assert c.accuracy.samples == 1
        assert c.accuracy.verifier is False


class TestProviderJsonMode:
    async def test_json_mode_sent_then_falls_back_on_400(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append("response_format" in body)
            if "response_format" in body:
                return httpx.Response(400, json={"error": "unsupported"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{\"findings\": []}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            })

        provider = OpenAICompatProvider("k", base_url="https://x/v1")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await provider.complete("s", "u", max_tokens=10, json_mode=True)
        assert calls == [True, False]  # tried json, fell back
        assert provider._json_mode_supported is False
        assert result.text == '{"findings": []}'

    async def test_cached_tokens_parsed(self):
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "prompt_cache_hit_tokens": 70},
            })

        provider = OpenAICompatProvider("k")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await provider.complete("s", "u", max_tokens=10)
        assert result.cached_tokens == 70


class TestAnthropicProvider:
    async def test_messages_api_shape(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 8, "output_tokens": 2},
            })

        provider = AnthropicProvider("ak", model="claude-haiku-4-5-20251001")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await provider.complete("sys", "usr", max_tokens=10)
        assert captured["path"] == "/v1/messages"
        assert captured["headers"]["x-api-key"] == "ak"
        assert captured["headers"]["anthropic-version"]
        assert result.text == "hello"
        assert result.prompt_tokens == 8 and result.completion_tokens == 2
