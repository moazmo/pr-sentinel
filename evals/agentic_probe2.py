"""Agentic-review probe v2 — the *proper* loop (Lever 2), retesting what the naive
D38 probe botched.

D38 measured a NAIVE tool-loop (whole-file `fetch_file`, no structure, bare single
call, default reasoning effort) and found it HURT recall on `deepseek-v4-flash`
(1/10 vs 4/10 diff-only) — attention dilution. This v2 is the best-shot redesign to
counter exactly that failure mode:

  1. `fetch_definition(path, symbol)` returns only the bounded *definition* of a
     symbol (reusing repo_context.extract_python_def / extract_braced_def), not the
     whole file — minimal noise per fetch. Plus `list_dir(path)` to navigate.
  2. RepoAudit structure in the prompt: hypothesize the suspicious lines from the
     DIFF first, fetch a definition only to *confirm/refute* a hypothesis, then
     validate — the diff stays the focus, fetched defs are reference-only.
  3. `reasoning_effort=high` (DeepSeek V4 thinking supports it with tool calls, D38).
  4. Same diff-only control (imported from agentic_probe) on the same cached PRs, so
     the number is directly comparable to D38's 1/10 vs 4/10.

Honest-numbers rule: this is a research probe. Measure recall vs the diff-only baseline
BEFORE anything gets near the product graph. Scored exactly like realpr/agentic_probe
(a hit = a finding on the reintroduced buggy line).

    PR_SENTINEL_API_KEY=sk-... GITHUB_TOKEN=$(gh auth token) \
        python evals/agentic_probe2.py --limit 10 [--control] [--model deepseek-v4-flash]
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pr_sentinel.agents import _extract_finding_dicts  # noqa: E402
from pr_sentinel.repo_context import _LANG, _ext, extract_braced_def, extract_python_def  # noqa: E402
from pr_sentinel.security import sanitize_for_prompt  # noqa: E402

import agentic_probe  # noqa: E402  (sibling: CONTROL_SYSTEM, _chat retry shape)
import realpr  # noqa: E402  (sibling: discover / reverse_patch / _reverted_lines / CACHE)

BASE = os.environ.get("PR_SENTINEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
URL = BASE + "/chat/completions"
GH = "https://api.github.com"
MAX_ROUNDS = 4
MAX_FETCHES = 6
MAX_DEF_CHARS = 1600
RPM_SLEEP = float(os.environ.get("PROBE_RPM_SLEEP", "0.8"))
REASONING_EFFORT = os.environ.get("PR_SENTINEL_REASONING_EFFORT", "high")

SYSTEM_V2 = (
    "You are a senior code reviewer auditing a pull-request DIFF for real bugs the "
    "change INTRODUCES. Work like a focused auditor, not a browser:\n"
    "1. Read the diff and form concrete hypotheses about which changed lines could be "
    "wrong (a broken contract, a removed guard, a wrong type, an off-by-one, a caller "
    "expectation the change violates).\n"
    "2. ONLY when a hypothesis genuinely needs a definition you cannot see, call "
    "`fetch_definition(path, symbol)` to pull that one symbol's definition (use "
    "`list_dir(path)` if you must locate a file first). Fetch to CONFIRM or REFUTE a "
    "specific hypothesis — not to browse. A few fetches at most.\n"
    "3. The DIFF is the subject; fetched definitions are reference only. Do not report "
    "issues in unchanged/reference code.\n"
    "When done, reply with ONLY a JSON object, each finding verdict-first:\n"
    '{"findings":[{"file":"<path from the diff>","line_start":<int>,"line_end":<int>,'
    '"severity":"critical|high|medium|low","category":"<short>","message":"<the bug and '
    'why it is wrong>"}]}. Report only bugs you are confident the diff introduces. '
    'If none, reply {"findings":[]}.'
)

TOOLS_V2 = [
    {
        "type": "function",
        "function": {
            "name": "fetch_definition",
            "description": "Return just the definition of `symbol` in repo file `path` "
                           "at this PR's commit (bounded). Use to confirm/refute a hypothesis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"},
                    "symbol": {"type": "string", "description": "function/class/type name"},
                },
                "required": ["path", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory's entries at this PR's commit (to locate a file).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "repo-relative dir path"}},
                "required": ["path"],
            },
        },
    },
]


def _gh_get(client: httpx.Client, repo: str, path: str, ref: str, token: str):
    """Raw contents-API result (dict for a file, list for a directory) or None."""
    try:
        r = client.get(f"{GH}/repos/{repo}/contents/{path}", params={"ref": ref},
                       headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _file_text(data) -> str | None:
    if isinstance(data, dict) and data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        except (KeyError, ValueError):
            return None
    return None


def fetch_definition(client, repo, path, symbol, ref, token) -> str:
    """Bounded definition of `symbol` in `path`. Falls back to the file head if the
    symbol isn't found — still far smaller than a whole-file dump."""
    data = _gh_get(client, repo, path, ref, token)
    content = _file_text(data)
    if not content:
        return f"({path}: not found or not a file)"
    lang = _LANG.get(_ext(path))
    snippet = None
    if lang == "py":
        snippet = extract_python_def(content, symbol)
    elif lang in ("js", "go"):
        snippet = extract_braced_def(content, symbol, lang)
    if not snippet:
        head = "\n".join(content.splitlines()[:30])
        snippet = f"(symbol '{symbol}' not found; file head)\n{head}"
    snippet = sanitize_for_prompt(snippet[:MAX_DEF_CHARS])
    return f"(definition from {path} — reference only, not under review)\n{snippet}"


