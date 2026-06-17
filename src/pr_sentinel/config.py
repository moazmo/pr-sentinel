"""Configuration: `.pr-sentinel.yml`, Pydantic-validated, defaults-first.

Two rules from the threat model and NFR1:
- The config is read from the BASE branch, never the PR head (a hostile PR
  must not be able to disable the security agent or raise spend caps).
  That read happens in github_client; this module only parses.
- Malformed config never crashes CI: fall back to defaults and surface a
  warning that ends up in the PR comment.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from .models import AgentName, Severity

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".pr-sentinel.yml"

ANALYST_AGENTS = (
    AgentName.ARCHITECT,
    AgentName.SECURITY,
    AgentName.PERFORMANCE,
    AgentName.TEST,
)


class ProviderConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5-mini"
    api_key_env: str = "PR_SENTINEL_API_KEY"
    # "openai-compat" (default) or "anthropic" (native Messages API).
    kind: str = "openai-compat"
    # Two-tier routing (V2 A5): analysts may use a cheaper model than the
    # verifier/reviewer. Both default to `model` when unset.
    analyst_model: str | None = None
    review_model: str | None = None

    @property
    def resolved_analyst_model(self) -> str:
        return self.analyst_model or self.model

    @property
    def resolved_review_model(self) -> str:
        return self.review_model or self.model


class AccuracyConfig(BaseModel):
    """V2 accuracy core knobs. Defaults tuned for cheap-model ensembles."""

    # Self-consistency samples per analyst per chunk. >1 trades a little
    # cached-input cost for a large variance cut (findings are majority-voted).
    samples: int = Field(default=3, ge=1, le=5)
    # Findings must appear in at least this many samples to survive the vote
    # (high/critical findings are exempt — they go to evidence verification).
    min_support: int = Field(default=2, ge=1, le=5)
    # The adjudication pass: one batched LLM call that confirms/rejects each
    # merged finding against the numbered diff before the reviewer writes prose.
    verifier: bool = True
    # Adaptive sampling (V2 P12): spend the first sample, and only draw the
    # remaining samples for chunks where that sample found something worth
    # re-checking. Saves ~40% of calls on clean code without losing the vote
    # where it matters. No effect when samples == 1.
    adaptive: bool = True
    # Opt-in final pass that flags cross-file issues per-file analysts miss
    # (V2 P13). One extra LLM call.
    cross_file: bool = False
    # --- V2.5 research levers (RESEARCH_SYNTHESIS_2026-06-12) -------------------
    # All four ship OFF by default. They were built from two independent research
    # passes and A/B-measured on the 37-fixture benchmark: on cheap flash, every
    # lever arm landed within run-to-run noise of the levers-off baseline (91%) —
    # no measurable accuracy gain. Honest-numbers rule (CLAUDE.md / D29): we don't
    # flip a default that changes review behavior without an eval that justifies
    # it, so they stay opt-in. `mode: thorough` turns them on for max-recall users.
    # See docs/V2.5_LEVERS_2026-06-13.md and DECISIONS D29-D34.
    #
    # Confirmation-bias debiasing (L1): judge the code on its own merits, ignore
    # the PR title's reassurance/alarm (arXiv 2603.18740). Accuracy-neutral on
    # flash here, but real security value (a hostile title can't lower scrutiny),
    # so it's the lever most worth enabling — opt-in.
    debias: bool = False
    # Calibration prefix (L5): per-agent when-to-flag / when-to-stay-silent
    # anchors, front-loaded so they sit in the cached prefix (~free per call).
    # Measured ≈ baseline (a first cut over-primed test-agent false positives;
    # the precision-rebalanced version is neutral). Opt-in.
    calibration: bool = False
    # Chain-of-thought (L2/L3): "off" | "brief". "brief" emits a short top-level
    # `analysis` scan before the findings array (parser ignores non-finding keys;
    # the ensemble votes on findings, not reasoning). Verdict-first per finding
    # (PromptAudit 2605.24171). Opt-in; on in `thorough`.
    cot: str = "off"
    # Prompt-diverse ensemble (L4): when sampling (samples > 1), give each sample
    # a different lens (standard / checklist / adversarial) instead of only a
    # temperature jitter (Self-MoA, 2502.00674). Opt-in; on in `thorough`.
    # NOTE: DeepSeek V4 thinking mode ignores temperature, so when analyst_thinking
    # is on, lenses are the ONLY working diversity source for the ensemble.
    lenses: bool = False
    # Reasoning controls (DeepSeek V4: thinking is a request parameter, ON by
    # default; temperature is inert while thinking is enabled). These are
    # DeepSeek-specific — the `thinking` field is only sent to the provider when
    # NOT None, so non-DeepSeek OpenAI-compatible endpoints are unaffected by the
    # default. `analyst_thinking`: None = leave the provider default (DeepSeek =
    # on); False = disable for the four analysts (~5 vs ~700 output tokens per
    # call AND restores temperature-driven ensemble diversity); True = force on.
    # The verifier/reviewer keep the provider default (they're the judgment gate).
    # `reasoning_effort` ("" | low | medium | high) tunes depth when thinking is on.
    analyst_thinking: bool | None = None
    reasoning_effort: str = ""
    # Repository-context prefetch (L3): deterministically fetch definitions of the
    # symbols the diff references (same-file siblings + imported modules) and hand
    # analysts a bounded, delimited context block — to judge the context-dependent
    # bugs a ±N-line diff can't (the bulk of the real-PR recall gap). Python-first,
    # opt-in, live-path (needs head-ref fetches). Off by default until a measured
    # win on the real-PR benchmark (more context can also hurt — D34/SWE-PRBench).
    repo_context: bool = False


class AgentsConfig(BaseModel):
    enabled: list[AgentName] = Field(default_factory=lambda: list(ANALYST_AGENTS))
    # Repo-specific guidance appended to every analyst prompt (V2 P5), e.g.
    # "This is a Django project; ignore TODO comments."
    guidance: str = Field(default="", max_length=2000)
    # Per-agent guidance keyed by agent name (architect|security|...).
    instructions: dict[str, str] = Field(default_factory=dict)


class LimitsConfig(BaseModel):
    max_files: int = Field(default=35, ge=1, le=300)
    max_input_tokens: int = Field(default=120_000, ge=1_000)
    max_output_tokens_per_agent: int = Field(default=2_000, ge=100, le=32_000)
    # Input-token budget for a single LLM call; small files are batched up to it.
    tokens_per_call: int = Field(default=12_000, ge=1_000)
    max_concurrent_requests: int = Field(default=8, ge=1, le=32)
    agent_timeout_seconds: float = Field(default=120.0, gt=0)


class ReviewConfig(BaseModel):
    include_deletions: bool = False
    language_hint: str = ""
    # Extra context lines fetched from the head ref around each hunk (V2 A7).
    # 0 disables the extra contents-API calls entirely.
    context_lines: int = Field(default=8, ge=0, le=25)
    # Review only changes since the last review on `synchronize` (V2 P3).
    incremental: bool = True
    # Permanently silence findings (V2 P4). Each entry is "<path-glob>" or
    # "<path-glob>:<category-glob>", e.g. "legacy/**" or "api/*.py:nit".
    suppress: list[str] = Field(default_factory=list)


class OutputConfig(BaseModel):
    # Post findings as inline review comments anchored to diff lines (V2 B1);
    # unanchorable findings stay in the summary comment either way.
    inline: bool = True
    # Render concrete one-line fixes as GitHub ```suggestion blocks (V2 P1).
    suggestions: bool = True
    # Submit the inline review as REQUEST_CHANGES when an unresolved finding is
    # at/above this severity; "" disables (V2 P7). critical|high|medium|...
    request_changes_at: str = ""
    # Apply PR labels derived from findings (V2 P8).
    labels: bool = False


class SastConfig(BaseModel):
    """SAST grounding (research lever L1): run Semgrep over the changed files and
    feed its hits into the pipeline as candidate findings the verifier triages.
    Off by default — it needs Semgrep in the runner image and the head-ref file
    contents (live path only; the static-fixture eval can't exercise it). Hits are
    filtered to added lines and go through the same anchoring + verifier as any
    finding, so the rule engine's noise is filtered by the LLM (SAST-Genius pattern)."""

    enabled: bool = False
    # Semgrep --config value: a registry pack (e.g. "p/default", "p/ci") | a path.
    # NOT "auto": `--config auto` refuses to run when telemetry is disabled and
    # pings semgrep.dev to pick rules — wrong for a privacy-first tool (measured
    # 2026-06-17, D39). "p/default" is the broad OSS pack, telemetry-free.
    rules: str = "p/default"


class GateConfig(BaseModel):
    """Merge-gating via a GitHub Check Run (V2 P2). The check fails when an
    unresolved finding is at/above `level`; "off" never fails the check."""

    # off | critical | high | medium | low | nit
    level: str = "off"


class SentinelConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    accuracy: AccuracyConfig = Field(default_factory=AccuracyConfig)
    min_severity: Severity = Severity.MEDIUM
    ignore: list[str] = Field(default_factory=list)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    sast: SastConfig = Field(default_factory=SastConfig)
    # Maintain a PR description between markers in the PR body (V2 B4).
    describe: bool = False
    dry_run: bool = False
    # One-liner preset that sets accuracy knobs (V2 P6): fast|balanced|thorough.
    mode: str = ""

    def model_post_init(self, _ctx) -> None:
        """A preset (`mode`) is a low-friction front-end over the accuracy
        block and wins over individual accuracy knobs when set — use one or
        the other, not both."""
        if self.mode == "fast":
            self.accuracy.samples = 1
            self.accuracy.min_support = 1
            self.accuracy.verifier = False
            self.accuracy.debias = False
            self.accuracy.calibration = False
            self.accuracy.lenses = False
            self.accuracy.cot = "off"
        elif self.mode == "thorough":
            self.accuracy.samples = 3
            self.accuracy.min_support = 2
            self.accuracy.verifier = True
            # Max-recall: turn on every research lever for users who want them.
            self.accuracy.debias = True
            self.accuracy.calibration = True
            self.accuracy.lenses = True
            self.accuracy.cot = "brief"
        # "balanced" / "" keep the defaults (samples=3, verifier on, research
        # levers off — measured ≈ baseline on flash, so off by default; D29).

    # Not user-facing config; carries parse warnings into the final comment.
    warnings: list[str] = Field(default_factory=list)


def load_config(raw_yaml: str | None) -> SentinelConfig:
    """Parse user YAML into a SentinelConfig. Any problem -> defaults + warning.

    Unknown keys are dropped with a warning rather than rejected, so a config
    written for a future version still works on an old one.
    """
    if not raw_yaml or not raw_yaml.strip():
        return SentinelConfig()

    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        logger.warning("Malformed %s, using defaults: %s", CONFIG_FILENAME, exc)
        return SentinelConfig(
            warnings=[f"`{CONFIG_FILENAME}` could not be parsed; defaults were used."]
        )

    if data is None:
        return SentinelConfig()
    if not isinstance(data, dict):
        return SentinelConfig(
            warnings=[f"`{CONFIG_FILENAME}` is not a mapping; defaults were used."]
        )

    known = set(SentinelConfig.model_fields) - {"warnings"}
    unknown = [k for k in data if k not in known]
    cleaned: dict[str, Any] = {k: v for k, v in data.items() if k in known}

    try:
        config = SentinelConfig(**cleaned)
    except ValidationError as exc:
        logger.warning("Invalid %s, using defaults: %s", CONFIG_FILENAME, exc)
        return SentinelConfig(
            warnings=[f"`{CONFIG_FILENAME}` failed validation; defaults were used."]
        )

    if unknown:
        config.warnings.append(
            f"Unknown config keys ignored: {', '.join(sorted(unknown))}."
        )
    return config
