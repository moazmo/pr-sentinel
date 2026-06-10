# Design Decisions

Every significant architecture choice, with the options weighed and why the chosen side won. Optimization criteria applied throughout, in order: (1) lowest cost to the end user, (2) maximum adoption/reachability, (3) implementation simplicity — but simplicity is sacrificed wherever it would hurt output quality.

## D1 — Language/runtime: Python 3.12, Docker container action, prebuilt GHCR image

Python keeps the project in the LangGraph ecosystem it's built around. The usual argument for TypeScript actions is cold-start speed — eliminated here by publishing a **prebuilt image** to GHCR on each release (pulled in seconds) instead of building the Dockerfile at PR time (60–120s). Docker isolation also means our dependency tree never pollutes the user's runner. Rejected: composite action with `setup-python` (slow, pollutes the runner), TypeScript (off-stack, loses LangGraph).

## D2 — Diff acquisition: paginated `List pull request files` endpoint, never the whole-diff endpoint

The whole-diff media type hard-fails with HTTP 406 past 3,000 lines / 300 files — exactly the PRs where review matters. The files endpoint paginates with no cliff, and per-file `patch` objects feed the per-file strategy (D7) directly. `patch` is absent for binaries and oversized per-file diffs — both surfaced as "skipped, disclosed". Bonus: no `actions/checkout` needed at all, which removes a line from the user's install.

## D3 — Orchestration: clean fan-out/fan-in LangGraph graph, no loops

`ingest → [architect | security | performance | test] → merge_findings → reviewer → publish`. The deterministic merge is its **own node** (not buried in the reviewer) so it's independently testable. No reviewer→analyst feedback loops in v1: at diff scale a loop adds latency and token spend (the user's money) for marginal catch-rate gain. The graph shape accommodates one later if it ever earns its way in. Per-agent failure rule: one analyst failing is recorded and disclosed; the other three still report.

## D4 — Parallel analysts with a concurrency cap and retries

Parallel ≈ the latency of one agent; sequential = the sum of four. Token cost is identical either way, so speed wins. Guardrails: a semaphore caps simultaneous LLM requests (default 8 across all in-flight calls), exponential backoff on 429/5xx (3 attempts), and a per-agent timeout (default 120s) after which that agent is marked failed.

## D5 — Dedup: hybrid — deterministic pre-pass, then LLM semantic merge

Stage 1 (pure functions): collapse exact duplicates (same file + overlapping lines + same category → keep highest severity, credit all agents), filter by severity threshold **before** the reviewer (filtered findings never cost reviewer tokens), severity-order with a hard cap of 40, cluster by 5-line proximity. Stage 2 (Reviewer LLM): resolve semantic duplicates ("unparameterized query" vs "SQL injection risk"), cut noise, write the prose. Pure-LLM aggregation makes noise control depend on prompt luck and bills the user for merging code could do; pure-deterministic misses semantic duplicates, the common case across four lenses. Hybrid is the most code — and the place quality demands it, because this stage IS the product's perceived quality.

## D6 — Provider: the OpenAI-compatible protocol IS the v1 provider

One thin self-written async httpx client (~130 lines) speaking the chat-completions schema, with three knobs: `base_url`, `model`, key env var. One integration reaches OpenAI, OpenRouter (incl. free models), Groq, DeepSeek, Mistral, and local Ollama — including running at literally $0. Rejected: LangChain model wrappers / LiteLLM (heavy dependency, opaque internals, fatter Docker image) — LangGraph is used for what it's good at (orchestration), not as a model-wrapper layer. Native Anthropic SDK is the first roadmap provider, a clean drop-in behind the same `LLMProvider` protocol.

## D7 — Large diffs: per-file review + shared PR map, disclosed truncation as backstop

Every analyst call includes a compact PR map (title + full changed-file list with +/- counts, ~200–500 tokens) restoring most cross-file awareness. Small files batch into one call up to a ~12k-token budget; large files go alone; a single over-budget file is truncated hunk-by-hunk (earliest kept) **with disclosure** in the comment. Global caps bound the whole run. Rejected: hierarchical summarize-then-reason (more calls = more user cost; summaries blur line-level findings), silent truncation (trust-destroying).

## D8 — Output: one sticky, upserted comment

Verdict header → severity-grouped findings with agent attribution → collapsed detail (`<details>`) → skipped-files disclosure → usage/cost footer → hidden HTML marker. On `synchronize`, the existing comment is found via the marker and **edited**, not re-posted — comment-stacking is the second-fastest way to get uninstalled after false positives. The 65,536-char GitHub limit is enforced by collapsing sections first, then truncating with a pointer to logs. Zero findings posts a short "looks clean" so users see it ran; zero findings **with agent failures** says so honestly instead.

## D9 — Config: `.pr-sentinel.yml`, YAML, Pydantic-validated, zero-config-first

The defaults are the product for 90% of users. Malformed config → defaults + a note in the comment (never a crash). Unknown keys → warn and ignore (forward-compatible). Read from the **base branch**, never the PR head — otherwise a hostile PR edits the config that reviews it.

## D10 — Cost/rate controls: built-in skip list + hard caps + visible cost + dry-run

Lockfiles/vendored/generated/minified files are always skipped (no config needed). Caps: 35 files, 120k input tokens/run, 2k output tokens/agent — hitting a cap skips + discloses, never errors. Token usage (from provider responses) is summed per agent and printed in the comment footer with an estimated dollar cost. `dry_run: true` runs the whole pipeline minus the LLM calls and posts the estimate — a try-before-you-spend install experience. These aren't UX polish; they're the guarantee that installing PR Sentinel costs at most ~N cents per PR no matter what arrives (economic-DoS protection).

## D11 — Evals: seeded-bug fixtures with a published results table

`evals/fixtures/`: one planted bug per analyst (SQL injection, N+1, leaky abstraction, untested branch), a prompt-injection fixture, and two **clean** fixtures weighted as the headline metric — false positives are the uninstall-risk number. Each fixture pairs a realistic diff with machine-checked expectations (agent, category, file, severity floor). `evals/run.py` executes the real pipeline with a real key and prints the README table. Honest numbers, dated, with the model named.

## D12 — Distribution: Marketplace + prebuilt GHCR image + semver with a moving major tag

Tag `v1.0.0` → release workflow builds + pushes the image (the only path that builds release images, keeping tag→source auditable), rewrites `action.yml` on the tag to the prebuilt image, and moves the `v1` major tag. Users pin `@v1` for patches or `@v1.0.0` to freeze. The README install block is the hardened workflow verbatim, because nobody reads past the copy-paste.

## Security posture (decided up front, not retrofitted)

`pull_request` trigger only; minimal permissions; all PR-derived text treated as untrusted data inside delimited blocks; structured output as an injection boundary; secrets confined to the HTTP client layer with output scrubbing as defense-in-depth; config from the base branch; fork PRs skipped gracefully. Decided before the first line of code because the insecure path is the easy path and must never be the development default. Details and the 2026 incident receipts: [SECURITY.md](SECURITY.md).
