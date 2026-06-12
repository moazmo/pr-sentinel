"""Large-diff strategy (D7): per-file review with a shared PR map.

- The PR map (title + full changed-file list with +/- counts) goes into every
  analyst prompt, restoring most cross-file awareness for ~200-500 tokens.
- Small files are batched into one call up to `tokens_per_call`; large files
  go alone; a single file over budget is truncated hunk-by-hunk (keep the
  earliest hunks) WITH explicit disclosure — silent truncation destroys trust.
- Global caps (max_files, max_input_tokens) bound the whole run; everything
  skipped is disclosed in the comment footer.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .config import SentinelConfig
from .diffmap import render_numbered
from .models import ChangedFile
from .provider import estimate_tokens
from .security import sanitize_for_prompt
from .skip_rules import skip_reason

_HUNK_HEADER = re.compile(r"^@@ .*@@", re.MULTILINE)

# Extensions that carry reviewable logic. Everything else still gets reviewed,
# just ranked below source code when the max_files cap forces choices.
_SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".swift", ".scala", ".sql",
    ".sh", ".ps1",
}


_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|[._]test\.|[._]spec\.")


def _is_test_path(path: str) -> bool:
    """True for real test files — segment/affix patterns, not a bare 'test'
    substring (which mis-tags `latest_handler.py`, `contest.py`) (F9)."""
    return bool(_TEST_PATH.search(path))


def file_priority(f: ChangedFile) -> float:
    """Ranking score for the max_files cap (V2 B3): when a PR exceeds the cap,
    keep the files where review matters most. Source > config > docs; bigger
    churn first (log-damped so one 2,000-line file doesn't outrank everything)."""
    path = f.path.lower()
    ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    if ext in _SOURCE_EXTENSIONS:
        weight = 0.6 if _is_test_path(path) else 1.0
    elif ext in (".yml", ".yaml", ".toml", ".json", ".tf"):
        weight = 0.5
    else:
        weight = 0.3
    return weight * math.log1p(f.additions + f.deletions)


@dataclass
class Chunk:
    """One analyst LLM call's worth of diff content."""

    files: list[ChangedFile] = field(default_factory=list)
    text: str = ""
    est_tokens: int = 0


def build_pr_map(pr_title: str, files: list[ChangedFile]) -> str:
    """Compact cross-file context shared by every analyst call.

    PR title is PR-controlled text — sanitized and clearly fenced as data.
    The PR body is deliberately excluded: it is the highest-volume injection
    surface and adds little to line-level review.
    """
    lines = [f"<pr_title>{sanitize_for_prompt(pr_title)[:300]}</pr_title>", "Changed files:"]
    for f in files:
        marker = " (skipped)" if f.skipped else ""
        lines.append(f"- {f.path} [{f.status}] +{f.additions}/-{f.deletions}{marker}")
    return "\n".join(lines)


def apply_skip_rules(files: list[ChangedFile], config: SentinelConfig) -> list[ChangedFile]:
    """Mark files that must not be reviewed; never drop them from the list —
    skipped files are disclosed in the output.

    When the max_files cap forces choices, the files KEPT are the highest
    review-priority ones (V2 B3), not merely the first ones the API returned.
    """
    candidates: list[ChangedFile] = []
    for f in files:
        reason = skip_reason(f.path, config.ignore)
        if reason is not None:
            f.skipped, f.skip_reason = True, reason
        elif f.patch is None:
            f.skipped, f.skip_reason = True, "binary or oversized diff (no patch from API)"
        elif f.status == "removed" and not config.review.include_deletions:
            f.skipped, f.skip_reason = True, "pure deletion (review.include_deletions: false)"
        else:
            candidates.append(f)

    if len(candidates) > config.limits.max_files:
        ranked = sorted(candidates, key=file_priority, reverse=True)
        for f in ranked[config.limits.max_files:]:
            f.skipped = True
            f.skip_reason = f"over max_files cap ({config.limits.max_files}, lowest review priority)"
    return files


def _truncate_patch(patch: str, token_budget: int) -> tuple[str, float]:
    """Keep whole hunks from the start until the budget is spent.
    Returns (truncated_patch, fraction_kept)."""
    headers = list(_HUNK_HEADER.finditer(patch))
    if len(headers) <= 1:
        char_budget = token_budget * 4
        kept = patch[:char_budget]
        return kept, len(kept) / max(1, len(patch))

    starts = [m.start() for m in headers] + [len(patch)]
    kept_parts: list[str] = []
    used = estimate_tokens(patch[: starts[0]])  # diff header before first hunk
    kept_parts.append(patch[: starts[0]])
    kept_hunks = 0
    for i in range(len(headers)):
        hunk = patch[starts[i] : starts[i + 1]]
        cost = estimate_tokens(hunk)
        if used + cost > token_budget and kept_hunks > 0:
            break
        kept_parts.append(hunk)
        used += cost
        kept_hunks += 1
    return "".join(kept_parts), kept_hunks / len(headers)


def _file_block(f: ChangedFile) -> str:
    """Render a file's patch with absolute new-file line numbers (V2 A1) so
    analysts cite numbers they can see, sanitized against delimiter escapes."""
    rename = f" (renamed from {f.previous_path})" if f.previous_path else ""
    numbered = render_numbered(f.patch or "")
    patch = sanitize_for_prompt(numbered if numbered else (f.patch or ""))
    return f'<file path="{f.path}" status="{f.status}"{rename}>\n{patch}\n</file>'


def build_chunks(files: list[ChangedFile], config: SentinelConfig) -> list[Chunk]:
    """Pack reviewable files into per-call chunks under the token budgets."""
    per_call = config.limits.tokens_per_call
    global_budget = config.limits.max_input_tokens
    used_global = 0

    chunks: list[Chunk] = []
    current = Chunk()

    for f in files:
        if f.skipped:
            continue
        block = _file_block(f)
        cost = estimate_tokens(block)

        if cost > per_call:
            # Truncation budgets against the raw patch, but cost is measured on
            # the line-numbered render (larger). Shrink-retry until the rendered
            # block actually fits, so per_call is a real ceiling, not a guess.
            overhead = estimate_tokens(_file_block(f.model_copy(update={"patch": ""})))
            original_patch = f.patch or ""
            keep_budget = per_call - overhead
            fraction = 1.0
            for _ in range(4):
                truncated, fraction = _truncate_patch(original_patch, max(50, keep_budget))
                f.patch = truncated
                block = _file_block(f)
                cost = estimate_tokens(block)
                if cost <= per_call:
                    break
                keep_budget = int(keep_budget * 0.7)
            f.truncated = True
            f.truncation_note = f"reviewed partially ({fraction:.0%} of hunks) due to size"

        if used_global + cost > global_budget:
            f.skipped = True
            f.skip_reason = f"over max_input_tokens cap ({global_budget})"
            continue

        if current.files and current.est_tokens + cost > per_call:
            chunks.append(current)
            current = Chunk()

        current.files.append(f)
        current.text += ("\n\n" if current.text else "") + block
        current.est_tokens += cost
        used_global += cost

    if current.files:
        chunks.append(current)
    return chunks
