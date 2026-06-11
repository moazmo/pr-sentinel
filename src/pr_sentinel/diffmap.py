"""Deterministic diff geometry (V2 A1): parse unified-diff patches into
hunks with absolute new-file line numbers.

Three consumers:
- chunking renders *numbered* hunks so analysts cite line numbers they can
  SEE instead of inferring them from `@@` headers (the single biggest
  localization win, borrowed from the category's best practice);
- verification anchors findings: an `evidence` line must literally exist in
  the line map or the finding is dropped (the hallucination killer);
- the publisher anchors inline review comments to verified diff lines.

Pure functions only — no I/O, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    tag: str  # "+", "-", " "
    text: str  # content WITHOUT the diff tag
    new_lineno: int | None  # None for removed lines
    old_lineno: int | None  # None for added lines


@dataclass
class Hunk:
    new_start: int
    old_start: int
    header_suffix: str = ""
    lines: list[DiffLine] = field(default_factory=list)


def parse_patch(patch: str) -> list[Hunk]:
    """Parse a per-file unified diff (GitHub `patch` field) into hunks.

    Tolerant by design: unknown lines (e.g. "\\ No newline at end of file")
    are skipped; a malformed header ends parsing of that patch rather than
    raising — a finding we can't anchor is better than a crashed review.
    """
    hunks: list[Hunk] = []
    current: Hunk | None = None
    new_no = old_no = 0
    for raw in patch.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            current = Hunk(new_start=new_no, old_start=old_no,
                           header_suffix=m.group(5).strip())
            hunks.append(current)
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            current.lines.append(DiffLine("+", raw[1:], new_no, None))
            new_no += 1
        elif raw.startswith("-"):
            current.lines.append(DiffLine("-", raw[1:], None, old_no))
            old_no += 1
        elif raw.startswith(" ") or raw == "":
            text = raw[1:] if raw.startswith(" ") else ""
            current.lines.append(DiffLine(" ", text, new_no, old_no))
            new_no += 1
            old_no += 1
        # anything else ("\\ No newline...") is ignored
    return hunks


def render_numbered(patch: str) -> str:
    """Render a patch with absolute new-file line numbers on every line the
    model may cite (+/context). Removed lines keep their `-` tag, unnumbered.

    Example output line:  `  42 + query = f"SELECT ..."`
    """
    out: list[str] = []
    for hunk in parse_patch(patch):
        out.append(f"@@ new file line {hunk.new_start} @@ {hunk.header_suffix}".rstrip())
        for line in hunk.lines:
            if line.new_lineno is not None:
                out.append(f"{line.new_lineno:>5} {line.tag} {line.text}")
            else:
                out.append(f"      - {line.text}")
    return "\n".join(out)


def line_text_map(patch: str) -> dict[int, str]:
    """new-file line number -> line text, for every added/context line."""
    mapping: dict[int, str] = {}
    for hunk in parse_patch(patch):
        for line in hunk.lines:
            if line.new_lineno is not None:
                mapping[line.new_lineno] = line.text
    return mapping


def added_line_numbers(patch: str) -> set[int]:
    """New-file line numbers of `+` lines only — the set GitHub will accept
    for RIGHT-side inline review comments."""
    numbers: set[int] = set()
    for hunk in parse_patch(patch):
        for line in hunk.lines:
            if line.tag == "+" and line.new_lineno is not None:
                numbers.add(line.new_lineno)
    return numbers


def extend_patch(patch: str, file_content: str, context_lines: int) -> str:
    """Extend each hunk with up to `context_lines` of surrounding code from
    the file's full content at the head ref (V2 A7).

    Rebuilds a valid unified diff whose extra lines are plain context, so
    parse_patch/render_numbered work on the result unchanged. Overlapping
    extended hunks are merged. Fails open: any inconsistency between the
    patch and the provided content returns the original patch untouched.
    """
    if context_lines <= 0 or not file_content:
        return patch
    src = file_content.splitlines()
    hunks = parse_patch(patch)
    if not hunks:
        return patch

    # Sanity: context/added lines in the patch must match the head content,
    # otherwise the content is from the wrong ref — keep the original.
    for hunk in hunks:
        for line in hunk.lines:
            if line.new_lineno is not None and line.tag in ("+", " "):
                idx = line.new_lineno - 1
                if idx >= len(src) or src[idx] != line.text:
                    return patch

    out: list[str] = []
    prev_end_new = 0  # last new-file line already emitted (for overlap merge)
    for hunk in hunks:
        new_lines = [ln.new_lineno for ln in hunk.lines if ln.new_lineno is not None]
        if not new_lines:
            out.extend(_render_hunk_raw(hunk))
            continue
        start = max(1, min(new_lines) - context_lines, prev_end_new + 1)
        end = min(len(src), max(new_lines) + context_lines)

        old_offset = hunk.old_start - hunk.new_start
        pre = [(n, src[n - 1]) for n in range(start, min(new_lines))]
        post = [(n, src[n - 1]) for n in range(max(new_lines) + 1, end + 1)]

        old_start = start + old_offset
        old_count = len(pre) + len(post) + sum(1 for ln in hunk.lines if ln.tag in ("-", " "))
        new_count = len(pre) + len(post) + sum(1 for ln in hunk.lines if ln.tag in ("+", " "))
        out.append(f"@@ -{old_start},{old_count} +{start},{new_count} @@ {hunk.header_suffix}".rstrip())
        out.extend(f" {text}" for _, text in pre)
        for line in hunk.lines:
            out.append(f"{line.tag}{line.text}")
        out.extend(f" {text}" for _, text in post)
        prev_end_new = end
    return "\n".join(out)


def _render_hunk_raw(hunk: Hunk) -> list[str]:
    lines = [f"@@ -{hunk.old_start} +{hunk.new_start} @@ {hunk.header_suffix}".rstrip()]
    lines.extend(f"{ln.tag}{ln.text}" for ln in hunk.lines)
    return lines
