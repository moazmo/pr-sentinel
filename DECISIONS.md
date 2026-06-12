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

---

# V2 decisions (accuracy-per-dollar)

V2's thesis: review accuracy is a *systems* problem, not a model-size problem. LLM review errors are variance, mislocalization, and hallucination — so the architecture attacks each with a system-level countermeasure, on cheap models, with published numbers. pr-agent (the established OSS competitor) is single-LLM-call-per-tool by design and structurally can't follow.

## D13 — Line-numbered diff decoration

Analysts receive hunks with absolute new-file line numbers on every reviewable line (`diffmap.render_numbered`) and are told to cite the numbers shown, not infer them. Localization stops being a guess; it's the prerequisite for evidence anchoring (D14) and inline comments (D17). Deterministic, no LLM. Rejected: leaving raw `@@` headers (model miscounts on multi-hunk files).

## D14 — Evidence anchoring (the hallucination killer)

Every finding must quote the offending line in an `evidence` field. A pure-function pass (`verification.anchor_findings`) checks that quote against the diff's line map: match near the claimed range → keep and snap the range to the real location; match elsewhere in the file → keep and move it there; no match anywhere → drop. Hallucinated findings become *structurally unpostable*, not merely discouraged by prompt. This widens nothing on the security side — it only ever removes findings. Nobody else in the category does this.

## D15 — Self-consistency ensemble

Each analyst reviews each chunk K times (default 3) at a higher temperature, and `merge.vote_findings` majority-votes: a finding kept if seen in ≥`min_support` samples, OR if it's high/critical (those go to anchoring + the verifier rather than dying on a vote). This converts run-to-run variance — the dominant cheap-model failure mode, measured in v1 — into signal, and simultaneously crushes false positives (a hallucination must now *repeat* AND carry verifiable evidence). DeepSeek's cached-input pricing makes K samples cost ~1.3×, not K×, because the shared system+PR-map prefix is cache-priced after the first sample. Rejected: a single sample (v1 behavior — the source of the variance).

## D16 — Verifier adjudication pass

A dedicated node between merge and reviewer (`agents.run_verifier`) makes one batched call that confirms/rejects/downgrades each surviving finding against the numbered diff, then the reviewer writes prose only over adjudicated findings. Distinct role from the reviewer: reviewer = dedupe/prioritize/communicate; verifier = fact-check against code. Fail-open: any verifier failure passes findings through unadjudicated (a missing adjudication beats a missing review). Default on; one flag off.

## D17 — Inline review comments

Findings anchored to a verified added line (D13/D14) post as a GitHub Review with per-line comments (`github_client.create_inline_review`); the rest stay in the sticky summary, which keeps the verdict, an inline index, the footer, and disclosures. Inline failure falls back to putting everything in the summary (fail-open). Re-checked against the deterministic added-line set at publish time so a reviewer-mangled line number can't anchor a comment to the wrong code. Rejected for v1 (scope); the single most-visible UX gap vs every competitor.

## D18 — Two-tier model routing + structured output + native Anthropic

`provider.analyst_model` / `review_model` let the cheap model do the bulk analyst sampling while a (still cheap) model verifies/reviews — both default to `model`. The provider sends `response_format: json_object` and remembers a 400 to fall back bare (so DeepSeek/OpenAI get guaranteed JSON, Ollama still works). One pooled httpx client per provider (no per-call TLS handshake). A native `AnthropicProvider` (Messages API, ~70 LOC, no SDK) fulfils the oldest roadmap promise behind the same Protocol. Cached-token counts surface in the cost footer.

## D19 — Comment commands (`@pr-sentinel review|ask|describe`)

The action also handles `issue_comment` events. `review` re-runs the pipeline; `ask <q>` answers a question about the diff in one call; `describe` writes a summary into the PR body between markers. Hard gate: the commenter's `author_association` must be OWNER/MEMBER/COLLABORATOR — a drive-by commenter must not be able to spend the repo owner's API budget (the economic-DoS rule, extended to the comment surface). Command text is never echoed into privileged context.

