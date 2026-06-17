"""SAST-grounding probe (research lever L1 / D35): does feeding Semgrep hits into
the pipeline lift real-PR results? Measures on the SAME real bug-fix PRs as
`realpr.py`, so the number is comparable to the diff-only baseline.

Two phases, sequenced to spend the LLM budget only when it can matter:

  Phase 1 ($0, always): RAW Semgrep, no LLM.
    - recall: scan each PR's *parent-commit* file (the buggy state) and check
      whether Semgrep flags any reintroduced buggy line (= realpr ground truth).
    - false-positives: scan each PR's *fixed* file and count Semgrep hits landing
      on the fix's accepted-correct added lines.
    If raw recall ~0, SAST cannot help this benchmark (these are logic bugs, not
    textbook security bugs) — stop, report, skip the spend.

  Phase 2 (--pipeline, hits the LLM): preset the Semgrep hits into the graph's
    `findings` reducer so they flow through evidence anchoring + the rubric
    verifier, and compare recall WITH vs WITHOUT grounding on the same PRs, plus
    how many raw hits the verifier keeps (the FP-kill / grounding story).

Semgrep runs via the official `semgrep/semgrep` Docker image (it has no native
Windows build, and a Docker image variant is the product's ship path anyway), so
this needs Docker. `--config auto` requires telemetry ON, so the honest default
here is a concrete registry ruleset (`p/default`).

    GITHUB_TOKEN=$(gh auth token) python evals/sast_probe.py            # phase 1, $0
    PR_SENTINEL_API_KEY=sk-... GITHUB_TOKEN=$(gh auth token) \
        python evals/sast_probe.py --pipeline                           # + LLM arms
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import realpr  # noqa: E402  (sibling: discover / reverse_patch / _reverted_lines / CACHE)
from pr_sentinel.diffmap import added_line_numbers, line_text_map  # noqa: E402
from pr_sentinel.sast import parse_semgrep_json  # noqa: E402

GH = "https://api.github.com"
LOG = Path(__file__).parent / "_sast.log"
DEFAULT_RULES = os.environ.get("PR_SENTINEL_SAST_RULES", "p/default")


def _log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{date.today().isoformat()} {line}\n")
    except OSError:
        pass


def _gh(client: httpx.Client, path: str, token: str, **params):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = client.get(f"{GH}{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def parent_sha(client: httpx.Client, repo: str, sha: str, token: str) -> str | None:
    """First parent of the fix commit = the buggy state (before the fix)."""
    try:
        data = _gh(client, f"/repos/{repo}/commits/{sha}", token)
        parents = data.get("parents") or []
        return parents[0]["sha"] if parents else None
    except (httpx.HTTPError, KeyError, IndexError):
        return None


def fetch_content(client: httpx.Client, repo: str, path: str, ref: str, token: str) -> str | None:
    """File text at a ref via the contents API (None if missing / not a file)."""
    try:
        import base64
        r = client.get(f"{GH}/repos/{repo}/contents/{path}", params={"ref": ref},
                       headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return None


def semgrep_scan(tree: dict[str, str], rules: str = DEFAULT_RULES, timeout: float = 600.0) -> str:
    """Run the official Semgrep image over `tree` (relpath -> text) in ONE pass.
    Returns raw JSON with paths normalized to the keys of `tree` ('/src/' stripped).
    All files share one invocation so the registry ruleset downloads only once."""
    if not tree:
        return '{"results": []}'
    with tempfile.TemporaryDirectory(prefix="pr-sentinel-sast-") as tmp:
        root = Path(tmp)
        for rel, text in tree.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(text, encoding="utf-8")
            except OSError:
                continue
        winp = str(root)  # docker (invoked directly, not via MSYS) accepts the host path
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{winp}:/src", "-e", "SEMGREP_SEND_METRICS=off",
                 "semgrep/semgrep", "semgrep", "--config", rules, "--json", "--quiet", "/src"],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "MSYS_NO_PATHCONV": "1"},
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"  ! semgrep/docker failed: {type(exc).__name__}", flush=True)
            return '{"results": []}'
        raw = proc.stdout or '{"results": []}'
        return raw.replace("/src/", "")


def _ns(i: int, path: str) -> str:
    """Namespace a file under its PR index so two repos can't collide in one tree."""
    return f"pr{i}/{path}"


