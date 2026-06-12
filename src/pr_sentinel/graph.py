"""The LangGraph orchestration (D3, extended in V2): fan-out/fan-in plus an
adjudication stage. Still no loops.

            +-> architect --+
            +-> security  --+
ingest -----+-> performance -+--> merge_findings --> verifier --> reviewer --> publish
            +-> test      --+   (anchor evidence,     (LLM        (LLM       (inline +
                                 vote already done     adjudic-    semantic   summary,
                                 per-agent; dedup,     ation,      merge +    scrub,
                                 threshold, cap)       fail-open)  prose)     upsert)

V2 additions, all deterministic-first:
- ingest extends hunks with real context lines from the head ref (A7);
- merge_findings anchors every finding's quoted evidence against the diff
  line map and drops what doesn't exist (A2) — hallucinations can't post;
- verifier is a separate LLM pass that confirms/rejects each surviving
  finding before the reviewer writes prose (A4);
- publish posts verified findings as inline review comments and keeps the
  sticky summary comment (B1).
"""

from __future__ import annotations

import asyncio
import logging

from langgraph.graph import END, START, StateGraph

from .agents import run_analyst, run_cross_file, run_describe, run_reviewer, run_verifier
from .chunking import apply_skip_rules, build_chunks, build_pr_map
from .config import ANALYST_AGENTS, SentinelConfig
from .diffmap import added_line_numbers, extend_patch
from .formatter import format_dry_run, format_inline_body, format_review
from .github_client import GitHubClient
from .merge import merge_findings
from .models import AgentName, ReviewState, UsageStats
from .provider import LLMProvider
from .security import scrub_secrets
from .suppression import apply_suppressions
from .verification import anchor_findings

logger = logging.getLogger(__name__)


def _risk_labels(findings) -> list[str]:
    """PR labels derived from findings (V2 P8). Additive, capped, deterministic."""
    if not findings:
        return ["pr-sentinel:clean"]
    labels: set[str] = set()
    for f in findings:
        agent = f.agent.value
        if agent == AgentName.SECURITY.value:
            labels.add("security")
        elif agent == AgentName.PERFORMANCE.value:
            labels.add("performance")
        elif agent == AgentName.TEST.value:
            labels.add("needs-tests")
    if any(f.severity.value in ("critical", "high") for f in findings):
        labels.add("pr-sentinel:needs-attention")
    return sorted(labels)


async def _publish_check_run(github, state, config, findings, secrets) -> None:
    """Post a Check Run whose conclusion gates the merge when findings reach
    the configured severity (V2 P2). Fail-open: a check error never aborts."""
    from .models import Severity

    try:
        gate = Severity(config.gate.level.lower())
    except ValueError:
        return
    blocking = [f for f in findings if f.severity.rank <= gate.rank]
    conclusion = "failure" if blocking else "success"
    title = (
        f"{len(blocking)} blocking finding(s) at or above {gate.value}"
        if blocking else "No blocking findings"
    )
    summary_lines = [
        f"PR Sentinel found {len(findings)} finding(s); {len(blocking)} at or above "
        f"`{gate.value}` (the merge gate)."
    ]
    for f in blocking[:20]:
        summary_lines.append(f"- `{f.file}:{f.line_start}` — {f.severity.value}: {f.message}")
    annotations = [
        {
            "path": f.file,
            "start_line": max(1, f.line_start),
            "end_line": max(f.line_start, f.line_end),
            "annotation_level": "failure" if f.severity.rank <= gate.rank else "warning",
            "message": scrub_secrets(f.message, secrets),
            "title": f"{f.severity.value}: {f.category}",
        }
        for f in findings if f.line_start > 0
    ]
    await github.create_check_run(
        state["pr"].head_sha,
        conclusion=conclusion,
        title=title,
        summary=scrub_secrets("\n".join(summary_lines), secrets),
        annotations=annotations,
    )