## D20 — Published model × strategy leaderboard

The eval harness (`evals/run.py`) runs configurable strategies (samples, verifier, models via env) over a 17-fixture, 5-language set with hard negatives, and prints a labelled, cost-annotated table. The headline artifact: the same $0.14/M model goes from a naive single pass to the ensemble+verifier system, measured, with false-positive rates published. Honest numbers whatever they say — fixtures are never tuned to pass.

---

# V2.1 decisions (adoption features — all $0, user brings the key)

These convert "an accurate reviewer" into "a reviewer teams adopt and keep". Each is opt-in or a backward-compatible default; nothing changes the security or fail-open invariants.

## D21 — One-click fix suggestions

Findings carry an optional `fix` field (literal replacement code, distinct from prose `suggestion`). When a finding anchors to a single added line and its fix is one line, `format_inline_body` renders a GitHub ```suggestion block the author applies in one click. Gated to single-line replacements because a suggestion replaces the anchored line — a multi-line fix could mangle the file; those fall back to a fenced prose block. The fix only ships when it survives evidence anchoring + the verifier, so a wrong one-click fix is unlikely. Competitors gate this behind paid tiers; it's free here because the user's key does the work.

## D22 — Check Run + merge gating

`gate.level` (default `off`) posts a GitHub Check Run whose conclusion *fails* when an unresolved finding meets the severity, with per-line annotations in the Files tab. Teams make it a required status check to block risky merges — turning an advisory comment into enforceable infrastructure. Off by default so PR Sentinel never surprises anyone by failing their PR; needs `checks: write` only when enabled. Fail-open like everything else.

## D23 — Incremental review

On a re-review, the reviewed head SHA is embedded in the sticky comment's hidden marker; the next run compares `last_sha...head` and skips files unchanged since, so settled code isn't re-flagged or re-billed. Default on (`review.incremental`). The biggest real-world complaint about AI reviewers — "it keeps re-flagging the same thing on every push" — closed deterministically. Fail-open: any compare failure reverts to a full review.

## D24 — Finding suppression

Two escape hatches for residual false positives (the retention metric): config globs (`review.suppress: ["legacy/**", "api/*.py:nit"]`) and inline `pr-sentinel: ignore[category]` markers in the diff. Both pure functions, applied after anchoring. Config suppression is read from the base branch (a hostile PR can't suppress its own findings). A reviewer you can't quiet gets uninstalled; this is what keeps it.

## D25 — Custom per-repo instructions + presets

`agents.guidance` / `agents.instructions` append maintainer guidance to analyst prompts (from the base branch — same anti-injection property as config). `mode: fast|balanced|thorough` is a one-liner preset over the accuracy knobs for low-friction adoption. The customization-parity feature competitors lean on, without the config sprawl.

## D26 — Adaptive sampling

`accuracy.adaptive` (default on) spends one sample per chunk and only draws the remaining samples on chunks that found something — clean code is the common case and doesn't need a vote. ~40% fewer calls at the same catch rate, deepening the cost moat. The vote semantics are unchanged on chunks that do re-sample.

## D27 — Opt-in cross-file pass

`accuracy.cross_file` adds one final pass (`agents.run_cross_file`) that sees all changed files and flags the cross-file issues per-file review structurally misses — a stale caller after a signature change, a renamed symbol still referenced elsewhere. Its findings go through the same anchoring + suppression as any other. Closes the one real weakness of the per-file strategy (D7), as an opt-in extra call rather than a default cost.

## D28 — Review event, risk labels, readiness score

`output.request_changes_at` submits the review as REQUEST_CHANGES at a chosen severity (a real review signal, not just a comment). `output.labels` applies risk labels (`security`/`needs-tests`/…) for triage (needs `issues: write`). A deterministic merge-readiness (0–100) and review-effort (1–5) line in the summary — computed from finding counts/severities, no extra LLM call. The ensemble's `support` count surfaces as a per-finding confidence signal nobody else shows.
