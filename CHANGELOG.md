# Changelog

All notable changes to PR Sentinel. Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions are git tags.

## [2.1.0] — 2026-06-12

### Added — adoption features (all $0; you bring the key)
- **One-click fix suggestions:** findings with a precise `fix` render as GitHub ```suggestion blocks.
- **Merge gating:** `gate.level` posts a Check Run that fails at/above a severity, so reviews can be required.
- **Incremental review:** on a re-review, only files changed since the last review are re-examined.
- **Finding suppression:** `review.suppress` globs and inline `pr-sentinel: ignore[category]` markers.
- **Custom guidance:** `agents.guidance` / `agents.instructions` (base-branch only).
- **Presets:** `mode: fast | balanced | thorough`.
- **Risk labels** (`output.labels`), **REQUEST_CHANGES** (`output.request_changes_at`), **merge-readiness/effort score**, **confidence display** (ensemble agreement).
- **Adaptive sampling** (`accuracy.adaptive`, default on): ~40% fewer calls on clean code.
- **Cross-file pass** (`accuracy.cross_file`, opt-in): catches stale-caller / signature-mismatch issues.
- Repo health: Dependabot, issue/PR templates, CODE_OF_CONDUCT, CHANGELOG, self-review (dogfood) workflow, digest-pinned base image, README badges.

### Fixed
- Pooled the GitHub API client (one connection, not one per request) + retry/backoff on transient GitHub errors.
- Context-line extension fetches file contents in parallel, not serially.
- Reworked the >65k-char comment cap so an optional verdict/inline index can't mis-slice the collapse.
- Reviewer findings recover dropped agent attribution / evidence / fix from the input.
- File-priority test detection uses path patterns, not a bare `test` substring.

## [2.0.1] — 2026-06-11
### Fixed
- Verifier retries once on unparseable output (parity with reviewer/analysts).

## [2.0.0] — 2026-06-11
### Added — accuracy-per-dollar core
- Line-numbered diffs; deterministic evidence anchoring (drops findings whose quote isn't in the diff).
- Self-consistency ensemble (3 samples/analyst, majority vote) and a verifier adjudication pass.
- Inline review comments; `@pr-sentinel review | ask | describe` commands (author-association gated).
- Native Anthropic provider, two-tier model routing, JSON-mode with fallback, pooled provider client, cache-aware cost reporting.
- 17-fixture / 5-language eval suite with hard negatives and two prompt-injection vectors; env-driven leaderboard runner.
- Leaderboard (deepseek-v4-flash, 51 fixture-runs): naive 47/51 with 2 clean-fixture false positives → v2 ensemble+verifier 49/51 with 0, ~$0.005/review.

## [1.0.3] — 2026-06-10
### Fixed
- Marketplace: unique action name and sub-125-char description.
### Changed
- Action manifest no longer embeds a `secrets`-context expression (it broke action loading; caught by live E2E).

## [1.0.0] — 2026-06-10
- First release: five-agent fan-out/fan-in reviewer, hybrid dedup, BYOK over the OpenAI-compatible protocol, sticky comment, cost guardrails, injection hardening, 7-fixture eval harness.
