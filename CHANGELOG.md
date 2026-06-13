# Changelog

All notable changes to PR Sentinel. Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions are git tags.

## [2.6.0] — 2026-06-13 — structural levers (reasoning controls, SAST grounding, real-PR benchmark)

After v2.5 proved prompt-level levers are at the cheap-model ceiling, this release adds the *structural* levers the research pointed to. All behavior-preserving (new knobs default off / provider-default).

### Added
- **Reasoning controls** (`accuracy.analyst_thinking`, `accuracy.reasoning_effort`): DeepSeek V4 thinking is a request parameter (default on). Tri-state `analyst_thinking` is endpoint-safe (the `thinking` field is sent only when explicitly set, so non-DeepSeek endpoints are unaffected). Measured finding: **thinking is essential** — disabling it on analysts drops ~91% → ~61%, so it stays on (D36).
- **SAST grounding** (`sast.enabled`, default off): runs Semgrep over the changed files and feeds its hits into the verifier's triage — the documented 2025-26 precision lever. Opt-in, fail-open, live-path (needs Semgrep in the runner) (D35).
- **Real-PR benchmark** (`evals/realpr.py`): discovers real merged bug-fix PRs from the GitHub API, reverses them to reintroduce the bug, and scores recall on real bugs — the honest metric the seeded fixtures can't give. First measured result: **4/11 (36%)** recall on real reintroduced bugs (vs 91% on seeded fixtures) — the context-dependent gap the agentic-context roadmap targets.

### Notes
- Premium-tier distilled/RL-tuned review model specced in ROADMAP (the path past the cheap-model capability ceiling).

## [2.5.0] — 2026-06-13 — research levers

### Added — research levers (all $0; config toggles, **all off by default** — measured ≈ baseline on flash, on together in `mode: thorough`)
- **Confirmation-bias debiasing** (`accuracy.debias`): analysts judge the code on its own merits and ignore the PR title's framing. Accuracy-neutral on flash here, but real injection hardening.
- **Calibration anchors** (`accuracy.calibration`): per-agent flag/stay-silent examples in the cached prompt prefix to pin a cheap model's severity bar.
- **Diverse-lens ensemble** (`accuracy.lenses`): ensemble samples take different viewpoints (plain/checklist/adversarial) instead of only a temperature jitter.
- **Verdict-first chain-of-thought** (`accuracy.cot: brief`): optional short reasoning scan, findings emitted verdict-first.
- **Rubric meta-judge verifier:** the verifier now argues each finding's rejection first and keeps it only if the visible code survives — single pass, no bias-amplifying debate.

### Measured
- 3-run A/B on `deepseek-v4-flash` over the 37-fixture benchmark: levers-off baseline **101/111 (91%)**; debias+calibration **98/111 (88%)**; every lever arm within run-to-run noise of baseline. No measurable accuracy gain → levers ship **off by default** (honest-numbers rule; D29).

### Changed
- Eval benchmark expanded 17 → **37 fixtures across 7 languages**, weighted to misleading-title variants that measure debiasing, plus new bug classes (SSRF, insecure deserialization, `eval`, open redirect, ReDoS, secret logging, weak crypto, TLS-disabled) and clean false-positive controls.
- `evals/run.py`: per-lever env knobs, fail-fast timeouts, and a durable per-run results log.

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
