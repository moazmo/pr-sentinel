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


class AgentsConfig(BaseModel):
    enabled: list[AgentName] = Field(default_factory=lambda: list(ANALYST_AGENTS))


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
        elif self.mode == "thorough":
            self.accuracy.samples = 3
            self.accuracy.min_support = 2
            self.accuracy.verifier = True
        # "balanced" / "" keep the defaults (samples=3, verifier on).

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
