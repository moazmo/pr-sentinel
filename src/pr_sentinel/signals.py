"""Compact structured signals (Lever A, D46) — the redemption of repo_context.

`repo_context` injected RAW symbol *definitions* (full bodies) and gained only +3pp (D37):
the model had to read 1000 lines to find one fact, and the bulk diluted attention. The
2024–2026 literature is unanimous (Lu et al. ICML 2025; SWE-PRBench; RepoAudit): cross-file
context helps a cheap model ONLY when it is **compact, structured, and targeted** — facts, not
bodies. "Compression beats expansion."

This module extracts the highest-signal facts that are derivable from the DIFF ALONE (no repo
fetch, so they're measurable on the static benchmark and add zero round-trips on the live path):

- **Removed guards** — deleted `if`/`assert`/`except`/`raise`/early-return/None-check lines: the
  "removed workaround / dropped guard" miss class, the single most cited context-dependent defect.
- **Contract changes** — a function whose signature (params) or return-type annotation changed
  between the `-` and `+` sides: the "typing regression / caller-contract violation" miss class.

The card is a small (<600-char) delimited `<impact>` block the analysts see ALONGSIDE the diff —
an attention director, not extra context to wade through. PR-controlled data → sanitized + the
`impact` tag added to the delimiter scrubber. Deterministic, pure, fail-open (empty card = no-op).

A later, fetch-based extension (callee/caller signatures resolved across files) layers on top;
this diff-only core ships first because it targets the exact miss classes at $0 and is scorable
on the existing harness.
"""

from __future__ import annotations

import re

from .security import sanitize_for_prompt

MAX_ITEMS = 8
MAX_CARD_CHARS = 700

# Removed lines that look like a guard / control-flow check the change dropped. Matched against
# the line CONTENT (diff marker already stripped). Language-agnostic enough for py/js/ts/go.
_GUARD = re.compile(
    r"""(?x)
    ^\s*(
        if\b | elif\b | else\s+if\b |          # conditionals
        assert\b |                              # assertions
        except\b | catch\b | rescue\b |         # error handling
        raise\b | throw\b |                     # explicit error
        return\s+(None|null|nil|false|False)\b  # early bail-out
    )
    | \b(is\s+(not\s+)?None|!=\s*None|==\s*None|!==\s*null|===\s*null|err\s*!=\s*nil)\b  # None/nil checks
    """
)

# A function/method declaration line, with the name captured. Covers Python def, JS function/
# method/arrow-const, and Go func. Enough to detect a signature change by name.
_DEF = re.compile(
    r"""(?x)
    (?:^|\s)
    (?:
        (?:async\s+)?def\s+(?P<py>[A-Za-z_]\w*)\s*\( |
        (?:export\s+)?(?:async\s+)?function\s+(?P<js>[A-Za-z_]\w*)\s*\( |
        func\s+(?:\([^)]*\)\s*)?(?P<go>[A-Za-z_]\w*)\s*\( |
        (?:const|let|var)\s+(?P<arrow>[A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(
    )
    """
)


def _split_diff(patch: str) -> tuple[list[str], list[str]]:
    """Return (removed_lines, added_lines), content only (markers + file headers stripped)."""
    removed, added = [], []
    for ln in (patch or "").splitlines():
        if ln.startswith("-") and not ln.startswith("---"):
            removed.append(ln[1:])
        elif ln.startswith("+") and not ln.startswith("+++"):
            added.append(ln[1:])
    return removed, added


def removed_guards(patch: str) -> list[str]:
    """Deleted lines that look like a guard/check — the 'removed workaround' signal. Deduped,
    trimmed, only those NOT re-added verbatim (a pure move isn't a dropped guard)."""
    removed, added = _split_diff(patch)
    added_norm = {a.strip() for a in added}
    out: list[str] = []
    seen: set[str] = set()
    for line in removed:
        s = line.strip()
        if len(s) < 4 or s in added_norm or s in seen:
            continue
        if _GUARD.search(line):
            out.append(s)
            seen.add(s)
    return out


def _def_name(line: str) -> str | None:
    m = _DEF.search(line)
    if not m:
        return None
    return m.group("py") or m.group("js") or m.group("go") or m.group("arrow")


def contract_changes(patch: str) -> list[str]:
    """Functions whose declaration changed between the `-` and `+` sides (params/return type) —
    the 'signature change / typing regression / caller-contract' signal. One fact per name."""
    removed, added = _split_diff(patch)
    old = {n: ln.strip() for ln in removed if (n := _def_name(ln))}
    new = {n: ln.strip() for ln in added if (n := _def_name(ln))}
    out: list[str] = []
    for name in sorted(set(old) & set(new)):
        if old[name] != new[name]:
            out.append(f"{old[name]}  →  {new[name]}")
    return out


def build_signal_card(patch: str) -> str:
    """Assemble the compact `<impact>` card from a single file's patch. '' when nothing fires."""
    guards = removed_guards(patch)
    contracts = contract_changes(patch)
    if not guards and not contracts:
        return ""
    parts: list[str] = []
    if contracts:
        parts.append(
            "Signatures changed by this diff (verify EVERY call site still matches — "
            "a type/param/return change is the classic caller-contract bug):"
        )
        parts += [f"- {c}" for c in contracts[:MAX_ITEMS]]
    if guards:
        parts.append(
            "Guards/checks this diff REMOVES (verify each was not load-bearing — a dropped "
            "guard/workaround is invisible in the surrounding lines):"
        )
        parts += [f"- {g}" for g in guards[:MAX_ITEMS]]
    inner = sanitize_for_prompt("\n".join(parts))[:MAX_CARD_CHARS]
    return "<impact>\n" + inner + "\n</impact>"


def build_signals(files) -> str:
    """Combine per-file impact cards for the changed files into one bounded block (live graph +
    benchmark share this). `files` is any list with `.path`/`.patch`/`.skipped`."""
    blocks: list[str] = []
    budget = 2 * MAX_CARD_CHARS
    for f in files:
        if getattr(f, "skipped", False) or not getattr(f, "patch", None):
            continue
        card = build_signal_card(f.patch or "")
        if not card:
            continue
        block = f"{f.path}:\n{card}"
        if len(block) > budget:
            break
        blocks.append(block)
        budget -= len(block)
    return "\n\n".join(blocks)
