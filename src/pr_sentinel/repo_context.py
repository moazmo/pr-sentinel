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

Multi-language (D37): **Python** resolves same-file siblings + imported modules;
**JS/TS** resolves same-file siblings + relative (`./`, `../`) imports; **Go**
resolves same-file siblings only (cross-package symbols live in sibling files of
the same directory, which needs a directory listing the fetch abstraction here
doesn't provide — left to the agentic upgrade). Default off, opt-in, fail-open,
strictly bounded; gated on a measured multi-language win on `evals/realpr.py`
before any default change (more context can also hurt — D34/SWE-PRBench).
"""

from __future__ import annotations

import asyncio
import keyword
import posixpath
import re

from .security import sanitize_for_prompt

# Identifiers never worth resolving: Python keywords + ubiquitous builtins, plus
# the common JS and Go keywords/builtins so a multi-language diff doesn't try to
# "resolve" `function`, `const`, `func`, `range`, `nil`, etc.
_STOP = set(keyword.kwlist) | {
    # Python builtins / common
    "self", "cls", "True", "False", "None", "print", "len", "range", "str", "int",
    "float", "bool", "list", "dict", "set", "tuple", "type", "super", "isinstance",
    "open", "enumerate", "zip", "map", "filter", "sorted", "min", "max", "sum", "any",
    "all", "return", "raise", "assert", "yield", "await", "async", "def", "class",
    # JS / TS
    "function", "const", "let", "var", "new", "typeof", "instanceof", "export",
    "import", "default", "from", "require", "module", "exports", "this", "null",
    "undefined", "void", "delete", "extends", "throw", "catch", "finally", "try",
    "switch", "case", "break", "continue", "do", "of", "in", "else", "if", "for",
    "while", "console", "window", "document", "Object", "Array", "String", "Number",
    "Boolean", "Promise", "Math", "JSON", "Date", "Map", "Set",
    # Go
    "func", "package", "struct", "interface", "chan", "defer", "select", "nil",
    "make", "cap", "append", "panic", "recover", "error", "string", "byte", "rune",
    "fmt", "go",
}
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Extension -> language family for extraction. TS is treated as JS for signature
# extraction (close enough for a bounded definition snippet).
_LANG = {
    ".py": "py",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".go": "go",
}


def _ext(path: str) -> str:
    return path[path.rfind("."):].lower() if "." in path else ""


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


# --------------------------------------------------------------------------
# Python extraction (full: same-file siblings + imported modules)
# --------------------------------------------------------------------------

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
    return _wrap(found, max_chars)


# --------------------------------------------------------------------------
# JS/TS and Go extraction (brace-balanced definitions)
# --------------------------------------------------------------------------

def parse_js_imports(content: str) -> dict[str, str]:
    """Map imported local name -> module specifier (e.g. `./calc`) for ES imports
    and CommonJS `require`. Bare (node_modules) specifiers are kept too; the
    resolver drops the non-relative ones."""
    mapping: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        m = re.match(r"import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+['\"]([^'\"]+)['\"]", line)
        if m:
            names, mod = m.group(1), m.group(2)
            for part in names.split(","):
                part = part.strip()
                if not part:
                    continue
                alias = re.split(r"\s+as\s+", part)
                local = (alias[1] if len(alias) > 1 else alias[0]).strip()
                if local:
                    mapping[local] = mod
            continue
        m = re.match(r"import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+['\"]([^'\"]+)['\"]", line)
        if m:
            mapping[m.group(1)] = m.group(2)
            continue
        m = re.match(r"(?:const|let|var)\s+\{([^}]*)\}\s*=\s*require\(['\"]([^'\"]+)['\"]\)", line)
        if m:
            names, mod = m.group(1), m.group(2)
            for part in names.split(","):
                local = part.split(":")[0].strip()
                if local:
                    mapping[local] = mod
            continue
        m = re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*require\(['\"]([^'\"]+)['\"]\)", line)
        if m:
            mapping[m.group(1)] = m.group(2)
    return mapping


def _js_module_candidates(mod: str, from_path: str) -> list[str]:
    """Resolve a relative JS/TS import (`./x`, `../y`) to candidate repo paths,
    relative to the importing file's directory. Bare packages -> [] (skip)."""
    if not mod.startswith("."):
        return []
    base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), mod))
    exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    return [base + e for e in exts] + [base + "/index" + e for e in exts]


def extract_braced_def(content: str, name: str, lang: str, max_lines: int = 20) -> str | None:
    """Extract a JS/TS or Go definition by brace-balancing from the declaration
    line. Brace counting ignores string/comment edge cases — fine for a bounded
    signature snippet, not a parser. Returns the single declaration line for
    brace-less forms (arrow consts, type aliases)."""
    lines = content.splitlines()
    if lang == "js":
        pats = (
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{re.escape(name)}\b",
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+{re.escape(name)}\b",
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+{re.escape(name)}\b\s*=",
        )
    else:  # go
        pats = (
            rf"^\s*func\s+{re.escape(name)}\b",
            rf"^\s*func\s+\([^)]*\)\s+{re.escape(name)}\b",
            rf"^\s*type\s+{re.escape(name)}\b",
        )
    rx = [re.compile(p) for p in pats]
    for i, line in enumerate(lines):
        if not any(r.match(line) for r in rx):
            continue
        if "{" not in line:
            return line.rstrip()  # arrow const / type alias one-liner
        block = [line]
        depth = line.count("{") - line.count("}")
        for nxt in lines[i + 1 : i + max_lines]:
            block.append(nxt)
            depth += nxt.count("{") - nxt.count("}")
            if depth <= 0:
                break
        return "\n".join(block).rstrip()
    return None


