"""SAST grounding (L1): the pure Semgrep-JSON -> Finding parser. The subprocess
runner and temp-tree orchestration are live-path only (need Semgrep installed +
head-ref file contents), so we unit-test the deterministic parsing here."""

from __future__ import annotations

import json

from pr_sentinel.models import AgentName, Severity
from pr_sentinel.sast import parse_semgrep_json


def _semgrep(path, line, severity, check_id, message="bad"):
    return json.dumps(
        {
            "results": [
                {
                    "check_id": check_id,
                    "path": path,
                    "start": {"line": line},
                    "end": {"line": line},
                    "extra": {"severity": severity, "message": message},
                }
            ]
        }
    )


ADDED = {"app/api.py": {10, 11, 12}}
LINETEXT = {"app/api.py": {11: "    os.system(user_input)"}}


class TestParseSemgrep:
    def test_hit_on_added_line_becomes_finding(self):
        raw = _semgrep("app/api.py", 11, "ERROR", "python.lang.security.dangerous-os-system")
        out = parse_semgrep_json(raw, ADDED, LINETEXT)
        assert len(out) == 1
        f = out[0]
        assert f.agent == AgentName.SECURITY
        assert f.severity == Severity.HIGH  # ERROR -> high
        assert f.category == "sast-dangerous-os-system"
        assert "Semgrep" in f.message
        assert f.evidence == "os.system(user_input)"  # stripped, from line_text
        assert f.line_start == 11

    def test_hit_off_added_lines_dropped(self):
        # Line 50 is not in the added set -> pre-existing, not this PR's doing.
        raw = _semgrep("app/api.py", 50, "ERROR", "x.y.z")
        assert parse_semgrep_json(raw, ADDED, LINETEXT) == []

    def test_hit_without_quotable_evidence_dropped(self):
        # Added line 12 has no line_text entry -> can't anchor -> drop.
        raw = _semgrep("app/api.py", 12, "ERROR", "x.y.z")
        assert parse_semgrep_json(raw, ADDED, LINETEXT) == []

    def test_severity_mapping(self):
        raw = _semgrep("app/api.py", 11, "WARNING", "x.y.medium-rule")
        assert parse_semgrep_json(raw, ADDED, LINETEXT)[0].severity == Severity.MEDIUM
        raw = _semgrep("app/api.py", 11, "INFO", "x.y.low-rule")
        assert parse_semgrep_json(raw, ADDED, LINETEXT)[0].severity == Severity.LOW

    def test_malformed_json_is_empty(self):
        assert parse_semgrep_json("not json", ADDED, LINETEXT) == []
        assert parse_semgrep_json("", ADDED, LINETEXT) == []

    def test_no_results_is_empty(self):
        assert parse_semgrep_json(json.dumps({"results": []}), ADDED, LINETEXT) == []
