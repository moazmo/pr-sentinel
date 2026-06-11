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

import logging

from langgraph.graph import END, START, StateGraph

from .agents import run_analyst, run_describe, run_reviewer, run_verifier
from .chunking import apply_skip_rules, build_chunks, build_pr_map
from .config import ANALYST_AGENTS, SentinelConfig
from .diffmap import added_line_numbers, extend_patch
from .formatter import format_dry_run, format_inline_body, format_review
from .github_client import GitHubClient
from .merge import merge_findings
from .models import AgentName, ReviewState, UsageStats
from .provider import LLMProvider
from .security import scrub_secrets
from .verification import anchor_findings

logger = logging.getLogger(__name__)


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

        # V2 A7: extend hunks with real surrounding lines from the head ref.
        # Failures (missing file, wrong ref) leave the original patch — extra
        # context is an upgrade, never a requirement.
        if github is not None and config.review.context_lines > 0 and not config.dry_run:
            head = state["pr"].head_sha
            for f in files:
                if f.skipped or not f.patch or not head:
                    continue
                try:
                    content = await github.get_file_from_ref(f.path, head)
                except Exception:  # noqa: BLE001 — context is best-effort
                    content = None
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
        # V2 A2: evidence anchoring BEFORE dedup — every finding that survives
        # this line points at code that literally exists in the diff.
        anchored, dropped = anchor_findings(state.get("findings", []), state.get("files", []))
        if dropped:
            logger.info("Evidence anchoring dropped %d unverifiable finding(s).", dropped)
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
                    "body": scrub_secrets(format_inline_body(f), secrets),
                }
                for f in inline_findings
            ]
            inline_posted = await github.create_inline_review(
                state["pr"].number, state["pr"].head_sha, comments
            )
        if not inline_posted:
            # Fail-open: inline rejected -> everything back into the summary.
            summary_findings = list(findings)
            inline_findings = []

        agents_run = len(config.agents.enabled) + 1 + (1 if config.accuracy.verifier else 0)
        comment = format_review(
            summary_findings,
            files,
            state.get("usage", UsageStats()),
            config.provider.resolved_analyst_model,
            verdict=state.get("final_review", ""),
            errors=state.get("errors", []),
            warnings=config.warnings,
            agents_run=agents_run,
            inline_findings=inline_findings,
        )
        comment = scrub_secrets(comment, secrets)
        if github is not None:
            await github.upsert_comment(state["pr"].number, comment)

        # V2 B4 (opt-in): maintain the PR description between markers.
        if config.describe and github is not None:
            description, usage = await run_describe(
                provider, state["pr_map"], state.get("chunks", []), config
            )
            if description is not None:
                from .formatter import format_description

                await github.update_pr_description(
                    state["pr"].number,
                    scrub_secrets(format_description(description), secrets),
                )

        return {"final_review": comment}

    graph = StateGraph(ReviewState)
    graph.add_node("ingest", ingest)
    for agent in ANALYST_AGENTS:
        graph.add_node(agent.value, make_analyst_node(agent))
        graph.add_edge("ingest", agent.value)
        graph.add_edge(agent.value, "merge_findings")
    graph.add_node("merge_findings", merge_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("publish", publish)
    graph.add_edge(START, "ingest")
    graph.add_edge("merge_findings", "verifier")
    graph.add_edge("verifier", "reviewer")
    graph.add_edge("reviewer", "publish")
    graph.add_edge("publish", END)
    return graph.compile()
