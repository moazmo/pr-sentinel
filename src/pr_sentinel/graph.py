"""The LangGraph orchestration (D3): a clean fan-out/fan-in, no loops.

            +-> architect --+
            +-> security  --+
ingest -----+-> performance -+--> merge_findings --> reviewer --> publish
            +-> test      --+     (deterministic)      (LLM)

- ingest: fetch files (D2), apply skip rules (D10), build PR map + chunks (D7).
- analysts: parallel branches (D4); a disabled or failed agent degrades the
  review, never aborts it.
- merge_findings: the deterministic pre-pass (D5 stage 1) as its OWN node so
  it is independently testable.
- reviewer: semantic dedup + final prose (D5 stage 2).
- publish: format (D8), scrub secrets, upsert the sticky comment.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from .agents import run_analyst, run_reviewer
from .chunking import apply_skip_rules, build_chunks, build_pr_map
from .config import ANALYST_AGENTS, SentinelConfig
from .formatter import format_dry_run, format_review
from .github_client import GitHubClient
from .merge import merge_findings
from .models import AgentName, ReviewState, UsageStats
from .provider import LLMProvider
from .security import scrub_secrets

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
        merged, clusters = merge_findings(state.get("findings", []), config.min_severity)
        return {"merged_findings": merged, "_clusters": clusters}

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
        if config.dry_run:
            est_tokens = sum(c.est_tokens for c in state.get("chunks", []))
            comment = format_dry_run(
                state["files"],
                est_tokens * len(config.agents.enabled),
                config.provider.model,
                len(state.get("chunks", [])),
            )
        else:
            agents_run = len(config.agents.enabled) + 1  # + reviewer
            comment = format_review(
                state.get("merged_findings", []),
                state["files"],
                state.get("usage", UsageStats()),
                config.provider.model,
                verdict=state.get("final_review", ""),
                errors=state.get("errors", []),
                warnings=config.warnings,
                agents_run=agents_run,
            )
        comment = scrub_secrets(comment, secrets)
        if github is not None:
            await github.upsert_comment(state["pr"].number, comment)
        return {"final_review": comment}

    graph = StateGraph(ReviewState)
    graph.add_node("ingest", ingest)
    for agent in ANALYST_AGENTS:
        graph.add_node(agent.value, make_analyst_node(agent))
        graph.add_edge("ingest", agent.value)
        graph.add_edge(agent.value, "merge_findings")
    graph.add_node("merge_findings", merge_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("publish", publish)
    graph.add_edge(START, "ingest")
    graph.add_edge("merge_findings", "reviewer")
    graph.add_edge("reviewer", "publish")
    graph.add_edge("publish", END)
    return graph.compile()
