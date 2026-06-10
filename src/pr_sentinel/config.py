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


class SentinelConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    min_severity: Severity = Severity.MEDIUM
    ignore: list[str] = Field(default_factory=list)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    dry_run: bool = False

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
