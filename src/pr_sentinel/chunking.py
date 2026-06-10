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

import re
from dataclasses import dataclass, field

from .config import SentinelConfig
from .models import ChangedFile
from .provider import estimate_tokens
from .security import sanitize_for_prompt
from .skip_rules import skip_reason

_HUNK_HEADER = re.compile(r"^@@ .*@@", re.MULTILINE)


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
    skipped files are disclosed in the output."""
    kept = 0
    for f in files:
        reason = skip_reason(f.path, config.ignore)
        if reason is not None:
            f.skipped, f.skip_reason = True, reason
        elif f.patch is None:
            f.skipped, f.skip_reason = True, "binary or oversized diff (no patch from API)"
        elif f.status == "removed" and not config.review.include_deletions:
            f.skipped, f.skip_reason = True, "pure deletion (review.include_deletions: false)"
        else:
            kept += 1
            if kept > config.limits.max_files:
                f.skipped, f.skip_reason = True, f"over max_files cap ({config.limits.max_files})"
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
    rename = f" (renamed from {f.previous_path})" if f.previous_path else ""
    patch = sanitize_for_prompt(f.patch or "")
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
            overhead = estimate_tokens(_file_block(f.model_copy(update={"patch": ""})))
            keep_budget = per_call - overhead
            truncated, fraction = _truncate_patch(f.patch or "", max(50, keep_budget))
            f.truncated = True
            f.truncation_note = f"reviewed partially ({fraction:.0%} of hunks) due to size"
            f.patch = truncated
            block = _file_block(f)
            cost = estimate_tokens(block)

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
