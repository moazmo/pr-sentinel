"""Wave 1 features: suppression (P4), auto-fix suggestions (P1),
check run + gating (P2), incremental review (P3), presets (P6)."""

import json

import httpx

from pr_sentinel.config import SentinelConfig, load_config
from pr_sentinel.formatter import format_inline_body
from pr_sentinel.models import ChangedFile, Finding
from pr_sentinel.suppression import apply_suppressions


def finding(file="api.py", start=3, category="sql-injection", **kw):
    base = dict(agent="security", file=file, line_start=start, line_end=start,
                severity="high", category=category, message="m", evidence="bad")
    base.update(kw)
    return Finding(**base)


# ---- P4: suppression ------------------------------------------------------

PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+danger = eval(x)  # pr-sentinel: ignore\n"
    "+other = run(y)\n"
)


class TestSuppression:
    def _files(self):
        return [ChangedFile(path="api.py", status="modified", patch=PATCH)]

    def test_config_path_glob_suppresses(self):
        kept, n = apply_suppressions([finding(file="legacy/old.py")],
                                     [ChangedFile(path="legacy/old.py", status="modified")],
                                     ["legacy/**"])
        assert kept == [] and n == 1

    def test_config_path_and_category(self):
        findings = [finding(category="nit"), finding(category="sql-injection")]
        files = [ChangedFile(path="api.py", status="modified")]
        kept, n = apply_suppressions(findings, files, ["api.py:nit"])
        assert n == 1 and kept[0].category == "sql-injection"

    def test_inline_marker_suppresses_on_that_line(self):
        # 'danger = eval(x)' is new-file line 2 and carries the ignore marker.
        kept, n = apply_suppressions([finding(start=2)], self._files(), [])
        assert kept == [] and n == 1

    def test_inline_marker_does_not_suppress_other_lines(self):
        kept, n = apply_suppressions([finding(start=3)], self._files(), [])
        assert len(kept) == 1 and n == 0

    def test_scoped_inline_marker_only_matching_category(self):
        patch = "@@ -1 +1,2 @@\n+x = 1  # pr-sentinel: ignore[nit]\n"
        files = [ChangedFile(path="a.py", status="modified", patch=patch)]
        nit = finding(file="a.py", start=1, category="nit")
        sec = finding(file="a.py", start=1, category="sql-injection")
        kept, n = apply_suppressions([nit, sec], files, [])
        assert n == 1 and kept[0].category == "sql-injection"


# ---- P1: auto-fix suggestion blocks ---------------------------------------

class TestSuggestionBlocks:
    def test_single_line_fix_renders_suggestion_block(self):
        f = finding(fix='    return conn.execute(q, (uid,))')
        body = format_inline_body(f, suggestions=True)
        assert "```suggestion\n    return conn.execute(q, (uid,))\n```" in body

    def test_multiline_finding_falls_back_to_prose_block(self):
        f = finding(line_end=5, fix="a\nb", suggestion="rewrite")
        body = format_inline_body(f, suggestions=True)
        assert "```suggestion" not in body
        assert "Suggested fix" in body

    def test_suggestions_disabled_uses_prose(self):
        f = finding(fix="x = 1", suggestion="set x")
        body = format_inline_body(f, suggestions=False)
        assert "```suggestion" not in body and "set x" in body


# ---- P6: presets ----------------------------------------------------------

class TestPresets:
    def test_fast_preset(self):
        c = load_config("mode: fast")
        assert c.accuracy.samples == 1 and c.accuracy.verifier is False

    def test_thorough_preset(self):
        c = load_config("mode: thorough")
        assert c.accuracy.samples == 3 and c.accuracy.verifier is True

    def test_no_mode_keeps_defaults(self):
        c = SentinelConfig()
        assert c.accuracy.samples == 3


# ---- P2: check run + gating (client) --------------------------------------

class TestCheckRunClient:
    async def test_create_check_run_posts(self, monkeypatch):
        from pr_sentinel.github_client import GitHubClient

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        gh = GitHubClient("t", "octo/demo")
        gh._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ok = await gh.create_check_run(
            "abc123", conclusion="failure", title="t", summary="s",
            annotations=[{"path": "a.py", "start_line": 1, "end_line": 1,
                          "annotation_level": "failure", "message": "x"}],
        )
        await gh.aclose()
        assert ok and captured["path"].endswith("/check-runs")
        assert captured["body"]["conclusion"] == "failure"
        assert captured["body"]["output"]["annotations"][0]["path"] == "a.py"


# ---- P3: incremental (client) ---------------------------------------------

class TestIncrementalClient:
    async def test_last_reviewed_sha_parsed_from_marker(self, monkeypatch):
        from pr_sentinel.github_client import GitHubClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"id": 9, "body": "## review\n<!-- pr-sentinel-marker -->\n"
                                  "<!-- pr-sentinel-sha:deadbeef1234 -->"},
            ])

        gh = GitHubClient("t", "octo/demo")
        gh._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sha = await gh.last_reviewed_sha(1)
        await gh.aclose()
        assert sha == "deadbeef1234"

    async def test_compare_changed_paths(self):
        from pr_sentinel.github_client import GitHubClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"files": [
                {"filename": "a.py"}, {"filename": "b.py"}]})

        gh = GitHubClient("t", "octo/demo")
        gh._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        changed = await gh.compare_changed_paths("old", "new")
        await gh.aclose()
        assert changed == {"a.py", "b.py"}