def list_dir(client, repo, path, ref, token) -> str:
    data = _gh_get(client, repo, path, ref, token)
    if isinstance(data, list):
        return "directory listing:\n" + "\n".join(
            f"  {i.get('name','')} ({i.get('type','')})" for i in data[:80] if isinstance(i, dict))
    return f"({path}: not a directory)"


def _chat(client, key, model, messages, use_tools=True):
    """Chat call with reasoning_effort + the DeepSeek reasoning_content echo rule,
    retrying transient 504/429/5xx (mirrors agentic_probe._chat)."""
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 2200}
    if REASONING_EFFORT:
        payload["reasoning_effort"] = REASONING_EFFORT
    if use_tools:
        payload["tools"] = TOOLS_V2
    headers = {"Authorization": f"Bearer {key}",
               "HTTP-Referer": "https://github.com/moazmo/pr-sentinel",
               "X-Title": "PR Sentinel agentic probe v2"}
    last, delay = "?", 4.0
    for attempt in range(3):
        time.sleep(RPM_SLEEP)
        try:
            r = client.post(URL, json=payload, headers=headers, timeout=90)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        else:
            if r.status_code == 200 and "choices" in data:
                return data["choices"][0]["message"]
            code = (data.get("error") or {}).get("code", r.status_code)
            last = json.dumps(data)[:120]
            if code not in (429, 500, 502, 503, 504):
                print(f"  ! non-transient: {last}", flush=True)
                return None
        print(f"  · transient ({last}); retry {attempt+1}/3 in {delay:.0f}s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 16)
    print(f"  ! gave up: {last}", flush=True)
    return None


def review_pr(client, key, model, entry, token) -> tuple[list[dict], int]:
    """The v2 agentic loop on one reversed-buggy PR. Returns (findings, n_fetches)."""
    rev = realpr.reverse_patch(entry["patch"])
    user = (f"Repository: {entry['repo']}\nFile under review: {entry['file']}\n\n"
            f"<diff>\n{rev}\n</diff>\n\nAudit this change.")
    messages = [{"role": "system", "content": SYSTEM_V2}, {"role": "user", "content": user}]
    fetches = 0
    seen: dict[str, str] = {}
    for rnd in range(MAX_ROUNDS):
        print(f"  round {rnd+1}/{MAX_ROUNDS} (fetches {fetches})", flush=True)
        msg = _chat(client, key, model, messages)
        if msg is None:
            return [], fetches
        calls = msg.get("tool_calls") or []
        if not calls:
            return (_extract_finding_dicts(msg.get("content") or "") or []), fetches
        messages.append(msg)  # assistant turn w/ tool_calls (+ reasoning_content) — must echo
        for c in calls:
            name = (c.get("function") or {}).get("name", "")
            try:
                args = json.loads((c["function"]["arguments"]) or "{}")
            except (json.JSONDecodeError, KeyError, TypeError):
                args = {}
            cache_key = f"{name}:{args.get('path','')}:{args.get('symbol','')}"
            if cache_key in seen:
                body = seen[cache_key]
            elif fetches >= MAX_FETCHES:
                body = "(fetch budget exhausted — judge the diff now)"
            else:
                fetches += 1
                if name == "fetch_definition":
                    print(f"    fetch_definition({args.get('path','')}, {args.get('symbol','')})", flush=True)
                    body = fetch_definition(client, entry["repo"], args.get("path", ""),
                                            args.get("symbol", ""), entry["sha"], token)
                elif name == "list_dir":
                    print(f"    list_dir({args.get('path','')})", flush=True)
                    body = list_dir(client, entry["repo"], args.get("path", ""), entry["sha"], token)
                else:
                    body = f"(unknown tool {name})"
                seen[cache_key] = body
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "name": name, "content": body})
    print("  forcing final answer (no tools)", flush=True)
    msg = _chat(client, key, model, messages, use_tools=False)
    if msg:
        return (_extract_finding_dicts(msg.get("content") or "") or []), fetches
    return [], fetches


