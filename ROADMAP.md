# Roadmap

Scoped deliberately. v1 shipped a five-agent reviewer; **v2 shipped the accuracy-per-dollar core**; **v2.1 shipped the adoption features** (auto-fix suggestions, merge gating, incremental review, suppression, custom instructions, presets, risk labels, adaptive sampling, cross-file pass, confidence display). Everything below is deferred **on purpose**.

## Shipped (no longer roadmap)

- Inline review comments; native Anthropic provider; ensemble + evidence anchoring + verifier adjudication
- `@pr-sentinel review | ask | describe` commands; published model × strategy eval leaderboard
- One-click fix suggestions; Check Run + optional merge gating; incremental review; finding suppression
- Custom per-repo agent instructions; mode presets; risk labels; review-event (REQUEST_CHANGES)
- Adaptive sampling; opt-in cross-file pass; merge-readiness/effort score; confidence display
- **v2.5 research levers** — confirmation-bias debiasing, per-agent calibration, diverse-lens ensemble, verdict-first CoT, rubric meta-judge verifier; benchmark expanded to 37 fixtures / 7 languages; all behind config toggles, defaults set from the measured A/B (DECISIONS D29–D34).

## Next — structural levers (post-v2.5; prompt-level gains are at the flash ceiling)

Driven by `docs/NEXT_RESEARCH_2026-06-13.md`. The v2.5 A/B proved more prompting doesn't move cheap-model accuracy; these do.

- **Reasoning controls (built, measuring).** `accuracy.analyst_thinking` + `reasoning_effort` — DeepSeek V4 thinking is a parameter (default on). Measuring thinking off (~10× cheaper output), two-tier thinking, and effort levels; set defaults from data (D36).
- **SAST grounding — measured, parked (D39).** `sast.enabled` runs Semgrep through the verifier's triage. Measured 2026-06-17 (`evals/sast_probe.py`): **precision-safe** (the verifier killed 3/3 of Semgrep's clean-fixture FPs) but **0 net recall** — 0/32 on the logic-bug real-PR set, and the analysts already catch the 12 seeded security bugs. So it stays opt-in/off and the **SAST image variant is NOT shipped** — no measured win justifies the heavy dependency. Revisit only on a concrete security-heavy-repo user need (the deterministic-guarantee case) or a benchmark where the ensemble misses textbook bugs Semgrep catches.
- **Agentic cross-file context — measured twice, declined (D38 + D40).** A naive tool-loop (D38, 1/10) AND a proper RepoAudit-style loop with def-only fetches + `reasoning_effort=high` (D40, 3/10) both LOST to diff-only (5/10) on `deepseek-v4-flash` — attention dilution, intrinsic to giving the cheap model tools. The recall unlock is a **stronger-model technique** → folded into the premium distilled/RL-tuned model item below, not the cheap BYOK hot path.
- **Real-PR benchmark.** Inverted real bug-fix commits, scored on real bugs. `evals/realpr.py --precision` now adds a forward-fixed-version false-positive proxy → **precision + F1**, not recall-only (closes the gap both external review passes flagged). Next: promote it to a **release gate** — run on a fixed monthly snapshot, fail a release if recall regresses.
- **Context A/B on the live path** — measure `review.context_lines` 0/4/8 on a real PR (the static-fixture harness can't extend hunks). SWE-PRBench says more context can *hurt*; re-default to the measured winner (D34).
- **Benchmark to 60–100 fixtures** — extend the inverted-real-bug-fix and misleading-title sets; isolate per-lever arms (debias-only, calibration-only, lenses, cot) at `--runs 5` for tighter attribution.
- **Flagship head-to-head leaderboard row** (GPT-5 / Claude Opus single pass vs the flash ensemble) once a flagship key is wired — the comparison the architecture is built to win.
- **Deep-context mode** — optional `actions/checkout` to pull whole-file / dependency context beyond the ±N head-ref lines, **gated on the context A/B showing context helps**.
- **Per-language prompt packs** beyond `language_hint` and custom instructions.
- **Conversational follow-ups** — `@pr-sentinel explain <finding>` and threaded replies.

## Later

- **Multi git-provider support** — GitLab, Bitbucket, Azure DevOps.
- **Image signing / SLSA provenance** for the release pipeline (base image is already digest-pinned; Dependabot watches it).
- **Fork-PR review** via a documented maintainer-gated label workflow (never `pull_request_target`).
- **Feedback flywheel** — record which findings developers dismiss vs. commit, and re-rank noisy vs. high-fix-rate categories per repo (the BitsAI-CR / iCodeReviewer production pattern). Needs adoption data first.

## Much later (only after traction evidence)

- **GitHub App** — one-click install, no workflow file.
- **Hosted tier** — managed keys, team dashboard, org-wide policy. Engine stays open-source (open-core: paid is convenience, not capability).
- **Distilled / RL-tuned review model (premium capability lever).** The one path past the cheap-model *capability* ceiling — a small model RL-tuned with structured-reasoning distillation on review/vuln data (R2Vul-style, [arXiv 2504.04699]). Out of the $0 BYOK hot path by definition → a hosted-tier model, not the OSS default.
- **Learning across PRs** — repo-level memory of past reviews.
