"""Evidence anchoring (V2 A2): a finding must point at code that literally
exists in the diff, or it does not get posted.

Analysts are required to quote the offending line in `evidence`. These pure
functions check that quote against the deterministic line map from diffmap:

- exact/normalized match near the claimed line range -> keep, snap the range
  to the real location ("re-anchoring" — fixes off-by-N localization);
- match elsewhere in the same file's patch -> keep, MOVE the finding there;
- no match anywhere -> drop and count it.

This turns hallucinated findings from a prompt-discipline problem into a
structural impossibility, and makes every surviving line number trustworthy
enough to hang an inline review comment on.
"""

from __future__ import annotations

import logging

from .diffmap import line_text_map
from .models import ChangedFile, Finding

logger = logging.getLogger(__name__)

# How far from the claimed range we search before falling back to whole-file.
NEAR_WINDOW = 3


def _normalize(text: str) -> str:
    return "".join(text.split())


def anchor_finding(finding: Finding, line_map: dict[int, str]) -> Finding | None:
    """Return the finding with a verified, possibly corrected line range —
    or None when its evidence doesn't exist in the diff."""
    evidence = _normalize(finding.evidence or "")
    if len(evidence) < 4:  # empty or too short to mean anything
        return None

    def matches(lineno: int) -> bool:
        line = _normalize(line_map.get(lineno, ""))
        return bool(line) and (evidence in line or line in evidence)

    # 1) Near the claimed range.
    lo = max(1, finding.line_start - NEAR_WINDOW)
    hi = finding.line_end + NEAR_WINDOW
    near_hits = [n for n in range(lo, hi + 1) if matches(n)]
    if near_hits:
        anchored = finding.model_copy(deep=True)
        anchored.line_start = min(near_hits)
        anchored.line_end = max(near_hits)
        return anchored

    # 2) Anywhere in the file's patch: trust the evidence, fix the location.
    far_hits = sorted(n for n in line_map if matches(n))
    if far_hits:
        anchored = finding.model_copy(deep=True)
        anchored.line_start = far_hits[0]
        anchored.line_end = far_hits[0]
        return anchored

    return None


def anchor_findings(
    findings: list[Finding], files: list[ChangedFile]
) -> tuple[list[Finding], int]:
    """Anchor every finding against its file's line map.

    Returns (kept_findings, dropped_count). A finding for an unknown or
    skipped file is dropped too — there is nothing to anchor it to.
    """
    maps: dict[str, dict[int, str]] = {
        f.path: line_text_map(f.patch or "") for f in files if not f.skipped
    }
    kept: list[Finding] = []
    dropped = 0
    for finding in findings:
        line_map = maps.get(finding.file)
        if not line_map:
            dropped += 1
            continue
        anchored = anchor_finding(finding, line_map)
        if anchored is None:
            dropped += 1
            logger.info(
                "Dropped unanchorable %s finding at %s:%d (evidence not in diff).",
                finding.agent.value, finding.file, finding.line_start,
            )
            continue
        kept.append(anchored)
    return kept, dropped
