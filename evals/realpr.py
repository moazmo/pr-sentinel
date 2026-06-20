"""Real-PR benchmark (research lever L4): measure recall on REAL bugs, not
seeded fixtures.

Method — inverted bug-fix commits (the cheap, honest proxy for Martian/CodeAnt's
"did the developer act on the comment"): take a merged PR that *fixed* a bug,
reverse its diff so the bug is *reintroduced*, run PR Sentinel on that reversed
diff, and check whether we flag the reintroduced bug. Ground truth = the lines
the fix touched. No hand-typed commit SHAs — fix PRs are discovered live from the
GitHub API, so nothing is invented; a stale entry just 404s and is skipped.

This measures RECALL on real bugs (the hard axis). Precision/false-positives stay
on the clean fixtures + clean real PRs. Run manually (hits a real LLM):

    PR_SENTINEL_API_KEY=... GITHUB_TOKEN=... python evals/realpr.py --per-repo 3
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pr_sentinel.config import SentinelConfig  # noqa: E402
from pr_sentinel.graph import build_graph  # noqa: E402
from pr_sentinel.models import ChangedFile, PRMetadata  # noqa: E402
from pr_sentinel.provider import OpenAICompatProvider, estimate_cost_usd  # noqa: E402

# Active, well-known repos with small, reviewable bug-fix PRs. Python-weighted on
# purpose: repo_context (L3) is Python-first, so the lever needs Python surface to
# act on; the JS/Go repos add language breadth for the diff-only recall number.
DEFAULT_REPOS = [
    # Python
    "psf/requests", "pallets/flask", "encode/httpx", "pallets/click",
    "pydantic/pydantic", "tiangolo/fastapi", "psf/black", "django/django",
    "scrapy/scrapy", "sqlalchemy/sqlalchemy", "psf/requests-html", "encode/starlette",
    # JS/TS
    "expressjs/express", "axios/axios", "lodash/lodash", "vuejs/core",
    # Go
    "gin-gonic/gin", "spf13/cobra", "spf13/viper", "labstack/echo",
]
SOURCE_EXT = {".py", ".js", ".ts", ".go", ".java", ".rb", ".rs", ".c", ".cpp", ".cs"}
CACHE = Path(__file__).parent / "_realpr_cache.json"
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")


def reverse_patch(patch: str) -> str:
    """Reverse a unified-diff patch so removed lines become added and vice-versa,
    and hunk headers swap old/new ranges. The result's `+` lines are the original
    buggy code the fix deleted — exactly what we want a reviewer to flag. Pure."""
    out: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                a, b, c, d, rest = m.groups()
                b, d = b or "1", d or "1"
                out.append(f"@@ -{c},{d} +{a},{b} @@{rest}")
            else:
                out.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            out.append("-" + line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            out.append("+" + line[1:])
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if patch.endswith("\n") else "")


_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|[._]test\.|[._]spec\.")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def _has_removal(patch: str) -> bool:
    """True if the patch deletes a line (so reversing it re-adds a buggy line)."""
    return any(ln.startswith("-") and not ln.startswith("---") for ln in patch.splitlines())


def _gh(client: httpx.Client, path: str, token: str, **params):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = client.get(f"https://api.github.com{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def discover(repos: list[str], per_repo: int, token: str) -> list[dict]:
    """Find merged bug-fix PRs via the GitHub search API and reduce each to its
    most-changed (small) source file = clean ground truth. Real SHAs only."""
    found: list[dict] = []
    with httpx.Client() as client:
        for repo in repos:
            try:
                hits = _gh(client, "/search/issues", token,
                           q=f"repo:{repo} is:pr is:merged bug in:title", per_page=30,
                           sort="updated", order="desc").get("items", [])
            except httpx.HTTPError:
                continue
            picked = 0
            for issue in hits:
                if picked >= per_repo:
                    break
                num = issue.get("number")
                try:
                    pr = _gh(client, f"/repos/{repo}/pulls/{num}", token)
                except httpx.HTTPError:
                    continue
                sha = pr.get("merge_commit_sha")
                if not sha:
                    continue
                try:
                    commit = _gh(client, f"/repos/{repo}/commits/{sha}", token)
                except httpx.HTTPError:
                    continue
                # Non-test source files, small, and where the fix actually
                # REMOVED/changed a line (so reversing reintroduces a buggy line
                # to catch — a fix that only adds lines gives no ground truth).
                src = [
                    f for f in commit.get("files", [])
                    if f.get("patch") and Path(f["filename"]).suffix in SOURCE_EXT
                    and 1 <= f.get("changes", 0) <= 60
                    and not _is_test_path(f["filename"])
                    and _has_removal(f["patch"])
                ]
                if not src:
                    continue
                target = max(src, key=lambda f: f.get("changes", 0))  # most-changed source file
                found.append({
                    "repo": repo, "sha": sha, "pr": num,
                    "title": issue.get("title", ""), "file": target["filename"],
                    "patch": target["patch"],
                })
                picked += 1
    return found


def buggy_files(entry: dict) -> list[ChangedFile]:
    reversed_patch = reverse_patch(entry["patch"])
    f = ChangedFile(path=entry["file"], status="modified", patch=reversed_patch)
    f.additions = reversed_patch.count("\n+")
    return [f]


def fixed_files(entry: dict) -> list[ChangedFile]:
    """The PR's merged FIX as-is (forward patch). Its `+` lines are the accepted
    correct code, so a finding landing on them is a false positive — the precision
    proxy both Kimi review passes asked for (recall alone can be gamed by a reviewer
    that flags everything). Pure."""
    patch = entry["patch"]
    f = ChangedFile(path=entry["file"], status="modified", patch=patch)
    f.additions = (patch or "").count("\n+")
    return [f]


def _reverted_lines(reversed_patch: str) -> set[int]:
    """New-file line numbers of the reintroduced (buggy) added lines."""
    from pr_sentinel.diffmap import added_line_numbers
    return added_line_numbers(reversed_patch)


_JUDGE_SYS = (
    "You decide whether a code reviewer found a SPECIFIC bug. The reviewer saw a diff whose "
    "'+' lines reintroduce a defect that was previously fixed. Given the reviewer's findings and "
    "the known fix, answer whether any finding identifies THIS defect — its root cause or its "
    "direct effect — regardless of the exact line number cited. Answer ONLY 'YES' or 'NO'."
)


async def judge_catch(provider, model: str, entry: dict, findings) -> tuple[bool, int, int]:
    """Semantic LLM-judge scorer (opt-in `--judge`): credits a context-aware finding that points
    at the real bug even when it cites a caller/related line, not the exact reverted line — the
    line-overlap under-credit every research pass flagged. Returns (caught, prompt_tok, compl_tok)."""
    if not findings:
        return False, 0, 0
    rev = reverse_patch(entry["patch"])
    fnd = "\n".join(
        f"- {f.file}:{f.line_start} [{f.severity.value}] {f.message[:200]}" for f in findings[:12]
    )
    user = (
        f"BUGGY CHANGE (the '+' lines reintroduce the defect):\n<diff>\n{rev[:4000]}\n</diff>\n\n"
        f"The previously-merged FIX was the reverse of this diff (so the '-' lines here are the "
        f"correct code the change removed).\n\nREVIEWER FINDINGS:\n{fnd or '(none)'}\n\n"
        f"Did the reviewer identify THIS reintroduced defect (root cause or direct effect), "
        f"regardless of exact line? Answer YES or NO."
    )
    try:
        from pr_sentinel.provider import ProviderError
        # thinking=False: a YES/NO classification needs no reasoning tokens, and leaving
        # DeepSeek thinking on with a tiny max_tokens budget starves `content` (the 0/60 bug).
        r = await provider.complete(_JUDGE_SYS, user, max_tokens=16, temperature=0.0,
                                    model=model, thinking=False)
    except (ProviderError, Exception):  # noqa: BLE001 — judge is best-effort
        return False, 0, 0
    caught = r.text.strip().upper().startswith("YES")
    return caught, r.prompt_tokens, r.completion_tokens


async def main() -> int:
    api_key = os.environ.get("PR_SENTINEL_API_KEY", "")
    if not api_key:
        print("Set PR_SENTINEL_API_KEY (real LLM).")
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    per_repo = int(sys.argv[sys.argv.index("--per-repo") + 1]) if "--per-repo" in sys.argv else 3

    if CACHE.exists() and "--refresh" not in sys.argv:
        manifest = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        manifest = discover(DEFAULT_REPOS, per_repo, token)
        CACHE.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if "--limit" in sys.argv:
        manifest = manifest[: max(1, int(sys.argv[sys.argv.index("--limit") + 1]))]
    print(f"{len(manifest)} real bug-fix PRs in the benchmark.")

    base_url = os.environ.get("PR_SENTINEL_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("PR_SENTINEL_MODEL", "deepseek-v4-flash")
    provider = OpenAICompatProvider(api_key, base_url=base_url, model=model,
                                    max_concurrent=4, timeout_seconds=45)
    config = SentinelConfig()
    config.provider.model = model
    config.provider.base_url = base_url
    config.review.context_lines = 0
    config.output.inline = False
    # Sampling knobs as pure env (for the confident-wrong vs uncertain diagnostic:
    # samples=5 min_support=1 adaptive=off measures "union of N samples" recall — the
    # ceiling aggregation could reach — vs the voted ensemble baseline).
    config.accuracy.samples = int(os.environ.get("PR_SENTINEL_SAMPLES", config.accuracy.samples))
    config.accuracy.min_support = int(
        os.environ.get("PR_SENTINEL_MIN_SUPPORT", config.accuracy.min_support))
    if os.environ.get("PR_SENTINEL_ADAPTIVE", "").lower() in ("off", "false", "0"):
        config.accuracy.adaptive = False
    config.accuracy.verifier_aspects = int(
        os.environ.get("PR_SENTINEL_VERIFIER_ASPECTS", config.accuracy.verifier_aspects))
    if os.environ.get("PR_SENTINEL_SIGNALS", "").lower() in ("on", "true", "1"):
        config.accuracy.structured_signals = True
    # Lever 4: reasoning controls as pure env, mirroring evals/run.py — so the
    # reasoning_effort A/B on real PRs needs no code change.
    config.accuracy.reasoning_effort = os.environ.get(
        "PR_SENTINEL_REASONING_EFFORT", config.accuracy.reasoning_effort)
    _think = os.environ.get("PR_SENTINEL_ANALYST_THINKING", "").lower()
    if _think in ("on", "true", "1"):
        config.accuracy.analyst_thinking = True
    elif _think in ("off", "false", "0"):
        config.accuracy.analyst_thinking = False
    if config.accuracy.reasoning_effort or config.accuracy.analyst_thinking is not None:
        print(f"reasoning: effort={config.accuracy.reasoning_effort or '-'} "
              f"thinking={config.accuracy.analyst_thinking}")

    # --repo-context measures lever L3: prefetch cross-file definitions (from the
    # repo at the fix commit) and inject them, to see if recall lifts off the
    # diff-only baseline. github=None in the graph, so we build context here and
    # preset it into the state (ingest preserves a preset value).
    want_ctx = "--repo-context" in sys.argv
    gh_client = httpx.AsyncClient(timeout=30) if want_ctx else None

    def _make_fetch(repo: str, ref: str):
        async def fetch(path: str):
            try:
                r = await gh_client.get(
                    f"https://api.github.com/repos/{repo}/contents/{path}",
                    params={"ref": ref},
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                if data.get("encoding") == "base64":
                    return base64.b64decode(data["content"]).decode("utf-8", "replace")
            except (httpx.HTTPError, ValueError, KeyError):
                return None
            return None
        return fetch

    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 1
    tag = " +repo_context" if want_ctx else ""
    if config.accuracy.reasoning_effort:
        tag += f" +effort={config.accuracy.reasoning_effort}"
    if config.accuracy.samples != 3 or config.accuracy.min_support != 2:
        tag += f" +samples={config.accuracy.samples}/ms={config.accuracy.min_support}"
    if config.accuracy.verifier_aspects != 1:
        tag += f" +mav={config.accuracy.verifier_aspects}"
    if config.accuracy.structured_signals:
        tag += " +signals"

    # Context is deterministic per (repo, sha) — fetch once, reuse across runs.
    ctx_cache: dict[int, str] = {}
    if want_ctx:
        from pr_sentinel.repo_context import gather_context
        for e in manifest:
            ctx_cache[e["pr"]] = await gather_context(buggy_files(e), _make_fetch(e["repo"], e["sha"]))

    from datetime import date
    want_judge = "--judge" in sys.argv
    review_model = config.provider.resolved_review_model
    total_caught = judge_caught = 0
    total_in = total_out = 0
    per_pr_hits: dict[int, int] = {e["pr"]: 0 for e in manifest}
    for run_i in range(1, runs + 1):
        run_caught = 0
        for e in manifest:
            files = buggy_files(e)
            targets = _reverted_lines(files[0].patch or "")
            graph = build_graph(provider, github=None)
            result = await graph.ainvoke({
                "config": config,
                "pr": PRMetadata(repo=e["repo"], number=e["pr"], title="Update " + e["file"]),
                "files": files,
                "repo_context": ctx_cache.get(e["pr"], ""),
            })
            findings = result.get("merged_findings", [])
            usage = result.get("usage")
            if usage:
                total_in += usage.total_prompt
                total_out += usage.total_completion
            hit = any(f.file == e["file"] and (f.line_start in targets or f.line_end in targets
                      or any(t in range(f.line_start, f.line_end + 1) for t in targets))
                      for f in findings)
            run_caught += hit
            per_pr_hits[e["pr"]] += hit
            jhit = False
            if want_judge and run_i == 1:
                jhit, jp, jc = await judge_catch(provider, review_model, e, findings)
                total_in += jp
                total_out += jc
                judge_caught += jhit
            if runs == 1:
                jtag = f" · judge:{'YES' if jhit else 'no'}" if want_judge else ""
                print(f"[{'CAUGHT' if hit else 'missed'}] {e['repo']}#{e['pr']} {e['file']} — {e['title'][:55]}{jtag}")
        total_caught += run_caught
        try:
            with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
                fh.write(f"{date.today().isoformat()} [realpr{tag} run{run_i}/{runs}] {run_caught}/{len(manifest)}\n")
        except OSError:
            pass
        print(f"run {run_i}/{runs}: {run_caught}/{len(manifest)}")

    # Precision proxy (--precision): run each PR's FIXED (forward) version and count
    # findings landing on the fix's added lines — those are false positives (the
    # accepted-correct code). Recall alone can be gamed by a reviewer that flags
    # everything; this is the F1 axis both review passes flagged as missing. One
    # extra pass over the manifest (≈ doubles cost), so it is opt-in.
    fp_prs = -1
    if "--precision" in sys.argv:
        fp_prs = 0
        for e in manifest:
            files = fixed_files(e)
            targets = _reverted_lines(files[0].patch or "")  # = added lines of the fix
            graph = build_graph(provider, github=None)
            result = await graph.ainvoke({
                "config": config,
                "pr": PRMetadata(repo=e["repo"], number=e["pr"], title="Update " + e["file"]),
                "files": files,
                "repo_context": "",
            })
            usage = result.get("usage")
            if usage:
                total_in += usage.total_prompt
                total_out += usage.total_completion
            if any(f.file == e["file"] and (f.line_start in targets or f.line_end in targets
                   or any(t in range(f.line_start, f.line_end + 1) for t in targets))
                   for f in result.get("merged_findings", [])):
                fp_prs += 1

    cost, _ = estimate_cost_usd(model, total_in, total_out)
    cells = (len(manifest) or 1) * runs
    line = f"Recall on real reintroduced bugs{tag}: {total_caught}/{cells} ({100*total_caught//cells}%) over {runs} run(s) · ≈${cost:.3f}"
    print(f"\n{line}")
    # Per-PR consistency (how many of the N runs caught each) — separates signal from noise.
    consistent = sum(1 for v in per_pr_hits.values() if v == runs)
    flaky = sum(1 for v in per_pr_hits.values() if 0 < v < runs)
    print(f"per-PR: {consistent} caught every run, {flaky} flaky (caught some runs)")
    if want_judge:
        n = len(manifest) or 1
        jline = (f"Semantic-judge recall{tag}: {judge_caught}/{n} ({100*judge_caught//n}%) "
                 f"vs line-overlap {per_pr_hits and sum(1 for v in per_pr_hits.values() if v > 0)}/{n} (run1)")
        print(jline)
        try:
            with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
                fh.write(f"{date.today().isoformat()} [realpr judge{tag}] {jline}\n")
        except OSError:
            pass
    try:
        with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
            fh.write(f"{date.today().isoformat()} [realpr{tag}] {line} | consistent={consistent} flaky={flaky}\n")
    except OSError:
        pass
    if fp_prs >= 0:
        tp_prs = sum(1 for v in per_pr_hits.values() if v > 0)
        n = len(manifest) or 1
        recall_prs = tp_prs / n
        denom = tp_prs + fp_prs
        precision = tp_prs / denom if denom else 0.0
        f1 = (2 * precision * recall_prs / (precision + recall_prs)) if (precision + recall_prs) else 0.0
        pline = (f"Precision proxy{tag}: TP={tp_prs} FP={fp_prs} "
                 f"precision={100*precision:.0f}% recall={100*recall_prs:.0f}% F1={100*f1:.0f}% "
                 f"· clean-pass {n - fp_prs}/{n}")
        print(pline)
        try:
            with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
                fh.write(f"{date.today().isoformat()} [realpr precision{tag}] {pline}\n")
        except OSError:
            pass
    await provider.aclose()
    if gh_client:
        await gh_client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