def _build_braced_context(
    changed_content: str,
    referenced: set[str],
    lang: str,
    from_path: str,
    src_by_path: dict[str, str],
    max_defs: int = 6,
    max_chars: int = 2400,
) -> str:
    """JS/Go assembler: same-file sibling defs, plus (JS only) defs from resolved
    relative imports."""
    imports = parse_js_imports(changed_content) if lang == "js" else {}
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in sorted(referenced):
        if len(found) >= max_defs:
            break
        if name in seen:
            continue
        local = extract_braced_def(changed_content, name, lang)
        if local:
            found.append((name, local))
            seen.add(name)
    if lang == "js":
        for name in sorted(referenced):
            if len(found) >= max_defs or name in seen:
                continue
            mod = imports.get(name)
            if not mod:
                continue
            for cand in _js_module_candidates(mod, from_path):
                src = src_by_path.get(cand)
                if src:
                    snippet = extract_braced_def(src, name, lang)
                    if snippet:
                        found.append((f"{mod}.{name}", snippet))
                        seen.add(name)
                        break
    return _wrap(found, max_chars)


def _wrap(found: list[tuple[str, str]], max_chars: int) -> str:
    """Assemble found (label, snippet) pairs into a bounded, sanitized,
    delimited block. Content is sanitized BEFORE the wrapper tags are added — it
    comes from the PR head ref (PR-controlled), so a hostile imported module can't
    close the block early and break out of the data context (invariant 3)."""
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
    inner = sanitize_for_prompt("\n\n".join(parts))
    return "<repo_context>\n" + inner + "\n</repo_context>"


# --------------------------------------------------------------------------
# Orchestration (shared by the live graph and the real-PR benchmark)
# --------------------------------------------------------------------------

async def gather_context(files, fetch, max_files: int = 8, total_chars: int = 6000) -> str:
    """Fetch-agnostic orchestration shared by the graph (github-backed fetch) and
    the real-PR benchmark (contents-API fetch). `files` is any list of objects with
    `.path`/`.patch`/`.skipped`; `fetch(path)` is an async callable returning the
    head/ref content of a repo path (or None). Returns a bounded context block.

    Languages: Python (same-file + imported modules), JS/TS (same-file + relative
    imports), Go (same-file siblings only). Unknown extensions are skipped."""
    src = [
        f for f in files
        if not getattr(f, "skipped", False)
        and getattr(f, "patch", None)
        and _ext(f.path) in _LANG
    ][:max_files]
    if not src:
        return ""

    changed_src = dict(zip(
        [f.path for f in src],
        await asyncio.gather(*[fetch(f.path) for f in src]),
    ))

    # Plan: per changed file, its language, content, and referenced symbols; and
    # collect every external repo path we may need to fetch (one round-trip set).
    plan: list[tuple[object, str, str, set[str]]] = []
    wanted: set[str] = set()
    for f in src:
        content = changed_src.get(f.path)
        if not content:
            continue
        lang = _LANG[_ext(f.path)]
        referenced = referenced_names(added_code(f.patch or ""))
        plan.append((f, lang, content, referenced))
        if lang == "py":
            imports = parse_python_imports(content)
            for name in referenced:
                mod = imports.get(name)
                if mod and not mod.startswith("."):
                    wanted.add(mod.replace(".", "/") + ".py")
                    wanted.add(mod.replace(".", "/") + "/__init__.py")
        elif lang == "js":
            imports = parse_js_imports(content)
            for name in referenced:
                mod = imports.get(name)
                if mod:
                    wanted.update(_js_module_candidates(mod, f.path))
        # go: same-file only — no external paths to fetch

    fetched = dict(zip(
        list(wanted)[:16],
        await asyncio.gather(*[fetch(p) for p in list(wanted)[:16]]),
    ))
    src_by_path = {p: c for p, c in fetched.items() if c}

    blocks: list[str] = []
    budget = total_chars
    for f, lang, content, referenced in plan:
        if budget <= 0:
            break
        cap = min(2400, budget)
        if lang == "py":
            imports = parse_python_imports(content)
            module_source: dict[str, str] = {}
            for name in referenced:
                mod = imports.get(name)
                if not mod or mod.startswith("."):
                    continue
                for cand in (mod.replace(".", "/") + ".py", mod.replace(".", "/") + "/__init__.py"):
                    if src_by_path.get(cand):
                        module_source[mod] = src_by_path[cand]
                        break
            block = build_python_context(content, referenced, module_source, max_chars=cap)
        else:
            block = _build_braced_context(content, referenced, lang, f.path, src_by_path, max_chars=cap)
        if block:
            blocks.append(block)
            budget -= len(block)
    return "\n\n".join(blocks)