def build_graph(
    provider: LLMProvider,
    github: GitHubClient | None,
    known_secrets: list[str] | None = None,
):
    """Compile the review graph. `github=None` (tests/evals) skips network I/O:
    ingest then expects `files` preset in the state and publish only formats."""
    secrets = known_secrets or []

    async def ingest(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        files = state.get("files")
        if files is None:
            assert github is not None, "no files in state and no GitHub client"
            files = await github.list_pr_files(state["pr"].number)
        files = apply_skip_rules(files, config)

        # V2 P3: incremental review — on a re-review, skip files unchanged since
        # the last reviewed commit so we don't re-flag (and re-bill) settled
        # code. Fail-open: any problem reverts to a full review.
        if (
            github is not None and config.review.incremental and not config.dry_run
            and state["pr"].head_sha
        ):
            last_sha = await github.last_reviewed_sha(state["pr"].number)
            if last_sha and last_sha != state["pr"].head_sha:
                changed = await github.compare_changed_paths(last_sha, state["pr"].head_sha)
                if changed is not None:
                    n = 0
                    for f in files:
                        if not f.skipped and f.path not in changed:
                            f.skipped = True
                            f.skip_reason = "unchanged since the last review (incremental)"
                            n += 1
                    if n:
                        logger.info("Incremental: skipped %d file(s) unchanged since %s.",
                                    n, last_sha[:7])

        # V2 A7: extend hunks with real surrounding lines from the head ref.
        # Fetched in PARALLEL (F5) — serial round-trips were on the critical
        # path before any analyst ran. Failures (missing file, wrong ref) leave
        # the original patch — extra context is an upgrade, never a requirement.
        head = state["pr"].head_sha
        if github is not None and config.review.context_lines > 0 and not config.dry_run and head:
            targets = [f for f in files if not f.skipped and f.patch]

            async def _fetch(f):
                try:
                    return f, await github.get_file_from_ref(f.path, head)
                except Exception:  # noqa: BLE001 — context is best-effort
                    return f, None

            for f, content in await asyncio.gather(*(_fetch(f) for f in targets)):
                if content:
                    f.patch = extend_patch(f.patch, content, config.review.context_lines)

        chunks = build_chunks(files, config)
        pr_map = build_pr_map(state["pr"].title, files)
        return {"files": files, "chunks": chunks, "pr_map": pr_map}

    def make_analyst_node(agent: AgentName):
        async def analyst(state: ReviewState) -> dict:
            config: SentinelConfig = state["config"]
            if agent not in config.agents.enabled or config.dry_run:
                return {}
            findings, usage, error = await run_analyst(
                agent, provider, state["pr_map"], state["chunks"], config
            )
            update: dict = {"findings": findings, "usage": usage}
            if error is not None:
                update["errors"] = [error]
            return update

        return analyst

    def merge_node(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        files = state.get("files", [])
        # V2 A2: evidence anchoring BEFORE dedup — every finding that survives
        # this line points at code that literally exists in the diff.
        anchored, dropped = anchor_findings(state.get("findings", []), files)
        if dropped:
            logger.info("Evidence anchoring dropped %d unverifiable finding(s).", dropped)
        # V2 P4: author-controlled suppression (config globs + inline markers).
        anchored, suppressed = apply_suppressions(anchored, files, config.review.suppress)
        if suppressed:
            logger.info("Suppressed %d finding(s) per config/inline markers.", suppressed)
        merged, clusters = merge_findings(anchored, config.min_severity)
        return {"merged_findings": merged, "_clusters": clusters}

    async def verifier_node(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        merged = state.get("merged_findings", [])
        if config.dry_run or not config.accuracy.verifier or not merged:
            return {}
        kept, usage, error = await run_verifier(
            provider, state["pr_map"], merged, state.get("files", []), config
        )
        from .merge import cluster_by_proximity

        update: dict = {
            "merged_findings": kept,
            "_clusters": cluster_by_proximity(kept),
            "usage": usage,
        }
        if error is not None:
            update["errors"] = [error]
        return update

    async def cross_file_node(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        if config.dry_run or not config.accuracy.cross_file:
            return {}
        new_findings, usage = await run_cross_file(
            provider, state["pr_map"], state.get("chunks", []),
            state.get("merged_findings", []), config,
        )
        if not new_findings:
            return {"usage": usage}
        # Anchor + suppress the new findings like any other, then re-merge.
        from .merge import merge_findings
        from .suppression import apply_suppressions

        anchored, _ = anchor_findings(new_findings, state.get("files", []))
        anchored, _ = apply_suppressions(anchored, state.get("files", []), config.review.suppress)
        combined = state.get("merged_findings", []) + anchored
        merged, clusters = merge_findings(combined, config.min_severity)
        return {"merged_findings": merged, "_clusters": clusters, "usage": usage}

    async def reviewer_node(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        clusters = state.get("_clusters") or []
        if config.dry_run or not clusters:
            return {"final_review": "", "merged_findings": state.get("merged_findings", [])}
        verdict, findings, usage, error = await run_reviewer(
            provider, state["pr_map"], clusters, config
        )
        update: dict = {
            "final_review": verdict,
            "merged_findings": findings,
            "usage": usage,
        }
        if error is not None:
            update["errors"] = [error]
        return update

    async def publish(state: ReviewState) -> dict:
        config: SentinelConfig = state["config"]
        files = state.get("files", [])

        if config.dry_run:
            est_tokens = sum(c.est_tokens for c in state.get("chunks", []))
            comment = format_dry_run(
                files,
                est_tokens * len(config.agents.enabled) * config.accuracy.samples,
                config.provider.resolved_analyst_model,
                len(state.get("chunks", [])),
            )
            comment = scrub_secrets(comment, secrets)
            if github is not None:
                await github.upsert_comment(state["pr"].number, comment)
            return {"final_review": comment}

        findings = state.get("merged_findings", [])
        run_usage = state.get("usage", UsageStats())

        # V2 B4 (opt-in): maintain the PR description between markers. Run it
        # BEFORE formatting so its tokens are counted in the footer (F7).
        if config.describe and github is not None:
            description, desc_usage = await run_describe(
                provider, state["pr_map"], state.get("chunks", []), config
            )
            run_usage = run_usage.merge(desc_usage)
            if description is not None:
                from .formatter import format_description

                await github.update_pr_description(
                    state["pr"].number,
                    scrub_secrets(format_description(description), secrets),
                )

        # V2 B1: findings whose line is a verified `+` line become inline
        # review comments; the rest stay in the summary. Re-checked against
        # the deterministic line sets here so a reviewer-mangled line number
        # can't anchor a comment to the wrong code.
        inline_findings, summary_findings = [], []
        if config.output.inline and github is not None:
            addable = {f.path: added_line_numbers(f.patch or "") for f in files if not f.skipped}
            for finding in findings:
                if finding.line_start in addable.get(finding.file, set()):
                    inline_findings.append(finding)
                else:
                    summary_findings.append(finding)
        else:
            summary_findings = list(findings)

        inline_posted = False
        if inline_findings and github is not None:
            comments = [
                {
                    "path": f.file,
                    "line": f.line_start,
                    "body": scrub_secrets(
                        format_inline_body(f, suggestions=config.output.suggestions), secrets
                    ),
                }
                for f in inline_findings
            ]
            # V2 P7: submit as REQUEST_CHANGES when a finding meets the
            # configured severity, so the PR shows a real "changes requested".
            event = "COMMENT"
            threshold = config.output.request_changes_at.strip().lower()
            if threshold:
                from .models import Severity

                try:
                    floor = Severity(threshold)
                    if any(f.severity.rank <= floor.rank for f in findings):
                        event = "REQUEST_CHANGES"
                except ValueError:
                    pass
            inline_posted = await github.create_inline_review(
                state["pr"].number, state["pr"].head_sha, comments, event=event
            )
        if not inline_posted:
            # Fail-open: inline rejected -> everything back into the summary.
            summary_findings = list(findings)
            inline_findings = []

        agents_run = len(config.agents.enabled) + 1 + (1 if config.accuracy.verifier else 0)
        comment = format_review(
            summary_findings,
            files,
            run_usage,
            config.provider.resolved_analyst_model,
            verdict=state.get("final_review", ""),
            errors=state.get("errors", []),
            warnings=config.warnings,
            agents_run=agents_run,
            inline_findings=inline_findings,
        )
        # Embed the reviewed head SHA so the next run can review incrementally.
        if state["pr"].head_sha:
            comment += f"\n<!-- pr-sentinel-sha:{state['pr'].head_sha} -->"
        comment = scrub_secrets(comment, secrets)
        if github is not None:
            await github.upsert_comment(state["pr"].number, comment)

        # V2 P2: optional Check Run for the Files tab + merge gating.
        if github is not None and config.gate.level != "off":
            await _publish_check_run(github, state, config, findings, secrets)

        # V2 P8: risk labels derived from findings.
        if github is not None and config.output.labels:
            await github.add_labels(state["pr"].number, _risk_labels(findings))

        return {"final_review": comment}

    graph = StateGraph(ReviewState)
    graph.add_node("ingest", ingest)
    for agent in ANALYST_AGENTS:
        graph.add_node(agent.value, make_analyst_node(agent))
        graph.add_edge("ingest", agent.value)
        graph.add_edge(agent.value, "merge_findings")
    graph.add_node("merge_findings", merge_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("cross_file", cross_file_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("publish", publish)
    graph.add_edge(START, "ingest")
    graph.add_edge("merge_findings", "verifier")
    graph.add_edge("verifier", "cross_file")
    graph.add_edge("cross_file", "reviewer")
    graph.add_edge("reviewer", "publish")
    graph.add_edge("publish", END)
    return graph.compile()
