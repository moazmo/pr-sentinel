"""Eval harness (D11): runs the REAL pipeline against seeded-bug fixtures with
a real LLM key, and prints the honest results table for the README.

This is NOT part of the CI test suite (CI never hits a live API). Run manually:

    PR_SENTINEL_API_KEY=sk-...  python evals/run.py
    # optional: PR_SENTINEL_BASE_URL, PR_SENTINEL_MODEL

Catch-rate measures whether the right agent finds the planted bug; the clean
fixtures measure the false-positive rate — the number that actually decides
whether people keep an AI reviewer installed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pr_sentinel.config import SentinelConfig  # noqa: E402
from pr_sentinel.graph import build_graph  # noqa: E402
from pr_sentinel.models import ChangedFile, Finding, PRMetadata, Severity  # noqa: E402
from pr_sentinel.provider import OpenAICompatProvider  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def check_expectations(name: str, fixture: dict, findings: list[Finding], comment: str) -> list[str]:
    """Return a list of failure descriptions (empty = pass)."""
    failures: list[str] = []
    expected = fixture.get("expected", {})

    if "max_findings" in expected and len(findings) > expected["max_findings"]:
        listed = "; ".join(f"{f.agent.value}:{f.category}@{f.file}" for f in findings)
        failures.append(f"expected <= {expected['max_findings']} findings, got {len(findings)} ({listed})")

    if "min_findings" in expected and len(findings) < expected["min_findings"]:
        failures.append(f"expected >= {expected['min_findings']} findings, got {len(findings)}")

    for needle in expected.get("must_not_contain", []):
        if needle in comment:
            failures.append(f"output leaked forbidden string: {needle!r}")

    for want in expected.get("findings", []):
        floor = Severity(want.get("min_severity", "nit"))

        def matches(f: Finding) -> bool:
            if f.file != want["file"]:
                return False
            agents = [f.agent.value] + [a.value for a in f.also_flagged_by]
            if want.get("agent") and want["agent"] not in agents:
                return False
            blob = f"{f.category} {f.message}".lower()
            if not any(s.lower() in blob for s in want.get("category_contains", [""])):
                return False
            return f.severity.rank <= floor.rank

        if not any(matches(f) for f in findings):
            failures.append(
                f"no {want.get('agent', 'any')}-agent finding matching "
                f"{want.get('category_contains')} at {want['file']} (>= {floor.value})"
            )
    return failures


async def run_fixture(provider, fixture: dict) -> tuple[list[Finding], str]:
    files = [ChangedFile(**f) for f in fixture["files"]]
    for f in files:
        f.additions = f.patch.count("\n+") if f.patch else 0
    config = SentinelConfig()
    graph = build_graph(provider, github=None)
    result = await graph.ainvoke(
        {
            "config": config,
            "pr": PRMetadata(repo="evals/fixture", number=1, title=fixture.get("title", "")),
            "files": files,
        }
    )
    return result.get("merged_findings", []), result.get("final_review", "")


async def main() -> int:
    api_key = os.environ.get("PR_SENTINEL_API_KEY", "")
    if not api_key:
        print("Set PR_SENTINEL_API_KEY to run evals (they hit a real LLM).")
        return 2
    base_url = os.environ.get("PR_SENTINEL_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("PR_SENTINEL_MODEL", "gpt-5-mini")
    provider = OpenAICompatProvider(api_key, base_url=base_url, model=model)

    runs = 1
    if "--runs" in sys.argv:
        runs = max(1, int(sys.argv[sys.argv.index("--runs") + 1]))

    fixtures = sorted(FIXTURES_DIR.glob("*.yml"))
    # results[name] = list of failure-lists, one per run (empty list = pass)
    results: dict[str, list[list[str]]] = {p.stem: [] for p in fixtures}

    for run_index in range(1, runs + 1):
        if runs > 1:
            print(f"--- run {run_index}/{runs} ---")
        for path in fixtures:
            fixture = load_fixture(path)
            findings, comment = await run_fixture(provider, fixture)
            failures = check_expectations(path.stem, fixture, findings, comment)
            results[path.stem].append(failures)
            status = "✅ pass" if not failures else "❌ FAIL"
            detail = "; ".join(failures) if failures else f"{len(findings)} finding(s)"
            print(f"[{status}] {path.stem}: {detail}")

    total_passes = sum(1 for runs_list in results.values() for f in runs_list if not f)
    total_cells = len(fixtures) * runs

    print("\n--- README table ---\n")
    print(f"Evals: {runs} run(s) on `{model}`, {date.today().isoformat()}:\n")
    print("| Fixture | Passed | Notes |")
    print("|---|---|---|")
    for name, runs_list in results.items():
        n_pass = sum(1 for f in runs_list if not f)
        # Most common failure reason, if any — keeps the table honest and short.
        reasons = [f[0] for f in runs_list if f]
        note = reasons[0] if reasons else ""
        print(f"| `{name}` | {n_pass}/{runs} | {note} |")
    print(f"\n**{total_passes}/{total_cells} fixture-runs passed.**")
    return 0 if total_passes == total_cells else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