def review_control(client, key, model, entry) -> tuple[list[dict], int]:
    """Diff-only single pass — identical to agentic_probe's control for comparability."""
    rev = realpr.reverse_patch(entry["patch"])
    user = (f"Repository: {entry['repo']}\nFile under review: {entry['file']}\n\n"
            f"<diff>\n{rev}\n</diff>\n\nReview this change.")
    msg = _chat(client, key, model,
                [{"role": "system", "content": agentic_probe.CONTROL_SYSTEM},
                 {"role": "user", "content": user}], use_tools=False)
    if msg is None:
        return [], 0
    return (_extract_finding_dicts(msg.get("content") or "") or []), 0


def main() -> int:
    key = os.environ.get("PR_SENTINEL_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("Set PR_SENTINEL_API_KEY (DeepSeek).")
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "deepseek-v4-flash"
    control = "--control" in sys.argv
    mode = "control(diff-only)" if control else f"agentic-v2(effort={REASONING_EFFORT})"
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10

    cache = realpr.CACHE
    if cache.exists():
        manifest = json.loads(cache.read_text(encoding="utf-8"))
    else:
        manifest = realpr.discover(realpr.DEFAULT_REPOS, 3, token)
        cache.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    manifest = manifest[:limit]
    print(f"endpoint: {URL} · mode: {mode} · model={model} · {len(manifest)} PR(s)\n")

    caught = total_fetches = 0
    with httpx.Client() as client:
        for e in manifest:
            print(f"reviewing {e['repo']}#{e['pr']} {e['file']}", flush=True)
            targets = realpr._reverted_lines(realpr.reverse_patch(e["patch"]))
            if control:
                findings, nf = review_control(client, key, model, e)
            else:
                findings, nf = review_pr(client, key, model, e, token)
            total_fetches += nf
            hit = any(
                isinstance(f, dict) and f.get("file") == e["file"]
                and any(t in range(int(f.get("line_start", 0) or 0),
                                   int(f.get("line_end", f.get("line_start", 0)) or 0) + 1)
                        for t in targets)
                for f in findings)
            caught += hit
            print(f"[{'CAUGHT' if hit else 'missed'}] {e['repo']}#{e['pr']} {e['file']} "
                  f"({nf} fetches, {len(findings)} findings) — {e['title'][:50]}")
            try:
                with open(Path(__file__).parent / "_agentic.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{date.today().isoformat()} [{mode} {model}] {e['repo']}#{e['pr']} "
                             f"{'CAUGHT' if hit else 'missed'} fetches={nf} findings={len(findings)}\n")
            except OSError:
                pass

    n = len(manifest) or 1
    summary = f"{mode} recall: {caught}/{n} ({100*caught//n}%) · {total_fetches} fetches"
    print(f"\n{summary}")
    try:
        with open(Path(__file__).parent / "_agentic.log", "a", encoding="utf-8") as fh:
            fh.write(f"{date.today().isoformat()} [{mode} {model}] SUMMARY {summary}\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
