"""Finding suppression (V2 P4): let authors permanently silence a false
positive, two ways —

1. **Config globs:** `review.suppress: ["legacy/**", "api/*.py:nit"]` drops
   findings by path (and optionally category) glob. Read from the base branch
   like all config, so a hostile PR can't suppress its own findings.
2. **Inline markers:** a `pr-sentinel: ignore` (optionally `ignore[category]`)
   comment on or just above the offending line, written in the diff itself.

All pure functions — the residual-false-positive escape hatch that keeps a
reviewer installed. Suppression runs after anchoring (so line numbers are
real) and before posting.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch

from .diffmap import line_text_map
from .models import ChangedFile, Finding

# `pr-sentinel: ignore` or `pr-sentinel: ignore[some-category]` anywhere in a
# comment. Tolerates #, //, --, /* */ comment leaders by not anchoring.
_IGNORE_MARKER = re.compile(r"pr-sentinel:\s*ignore(?:\[([^\]]+)\])?", re.IGNORECASE)

# An inline marker suppresses a finding on its own line or the next 1-2 lines
# (people write the pragma just above the code).
_MARKER_REACH = 2


def _config_suppresses(finding: Finding, patterns: list[str]) -> bool:
    for pattern in patterns:
        path_glob, _, cat_glob = pattern.partition(":")
        if not _path_matches(finding.file, path_glob.strip()):
            continue
        if not cat_glob.strip() or fnmatch(finding.category.lower(), cat_glob.strip().lower()):
            return True
    return False


def _path_matches(path: str, glob: str) -> bool:
    path = path.replace("\\", "/")
    glob = glob.replace("**", "*")
    return fnmatch(path, glob) or fnmatch(path, f"{glob}/*")


def _inline_suppresses(finding: Finding, line_map: dict[int, str]) -> bool:
    for lineno in range(finding.line_start, finding.line_start + _MARKER_REACH + 1):
        m = _IGNORE_MARKER.search(line_map.get(lineno, ""))
        if m is None:
            continue
        scoped = m.group(1)
        if not scoped:
            return True  # bare ignore silences anything here
        # ignore[category] only silences a matching category.
        if fnmatch(finding.category.lower(), scoped.strip().lower()):
            return True
    return False


def apply_suppressions(
    findings: list[Finding], files: list[ChangedFile], patterns: list[str]
) -> tuple[list[Finding], int]:
    """Drop suppressed findings. Returns (kept, suppressed_count)."""
    maps = {f.path: line_text_map(f.patch or "") for f in files}
    kept: list[Finding] = []
    suppressed = 0
    for finding in findings:
        if _config_suppresses(finding, patterns) or _inline_suppresses(
            finding, maps.get(finding.file, {})
        ):
            suppressed += 1
            continue
        kept.append(finding)
    return kept, suppressed
