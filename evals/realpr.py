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
    "psf/requests", "pallets/flask", "encode/httpx", "pallets/click",
    "pydantic/pydantic", "tiangolo/fastapi", "psf/black",
    "expressjs/express", "gin-gonic/gin",
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


def _reverted_lines(reversed_patch: str) -> set[int]:
    """New-file line numbers of the reintroduced (buggy) added lines."""
    from pr_sentinel.diffmap import added_line_numbers
    return added_line_numbers(reversed_patch)


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

    # Context is deterministic per (repo, sha) — fetch once, reuse across runs.
    ctx_cache: dict[int, str] = {}
    if want_ctx:
        from pr_sentinel.repo_context import gather_context
        for e in manifest:
            ctx_cache[e["pr"]] = await gather_context(buggy_files(e), _make_fetch(e["repo"], e["sha"]))

    from datetime import date
    total_caught = 0
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
            if runs == 1:
                print(f"[{'CAUGHT' if hit else 'missed'}] {e['repo']}#{e['pr']} {e['file']} — {e['title'][:60]}")
        total_caught += run_caught
        try:
            with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
                fh.write(f"{date.today().isoformat()} [realpr{tag} run{run_i}/{runs}] {run_caught}/{len(manifest)}\n")
        except OSError:
            pass
        print(f"run {run_i}/{runs}: {run_caught}/{len(manifest)}")

    cost, _ = estimate_cost_usd(model, total_in, total_out)
    cells = (len(manifest) or 1) * runs
    line = f"Recall on real reintroduced bugs{tag}: {total_caught}/{cells} ({100*total_caught//cells}%) over {runs} run(s) · ≈${cost:.3f}"
    print(f"\n{line}")
    # Per-PR consistency (how many of the N runs caught each) — separates signal from noise.
    consistent = sum(1 for v in per_pr_hits.values() if v == runs)
    flaky = sum(1 for v in per_pr_hits.values() if 0 < v < runs)
    print(f"per-PR: {consistent} caught every run, {flaky} flaky (caught some runs)")
    try:
        with open(Path(__file__).parent / "_matrix.log", "a", encoding="utf-8") as fh:
            fh.write(f"{date.today().isoformat()} [realpr{tag}] {line} | consistent={consistent} flaky={flaky}\n")
    except OSError:
        pass
    await provider.aclose()
    if gh_client:
        await gh_client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
