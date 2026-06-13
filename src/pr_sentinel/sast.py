"""SAST grounding (research lever L1): run a deterministic scanner (Semgrep)
over the changed files and feed its hits into the pipeline as candidate findings
that the verifier then triages against the diff.

Why: the bugs a cheap LLM *misses* (a hardcoded key under a calm title, a
text-book injection) are exactly what rule engines catch every time; the noise a
rule engine produces is exactly what an LLM triage layer filters. Pairing the two
is the best-documented precision lever in the 2025-26 literature (SAST-Genius:
Semgrep 225 -> 20 false positives; OWASP 560 -> 64; ~2.5x detection). This module
is the "produce candidates" half; the verifier (prompts/verifier.md) is the triage
half — Semgrep hits enter `state["findings"]` like any analyst finding and go
through evidence anchoring + the rubric verifier before a human sees them.

Design constraints preserved:
- No `actions/checkout`: we reuse the head-ref file contents the context pass
  already fetches (D2 stays — diff comes from the API), write them to a temp tree,
  and scan that. Live-path only (the static-fixture eval harness has no files).
- Only hits on ADDED lines are kept — issues this PR introduced, not pre-existing
  debt — mirroring evidence anchoring.
- Fail-open: a missing/failed Semgrep never aborts the review.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from .models import AgentName, Finding, Severity

logger = logging.getLogger(__name__)

# Semgrep severity -> our severity. Deliberately conservative: the verifier can
# downgrade, and a confirmed real bug is worth more than a precise initial label.
_SEV = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}


def _short_check(check_id: str) -> str:
    """`python.lang.security.audit.dangerous-eval` -> `dangerous-eval`."""
    tail = check_id.rsplit(".", 1)[-1] if check_id else "semgrep"
    return tail[:80] or "semgrep"


def parse_semgrep_json(
    raw: str,
    added_lines: dict[str, set[int]],
    line_text: dict[str, dict[int, str]],
) -> list[Finding]:
    """Turn Semgrep JSON into Findings, keeping only hits on added lines whose
    evidence line we can quote (so anchoring keeps them). Pure + unit-tested."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    findings: list[Finding] = []
    for r in data.get("results", []):
        if not isinstance(r, dict):
            continue
        path = r.get("path", "")
        start = (r.get("start") or {}).get("line", 0)
        end = (r.get("end") or {}).get("line", start)
        added = added_lines.get(path, set())
        # Keep the hit only if its range touches a line this PR added.
        touched = [n for n in range(start, end + 1) if n in added]
        if not touched:
            continue
        anchor = min(touched)
        evidence = (line_text.get(path, {}) or {}).get(anchor, "").strip()
        if len(evidence) < 4:
            continue
        extra = r.get("extra") or {}
        sev = _SEV.get(str(extra.get("severity", "")).upper(), Severity.MEDIUM)
        check = _short_check(r.get("check_id", ""))
        msg = (extra.get("message") or "Static-analysis finding.").strip()
        try:
            findings.append(
                Finding(
                    agent=AgentName.SECURITY,
                    file=path,
                    line_start=anchor,
                    line_end=anchor,
                    severity=sev,
                    category=f"sast-{check}"[:80],
                    message=f"{msg} (Semgrep: {check})"[:1900],
                    evidence=evidence[:500],
                )
            )
        except Exception:
            logger.warning("Dropped malformed Semgrep finding for %s.", path)
    return findings


def run_semgrep_cli(workdir: str, rules: str = "auto", timeout: float = 120.0) -> str | None:
    """Run Semgrep over `workdir`, returning raw JSON (None on any failure).
    The scanner is optional infrastructure — absence degrades to no SAST."""
    try:
        proc = subprocess.run(
            ["semgrep", "--config", rules, "--json", "--quiet", "--no-git-ignore", workdir],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.info("Semgrep unavailable or timed out (%s); skipping SAST.", type(exc).__name__)
        return None
    # Semgrep exits non-zero when it finds issues; stdout still holds the JSON.
    return proc.stdout or None


def semgrep_findings(
    contents: dict[str, str],
    added_lines: dict[str, set[int]],
    line_text: dict[str, dict[int, str]],
    rules: str = "auto",
) -> list[Finding]:
    """Write the head contents to a temp tree, scan, and parse. `contents` maps
    path -> full file text (from the head ref the context pass already fetched)."""
    if not contents:
        return []
    with tempfile.TemporaryDirectory(prefix="pr-sentinel-sast-") as tmp:
        root = Path(tmp)
        for path, text in contents.items():
            dest = root / path
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            except OSError:
                continue
        raw = run_semgrep_cli(str(root), rules=rules)
        if not raw:
            return []
        # Semgrep paths are relative to the workdir; normalize to repo-relative.
        normalized = raw.replace(str(root).replace("\\", "/") + "/", "").replace(str(root) + "\\", "")
        return parse_semgrep_json(normalized, added_lines, line_text)