def _padded_newside(patch: str) -> str:
    """Reconstruct the new-side hunk of a fixture patch as scannable text whose
    PHYSICAL line numbers equal the real new-file line numbers (gaps padded with
    blank lines). That alignment lets a Semgrep hit be parsed with the real
    added-line set and preset straight into the graph at the correct line."""
    lt = line_text_map(patch)
    if not lt:
        return ""
    return "\n".join(lt.get(n, "") for n in range(1, max(lt) + 1)) + "\n"


def run_fixtures(do_pipeline: bool, rules: str) -> int:
    """Measure SAST on the SEEDED fixtures — where the security bugs (Semgrep's
    domain) actually live, unlike the logic-bug realpr set. Phase 1 ($0): raw
    Semgrep catch on bug fixtures + FP on clean fixtures. Phase 2 (--pipeline):
    preset the Semgrep hits into the graph and check whether the verifier keeps
    the true catches and KILLS the false positives (the D35 grounding thesis)."""
    import yaml

    fx_dir = Path(__file__).parent / "fixtures"
    tree: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for i, p in enumerate(sorted(fx_dir.glob("*.yml"))):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        for f in d.get("files", []):
            patch = f.get("patch", "") or ""
            content = _padded_newside(patch)
            if not content.strip():
                continue
            ns = f"fx{i}"
            tree[f"{ns}/{f['path']}"] = content
            meta[ns] = {
                "name": p.stem, "file": f["path"], "patch": patch,
                "added": added_line_numbers(patch), "ltext": line_text_map(patch),
                "clean": p.stem.startswith("clean_") or p.stem == "mt_scary_title_clean",
                "fixture": d,
            }
            break  # one source file per fixture is enough for the SAST signal

    print(f"scanning {len(tree)} fixture file(s) (1 semgrep pass)…", flush=True)
    raw = semgrep_scan(tree, rules)

    sast: dict[str, list] = {}
    for ns, m in meta.items():
        key = f"{ns}/{m['file']}"
        sast[ns] = parse_semgrep_json(raw, {key: m["added"]}, {key: m["ltext"]})

    catch = clean_fp = 0
    print(f"\n=== Fixtures · raw Semgrep ($0) · rules={rules} ===")
    for ns in sorted(meta, key=lambda k: meta[k]["name"]):
        m = meta[ns]
        h = sast[ns]
        flag = ""
        if h and m["clean"]:
            flag = " <<< CLEAN-FP"
            clean_fp += 1
        elif h:
            catch += 1
        cats = ",".join(sorted({f.category.replace("sast-", "") for f in h}))
        print(f"  [{'HIT' if h else ' - '}] {m['name']:30s} {cats}{flag}")
    print(f"\nraw: {catch} bug-fixture catches · {clean_fp} clean-fixture false positives")
    _log(f"[sast fixtures raw rules={rules}] catches={catch} clean_fp={clean_fp}")

    if not do_pipeline:
        print("\n(Phase 2 verifier-filter arm skipped — pass --pipeline.)")
        return 0

    # Phase 2: preset Semgrep hits into the graph and see what the verifier does.
    from pr_sentinel.config import SentinelConfig
    from pr_sentinel.graph import build_graph
    from pr_sentinel.models import ChangedFile, PRMetadata
    from pr_sentinel.provider import OpenAICompatProvider, estimate_cost_usd

    api_key = os.environ.get("PR_SENTINEL_API_KEY", "")
    if not api_key:
        print("\n--pipeline needs PR_SENTINEL_API_KEY.")
        return 2

    async def run() -> int:
        base_url = os.environ.get("PR_SENTINEL_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("PR_SENTINEL_MODEL", "deepseek-v4-flash")
        provider = OpenAICompatProvider(api_key, base_url=base_url, model=model,
                                        max_concurrent=4, timeout_seconds=45)
        config = SentinelConfig()
        config.provider.model = model
        config.provider.base_url = base_url
        config.review.context_lines = 0
        config.output.inline = False
        tot_in = tot_out = 0

        # Only fixtures Semgrep actually flagged are interesting for the verifier arm.
        flagged = [ns for ns in meta if sast[ns]]
        kept_true = killed_fp = surfaced_fp = 0
        print(f"\n=== Phase 2 (preset SAST → anchor → verifier) · model={model} ===")
        for ns in sorted(flagged, key=lambda k: meta[k]["name"]):
            m = meta[ns]
            files = [ChangedFile(**f) for f in m["fixture"]["files"]]
            for f in files:
                f.additions = f.patch.count("\n+") if f.patch else 0
            preset = [s.model_copy(deep=True) for s in sast[ns]]
            for s in preset:
                s.file = m["file"]  # strip the fx namespace
            result = await build_graph(provider, github=None).ainvoke({
                "config": config,
                "pr": PRMetadata(repo="evals/fixture", number=1,
                                 title=m["fixture"].get("title", "")),
                "files": files,
                "findings": preset,
            })
            usage = result.get("usage")
            if usage:
                tot_in += usage.total_prompt
                tot_out += usage.total_completion
            survived = any(f.category.startswith("sast-")
                           for f in result.get("merged_findings", []))
            if m["clean"]:
                if survived:
                    surfaced_fp += 1
                else:
                    killed_fp += 1
            elif survived:
                kept_true += 1
            verdict = "SURVIVED" if survived else "killed"
            print(f"  {m['name']:30s} SAST hit {verdict}"
                  f"{'  (clean fixture)' if m['clean'] else ''}")
        cost, _ = estimate_cost_usd(model, tot_in, tot_out)
        await provider.aclose()
        line = (f"verifier kept {kept_true} true SAST catches · killed {killed_fp}/"
                f"{killed_fp + surfaced_fp} clean-fixture FPs ({surfaced_fp} leaked) · ≈${cost:.3f}")
        print(f"\n{line}")
        _log(f"[sast fixtures pipeline model={model} rules={rules}] {line}")
        return 0

    return asyncio.run(run())


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    rules = sys.argv[sys.argv.index("--rules") + 1] if "--rules" in sys.argv else DEFAULT_RULES
    do_pipeline = "--pipeline" in sys.argv

    if "--fixtures" in sys.argv:
        return run_fixtures(do_pipeline, rules)

    if realpr.CACHE.exists():
        manifest = json.loads(realpr.CACHE.read_text(encoding="utf-8"))
    else:
        manifest = realpr.discover(realpr.DEFAULT_REPOS, 3, token)
        realpr.CACHE.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if limit:
        manifest = manifest[:limit]
    print(f"SAST probe · rules={rules} · {len(manifest)} real PR(s)\n")

    # ---- Build the two scan trees (buggy/parent for recall, fixed for FP) ----
    buggy_tree: dict[str, str] = {}
    fixed_tree: dict[str, str] = {}
    meta: dict[int, dict] = {}  # i -> {entry, buggy_added, buggy_ltext, fixed_added, fixed_ltext, file}
    with httpx.Client() as client:
        for i, e in enumerate(manifest):
            rev = realpr.reverse_patch(e["patch"])
            fwd = e["patch"]
            psha = parent_sha(client, e["repo"], e["sha"], token)
            buggy = fetch_content(client, e["repo"], e["file"], psha, token) if psha else None
            fixed = fetch_content(client, e["repo"], e["file"], e["sha"], token)
            meta[i] = {
                "entry": e, "file": e["file"],
                "buggy_added": added_line_numbers(rev), "buggy_ltext": line_text_map(rev),
                "fixed_added": added_line_numbers(fwd), "fixed_ltext": line_text_map(fwd),
            }
            if buggy:
                buggy_tree[_ns(i, e["file"])] = buggy
            if fixed:
                fixed_tree[_ns(i, e["file"])] = fixed
            print(f"  fetched {e['repo']}#{e['pr']} {e['file']} "
                  f"(buggy={'ok' if buggy else 'MISS'} fixed={'ok' if fixed else 'MISS'})", flush=True)

    print(f"\nscanning {len(buggy_tree)} buggy + {len(fixed_tree)} fixed files (1 semgrep pass each)…",
          flush=True)
    buggy_raw = semgrep_scan(buggy_tree, rules)
    fixed_raw = semgrep_scan(fixed_tree, rules)

    # ---- Phase 1: raw Semgrep recall + FP (no LLM) ----
    # Reuse the product's tested parser per PR: key the added/line-text dicts by
    # the namespaced path so parse_semgrep_json's added-line + quotable-evidence
    # filtering is identical to the live `sast_node`.
    def hits_for(raw: str, i: int, added_key: str, ltext_key: str):
        ns = _ns(i, meta[i]["file"])
        return parse_semgrep_json(raw, {ns: meta[i][added_key]}, {ns: meta[i][ltext_key]})

    recall_hits = {i: hits_for(buggy_raw, i, "buggy_added", "buggy_ltext") for i in meta}
    fp_hits = {i: hits_for(fixed_raw, i, "fixed_added", "fixed_ltext") for i in meta}

    raw_recall = sum(1 for i in meta if recall_hits[i])
    raw_fp = sum(1 for i in meta if fp_hits[i])
    n = len(manifest) or 1
    print(f"\n=== Phase 1 (raw Semgrep, $0) · rules={rules} ===")
    for i in meta:
        e = meta[i]["entry"]
        tag = "HIT " if recall_hits[i] else "miss"
        fp = " [FP on fixed]" if fp_hits[i] else ""
        cats = ",".join(sorted({f.category for f in recall_hits[i]}))
        print(f"  [{tag}] {e['repo']}#{e['pr']} {meta[i]['file']}{fp} {cats}")
    line1 = (f"raw recall {raw_recall}/{n} ({100*raw_recall//n}%) · "
             f"raw FP on accepted code {raw_fp}/{n}")
    print(f"\n{line1}")
    _log(f"[sast phase1 rules={rules}] {line1}")

    if not do_pipeline:
        print("\n(Phase 2 LLM arms skipped — pass --pipeline to run them.)")
        if raw_recall == 0:
            print("Raw Semgrep caught 0 reintroduced bugs on this set → SAST cannot lift "
                  "realpr recall here (logic bugs, not textbook security). FP arm still informative.")
        return 0

    # ---- Phase 2: through the pipeline, baseline vs SAST-grounded (hits LLM) ----
    from pr_sentinel.config import SentinelConfig
    from pr_sentinel.graph import build_graph
    from pr_sentinel.models import PRMetadata
    from pr_sentinel.provider import OpenAICompatProvider, estimate_cost_usd

    api_key = os.environ.get("PR_SENTINEL_API_KEY", "")
    if not api_key:
        print("\n--pipeline needs PR_SENTINEL_API_KEY (real LLM).")
        return 2

    async def run_arms() -> int:
        base_url = os.environ.get("PR_SENTINEL_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("PR_SENTINEL_MODEL", "deepseek-v4-flash")
        provider = OpenAICompatProvider(api_key, base_url=base_url, model=model,
                                        max_concurrent=4, timeout_seconds=45)
        config = SentinelConfig()
        config.provider.model = model
        config.provider.base_url = base_url
        config.review.context_lines = 0
        config.output.inline = False
        tot_in = tot_out = 0

        async def recall_through_pipeline(preset_sast: bool) -> tuple[int, int]:
            caught = survived = 0
            nonlocal tot_in, tot_out
            for i in meta:
                e = meta[i]["entry"]
                files = realpr.buggy_files(e)
                targets = meta[i]["buggy_added"]
                sast_findings = [f.model_copy(deep=True) for f in recall_hits[i]] if preset_sast else []
                # strip the pr-namespace so findings anchor against the real file path
                for f in sast_findings:
                    f.file = meta[i]["file"]
                state = {"config": config,
                         "pr": PRMetadata(repo=e["repo"], number=e["pr"], title="Update " + e["file"]),
                         "files": files}
                if sast_findings:
                    state["findings"] = sast_findings
                result = await build_graph(provider, github=None).ainvoke(state)
                usage = result.get("usage")
                if usage:
                    tot_in += usage.total_prompt
                    tot_out += usage.total_completion
                merged = result.get("merged_findings", [])
                hit = any(f.file == e["file"] and any(
                    t in range(f.line_start, f.line_end + 1) for t in targets) for f in merged)
                caught += hit
                # did a preset SAST finding survive anchoring+verifier into the output?
                if preset_sast and any(f.category.startswith("sast-") for f in merged):
                    survived += 1
            return caught, survived

        base_caught, _ = await recall_through_pipeline(preset_sast=False)
        sast_caught, sast_survived = await recall_through_pipeline(preset_sast=True)
        cost, _ = estimate_cost_usd(model, tot_in, tot_out)
        await provider.aclose()
        line2 = (f"pipeline recall: baseline {base_caught}/{n} → +SAST {sast_caught}/{n} "
                 f"· raw hits {raw_recall}, survived verifier {sast_survived} · ≈${cost:.3f}")
        print(f"\n=== Phase 2 (through pipeline) · model={model} ===\n{line2}")
        _log(f"[sast phase2 model={model} rules={rules}] {line2}")
        return 0

    return asyncio.run(run_arms())


if __name__ == "__main__":
    sys.exit(main())
