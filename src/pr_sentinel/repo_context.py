"""Repository context prefetch (research lever L3): the 36% real-PR recall gap is
dominated by *context-dependent* bugs a ±N-line diff can't explain — a caller's
expectations, a helper's contract, why a removed line mattered. The frontier
tools close this with agentic, repo-aware review.

A true agentic tool-loop (model fetches symbols on demand) is blocked on our
stack: DeepSeek's thinking mode — which we proved is essential (D36) — does not
support function calling. So this is the compatible alternative: **deterministically
pre-fetch** the definitions of the symbols the diff references but doesn't define,
rank + bound them, and hand the analysts a delimited "repository context" block
(data under review, never instructions — same injection rules as the diff).

Python-first (most of the real-PR misses are Python; the bulk of the value). The
extraction is structured so other languages slot in behind `LANG_RULES`.

Risk acknowledged: more context can *hurt* (SWE-PRBench attention dilution, D34).
Hence opt-in (`accuracy.repo_context`, default off), strictly bounded, and gated
on a measured win via `evals/realpr.py` before any default change.
"""

from __future__ import annotations

import asyncio
import keyword
import re

# Identifiers that are never worth resolving (keywords + ubiquitous builtins).
_STOP = set(keyword.kwlist) | {
    "self", "cls", "True", "False", "None", "print", "len", "range", "str", "int",
    "float", "bool", "list", "dict", "set", "tuple", "type", "super", "isinstance",
    "open", "enumerate", "zip", "map", "filter", "sorted", "min", "max", "sum", "any",
    "all", "return", "raise", "assert", "yield", "await", "async", "def", "class",
}
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def added_code(patch: str) -> str:
    """The added (`+`) lines of a unified diff, stripped of the `+` marker."""
    out = []
    for ln in (patch or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln[1:])
    return "\n".join(out)


def referenced_names(code: str) -> set[str]:
    """Names the code *uses* — prioritising call targets, minus stopwords. These
    are the symbols whose definitions would help a reviewer judge the change."""
    names = set(_CALL.findall(code))  # foo( ... ) call targets
    # Also attribute roots / bare names, but calls are the high-signal set.
    for m in _IDENT.findall(code):
        names.add(m)
    return {n for n in names if n not in _STOP and len(n) > 2}


def parse_python_imports(content: str) -> dict[str, str]:
    """Map imported local name -> module it came from, for `import`/`from` forms.
    Used to know which module to fetch a referenced symbol's definition from."""
    mapping: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"from\s+([.\w]+)\s+import\s+(.+)", line)
        if m:
            module, names = m.group(1), m.group(2)
            for part in names.replace("(", "").replace(")", "").split(","):
                part = part.strip()
                if not part or part == "*":
                    continue
                alias = part.split(" as ")
                local = (alias[1] if len(alias) > 1 else alias[0]).strip()
                mapping[local] = module
            continue
        m = re.match(r"import\s+([.\w]+)(?:\s+as\s+(\w+))?", line)
        if m:
            module, alias = m.group(1), m.group(2)
            mapping[alias or module.split(".")[0]] = module
    return mapping


def extract_python_def(content: str, name: str, max_lines: int = 18) -> str | None:
    """Return the `def`/`class name` block (signature + body up to a dedent or
    `max_lines`). Bounded so a giant function can't blow the context budget."""
    lines = content.splitlines()
    pat = re.compile(rf"^(\s*)(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        block = [line]
        for nxt in lines[i + 1 : i + max_lines]:
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent and not nxt.lstrip().startswith(("#", '"', "'")):
                break  # dedented to a sibling -> definition ended
            block.append(nxt)
        return "\n".join(block).rstrip()
    return None


def build_python_context(
    changed_content: str,
    referenced: set[str],
    module_source: dict[str, str],
    max_defs: int = 6,
    max_chars: int = 2400,
) -> str:
    """Assemble a bounded context block: definitions of referenced symbols found
    either in the changed file itself or in the modules it imports.

    `module_source` maps a module path -> that module's full text (the caller
    fetches these from the head ref). Returns "" when nothing useful is found.
    """
    imports = parse_python_imports(changed_content)
    found: list[tuple[str, str]] = []  # (label, snippet)
    seen: set[str] = set()
    # 1) Definitions in the changed file itself (siblings the diff calls).
    for name in sorted(referenced):
        if len(found) >= max_defs:
            break
        if name in seen:
            continue
        local = extract_python_def(changed_content, name)
        if local:
            found.append((name, local))
            seen.add(name)
    # 2) Definitions imported from other modules we have the source for.
    for name in sorted(referenced):
        if len(found) >= max_defs or name in seen:
            continue
        module = imports.get(name)
        if not module:
            continue
        src = module_source.get(module)
        if not src:
            continue
        snippet = extract_python_def(src, name)
        if snippet:
            found.append((f"{module}.{name}", snippet))
            seen.add(name)
    if not found:
        return ""
    parts: list[str] = []
    budget = max_chars
    for label, snippet in found:
        block = f"# {label}\n{snippet}"
        if len(block) > budget:
            break
        parts.append(block)
        budget -= len(block)
    if not parts:
        return ""
    return "<repo_context>\n" + "\n\n".join(parts) + "\n</repo_context>"


async def gather_context(files, fetch, max_files: int = 8, total_chars: int = 6000) -> str:
    """Fetch-agnostic orchestration shared by the graph (github-backed fetch) and
    the real-PR benchmark (contents-API fetch). `files` is any list of objects with
    `.path`/`.patch`/`.skipped`; `fetch(path)` is an async callable returning the
    head/ref content of a repo path (or None). Returns a bounded context block."""
    py = [f for f in files if not getattr(f, "skipped", False)
          and getattr(f, "patch", None) and f.path.endswith(".py")][:max_files]
    if not py:
        return ""
    changed_src = dict(zip(
        [f.path for f in py],
        await asyncio.gather(*[fetch(f.path) for f in py]),
    ))
    wanted: set[str] = set()
    per_file: list[tuple[str, str, set[str]]] = []
    for f in py:
        content = changed_src.get(f.path)
        if not content:
            continue
        referenced = referenced_names(added_code(f.patch or ""))
        per_file.append((f.path, content, referenced))
        imports = parse_python_imports(content)
        for name in referenced:
            mod = imports.get(name)
            if mod and not mod.startswith("."):
                wanted.add(mod)

    async def _module(mod: str):
        for cand in (mod.replace(".", "/") + ".py", mod.replace(".", "/") + "/__init__.py"):
            src = await fetch(cand)
            if src:
                return mod, src
        return mod, None

    module_source = {
        m: s for m, s in await asyncio.gather(*[_module(m) for m in list(wanted)[:12]]) if s
    }
    blocks: list[str] = []
    budget = total_chars
    for _path, content, referenced in per_file:
        block = build_python_context(content, referenced, module_source, max_chars=min(2400, budget))
        if block:
            blocks.append(block)
            budget -= len(block)
        if budget <= 0:
            break
    return "\n\n".join(blocks)
