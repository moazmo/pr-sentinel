"""Core data models. The Finding schema is the single source of truth:
every analyst emits it, the merge pass operates on it, the Reviewer consumes it.

Structured output doubles as a security boundary (threat model, Threat 2):
anything an analyst returns that does not validate against Finding is discarded,
so instruction text injected through the diff cannot reach the PR comment.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NIT = "nit"

    @property
    def rank(self) -> int:
        """Lower rank = more severe. Used for ordering and threshold filtering."""
        return _SEVERITY_ORDER[self]


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.NIT: 4,
}


class AgentName(str, Enum):
    ARCHITECT = "architect"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TEST = "test"
    REVIEWER = "reviewer"


class Finding(BaseModel):
    """One issue raised by one analyst agent."""

    agent: AgentName
    file: str
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    severity: Severity
    category: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    suggestion: str | None = Field(default=None, max_length=2000)
    # The exact offending line quoted from the diff (V2). Verification drops
    # any finding whose evidence does not literally exist in the patch.
    evidence: str | None = Field(default=None, max_length=500)
    # Filled by the merge pass when several agents raise the same issue.
    also_flagged_by: list[AgentName] = Field(default_factory=list)
    # How many ensemble samples raised this finding (V2 self-consistency).
    support: int = Field(default=1, ge=1)

    @field_validator("line_end")
    @classmethod
    def _end_not_before_start(cls, v: int, info) -> int:
        start = info.data.get("line_start", 0)
        return max(v, start)

    def overlaps(self, other: "Finding", slack: int = 0) -> bool:
        """True when both findings sit on the same file and their line ranges
        touch within `slack` lines."""
        if self.file != other.file:
            return False
        return (self.line_start - slack) <= other.line_end and (
            other.line_start - slack
        ) <= self.line_end


class ChangedFile(BaseModel):
    """A file from the GitHub `List pull request files` endpoint."""

    path: str
    status: str  # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    patch: str | None = None  # absent for binaries and oversized per-file diffs
    previous_path: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    truncated: bool = False
    truncation_note: str | None = None


class PRMetadata(BaseModel):
    repo: str  # "owner/name"
    number: int
    title: str = ""
    body: str = ""
    base_sha: str = ""
    head_sha: str = ""
    base_ref: str = ""
    author: str = ""


class UsageStats(BaseModel):
    """Token usage per agent, accumulated across all calls."""

    prompt_tokens: dict[str, int] = Field(default_factory=dict)
    completion_tokens: dict[str, int] = Field(default_factory=dict)
    cached_tokens: dict[str, int] = Field(default_factory=dict)

    def add(self, agent: str, prompt: int, completion: int, cached: int = 0) -> None:
        self.prompt_tokens[agent] = self.prompt_tokens.get(agent, 0) + prompt
        self.completion_tokens[agent] = self.completion_tokens.get(agent, 0) + completion
        if cached:
            self.cached_tokens[agent] = self.cached_tokens.get(agent, 0) + cached

    @property
    def total_prompt(self) -> int:
        return sum(self.prompt_tokens.values())

    @property
    def total_completion(self) -> int:
        return sum(self.completion_tokens.values())

    @property
    def total_cached(self) -> int:
        return sum(self.cached_tokens.values())

    def merge(self, other: "UsageStats") -> "UsageStats":
        merged = UsageStats(
            prompt_tokens=dict(self.prompt_tokens),
            completion_tokens=dict(self.completion_tokens),
            cached_tokens=dict(self.cached_tokens),
        )
        for agent, n in other.prompt_tokens.items():
            merged.prompt_tokens[agent] = merged.prompt_tokens.get(agent, 0) + n
        for agent, n in other.completion_tokens.items():
            merged.completion_tokens[agent] = merged.completion_tokens.get(agent, 0) + n
        for agent, n in other.cached_tokens.items():
            merged.cached_tokens[agent] = merged.cached_tokens.get(agent, 0) + n
        return merged


class AgentError(BaseModel):
    """A per-agent failure recorded for graceful degradation (NFR1):
    one analyst failing must not abort the other three."""

    agent: str
    message: str


def _merge_usage(a: UsageStats, b: UsageStats) -> UsageStats:
    return a.merge(b)


class ReviewState(TypedDict, total=False):
    """Shared LangGraph state. Analyst branches append findings/errors via
    reducers so parallel writes never clobber each other."""

    config: object  # SentinelConfig (kept loose to avoid a circular import)
    pr: PRMetadata
    files: list[ChangedFile]
    pr_map: str
    chunks: list[object]  # list[Chunk]
    findings: Annotated[list[Finding], operator.add]
    merged_findings: list[Finding]
    _clusters: list[list[Finding]]  # merge output handed to the reviewer
    final_review: str
    usage: Annotated[UsageStats, _merge_usage]
    errors: Annotated[list[AgentError], operator.add]
