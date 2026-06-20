# Changelog

All notable changes to PR Sentinel. Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions are git tags.

## [Unreleased]

### Added
- **Repository-context prefetch** (`accuracy.repo_context`, default off): deterministically fetches definitions of the cross-file symbols a diff references (same-file siblings + imported modules) and hands analysts a bounded `<repo_context>` block — the thinking-compatible alternative to an agentic tool-loop (DeepSeek thinking mode can't function-call). Python-first, live-path, fail-open. Measured over 3 runs on a 32-PR set: **24% → 27%** real-PR recall; per-PR it reliably adds 1 Python context-dependent catch (rest is non-Python run-to-run noise). A small, real, Python-only gain that costs extra fetches → ships off, recommended opt-in for Python repos (D37).
- **Multi-language repo-context:** `repo_context` now resolves **JS/TS** (same-file siblings + relative `./`/`../` imports) and **Go** (same-file/same-package siblings), not just Python — dependency-free regex extraction. Default still off, pending a measured multi-language win on the real-PR benchmark (Go cross-package needs a directory listing the fetch path doesn't provide → deferred to the agentic upgrade).
- `evals/realpr.py --repo-context` to A/B the lever on real PRs.
- `evals/realpr.py --precision`: a forward-fixed-version false-positive proxy → reports **precision + F1**, not recall-only (the F1 axis two external review passes flagged as missing).
- `evals/sast_probe.py`: measures the SAST lever (Semgrep via the official Docker image) on both the real-PR set and the seeded fixtures — Phase 1 ($0 raw recall + FP), Phase 2 (preset hits → anchor → verifier).
- `evals/agentic_probe2.py`: the *proper* agentic loop (def-only `fetch_definition`, RepoAudit hypothesize→confirm→validate, `reasoning_effort=high`) with a directly-comparable diff-only control.

### Added
- **MAV verifier** (`accuracy.verifier_aspects`, default 1): optional multi-angle verification (grounding/skeptic/impact rubrics, any-reject combine) — a precision-recall dial. Measured a slider, not a frontier-push (D45), so off by default.
- **`mode: thorough` is now max-recall:** sets `min_support=1` (keep every sample's findings, let the verifier filter) — measured **+~17pp recall / +12pp F1** on real PRs vs the voted default, at a higher false-positive rate (D45). The FP-averse default (`min_support=2`) is unchanged.

### Measured / decided
- **`min_support=1` is the biggest measured recall lever (D45):** same-subset (30 real PRs) the vote-then-verify default = 20% recall / F1 31%; min_support=1 + single verifier = **37% recall / F1 43%** (clean-pass 90%→67%). MAV (multi-aspect) is a precision-recall slider, doesn't dominate. First shipped win from the deep-research program; ships in `mode: thorough`, not as the default (precision/FP cost). Corrects D44's subset-error conclusion.
- **Aggregation diagnostic (D44):** a 9-agent deep-research synthesis reframed the ceiling as *diff-only-informational* (~27–32% even for frontier models). Measured: the ensemble vote discards 8 real catches (union-of-3 recall 25/60 vs voted 17/60) — recall is recoverable — but the crude fix (min_support=1) regresses precision (68%→52%, clean-pass 87%→67%, F1 flat) → not shipped; the recoverable catches need a stronger MAV verifier (next build). Added `realpr.py` sampling-env knobs, a `--judge` semantic scorer, `--limit`, and a keep-awake for long runs.
- **Stronger same-family model measured (D43):** `deepseek-v4-pro` recall **11/60** vs flash **17/60** on the 60-PR benchmark — **worse, not better** (more conservative + flakier JSON adherence). A bigger same-family model is not the capability lever; a true flagship (GPT-5/Opus) test is gated on a key. Added a Windows keep-awake for long evals (modern-standby killed three runs).
- **Real-PR benchmark sharpened (D41):** `realpr.py` repo set expanded 9 → 20 repos (Py/JS/TS/Go) → **60 PRs**. Baseline: recall **17/60 (28%)**, precision **68%**, **F1 40%**, clean-pass 52/60 — confirms the ~24–27% real-PR story on 2× the data, now with an F1 axis. The ruler for every lever + the reward signal for a future tuned model.
- **`reasoning_effort=high` measured (D42):** analyst effort=high scored **15/60** vs the default **17/60** — no gain, slightly worse, more cost. Default `accuracy.reasoning_effort` stays `""`. `realpr.py` now honors `PR_SENTINEL_REASONING_EFFORT`/`PR_SENTINEL_ANALYST_THINKING` (pure-env A/B, like `run.py`).
- **Premium tuned model (Lever 5) + feedback flywheel (Lever 6) specced, gated (D-roadmap):** the only levers left that can move recall (capability + data); both gated on launch/adoption. See ROADMAP.
- **Proper agentic loop measured (D40):** the best-shot redesign (def-only fetches + RepoAudit structure + effort=high) scored **3/10** vs diff-only **5/10** on the same 10 hard real PRs — recovered from D38's naive 1/10 but still **lost to diff-only**. Closes D38's "naive impl" caveat: agentic context hurts cheap-model recall regardless of loop quality → **not integrated**; the recall unlock is a stronger-model technique.
- **SAST grounding measured (D39):** raw Semgrep = 0/32 on the real-PR set (logic bugs, wrong instrument), 12 catches / 3 clean-FPs on the seeded security fixtures; through the pipeline the verifier **killed 3/3 of the FPs (0 leaked)** — the design is **precision-safe** — but added **0 net recall** (the analysts already catch those). → `sast.enabled` stays **opt-in/off** and **no SAST image variant is shipped** (no measured win to justify the dependency). Same disciplined outcome as repo_context (D37) and the agentic loop (D38).

### Changed
- `sast.rules` default `"auto"` → `"p/default"`: `--config auto` refuses to run when Semgrep telemetry is disabled and pings semgrep.dev to select rules — wrong for a privacy-first tool (D39).

### Security
- **Injection hardening for repository-context prefetch:** prefetched definitions (`<repo_context>`, fetched from the PR head ref and therefore PR-controlled) are now run through `sanitize_for_prompt`, and `repo_context` is added to the delimiter scrubber — a hostile imported module can no longer break out of the data block (closes a gap in the opt-in `accuracy.repo_context` path).

## [2.6.0] — 2026-06-13 — structural levers (reasoning controls, SAST grounding, real-PR benchmark)

After v2.5 proved prompt-level levers are at the cheap-model ceiling, this release adds the *structural* levers the research pointed to. All behavior-preserving (new knobs default off / provider-default).

### Added
- **Reasoning controls** (`accuracy.analyst_thinking`, `accuracy.reasoning_effort`): DeepSeek V4 thinking is a request parameter (default on). Tri-state `analyst_thinking` is endpoint-safe (the `thinking` field is sent only when explicitly set, so non-DeepSeek endpoints are unaffected). Measured finding: **thinking is essential** — disabling it on analysts drops ~91% → ~61%, so it stays on (D36).
- **SAST grounding** (`sast.enabled`, default off): runs Semgrep over the changed files and feeds its hits into the verifier's triage — the documented 2025-26 precision lever. Opt-in, fail-open, live-path (needs Semgrep in the runner) (D35).
- **Real-PR benchmark** (`evals/realpr.py`): discovers real merged bug-fix PRs from the GitHub API, reverses them to reintroduce the bug, and scores recall on real bugs — the honest metric the seeded fixtures can't give. First measured result: **7/32 (21%)** recall on real reintroduced bugs (vs 91% on seeded fixtures) — the context-dependent gap the agentic-context roadmap targets.

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
