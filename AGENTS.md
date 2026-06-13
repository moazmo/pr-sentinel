# AGENTS.md — guide for AI coding agents working on PR Sentinel

PR Sentinel is a multi-agent code-review GitHub Action: five LLM agents (Architect, Security, Performance, Test, Reviewer) review a PR diff through a LangGraph fan-out/fan-in pipeline and post one prioritized, agent-attributed comment. Python 3.12, Docker action, OpenAI-compatible BYOK provider.

## Commands

```bash
pip install -e ".[dev]"        # setup (use a venv)
pytest                         # 215 tests — LLM and GitHub API fully mocked, no network, no key
ruff check src tests evals     # lint (line length 100)
python evals/run.py --runs 3 --label flash-v2   # evals — REAL LLM; needs PR_SENTINEL_API_KEY
                               # env knobs: PR_SENTINEL_{BASE_URL,MODEL,SAMPLES,VERIFIER,
                               # ANALYST_MODEL,REVIEW_MODEL}; never run in CI
docker build -t pr-sentinel:dev .
```

On Windows, set `PYTHONUTF8=1` before running evals (emoji output).

## Architecture map

| File | Owns |
|---|---|
| `src/pr_sentinel/models.py` | **Finding schema — the single source of truth** (incl. `evidence`, `support`) — plus ReviewState |
| `src/pr_sentinel/graph.py` | Pipeline: ingest → 4 parallel analysts (×3 adaptive samples) → merge (vote+anchor+suppress) → verifier → cross_file → reviewer → publish (inline+suggestions, check-run, labels, sticky+incremental SHA) |
| `src/pr_sentinel/agents.py` | Analyst/reviewer/verifier/describe/ask runtime, prompt assembly, balanced-bracket JSON extraction |
| `src/pr_sentinel/diffmap.py` | **V2:** parse patches → numbered hunks, line maps, added-line sets, context extension (pure) |
| `src/pr_sentinel/verification.py` | **V2:** evidence anchoring — drop findings whose quote isn't in the diff (pure) |
| `src/pr_sentinel/suppression.py` | **V2.1:** drop findings by config glob or inline `pr-sentinel: ignore` marker (pure) |
| `src/pr_sentinel/prompts/*.md` | Agent system prompts — **product surface**, readable markdown |
| `src/pr_sentinel/merge.py` | Deterministic dedup/clustering + self-consistency `vote_findings` — pure, most-tested code |
| `src/pr_sentinel/provider.py` | Thin OpenAI-compat + native Anthropic clients; the ONLY place secrets live; json-mode, pooled client |
| `src/pr_sentinel/github_client.py` | Paginated files API, sticky upsert, inline review API, PR-body describe, base-branch config |
| `src/pr_sentinel/chunking.py` | PR map, numbered file blocks, token budgets, disclosed truncation, file-priority ranking |
| `src/pr_sentinel/formatter.py` | Comment markdown, inline-comment bodies, describe block, 65,536-char cap |
| `src/pr_sentinel/security.py` | Prompt sanitizer + output secret scrubbing |
| `src/pr_sentinel/config.py` | `.pr-sentinel.yml` (Pydantic, defaults-first, parsed from the BASE branch); accuracy/output blocks |
| `src/pr_sentinel/main.py` | Action entrypoint — every path exits 0; `@pr-sentinel` command dispatch (author-association gated) |
| `tests/` | 215 tests; `conftest.py` has MockProvider / SequenceProvider / FailingProvider / single_sample_config; `test_research_levers.py` pins the v2.5 lever wiring. Close any pooled httpx client you set in a test (`await x.aclose()`) — leaks surface as teardown OSError on Python 3.14 |
| `evals/` | 37 fixtures (7 languages, hard negatives, 2 injection vectors, misleading-title `mt_*` debias probes); `run.py` env-driven leaderboard runner (per-lever knobs, fail-fast timeouts, durable `_matrix.log`) |

## Invariants — never break these

1. **Never break the user's CI.** Every failure path degrades to a comment (or log line) and exit 0. No exception may escape `main.run()`.
2. **Secrets live only in the HTTP client layer.** No prompt template, state field, formatter, or log may reach the API key or GitHub token. The final comment is secret-scrubbed before posting; rendered prompts have a regression test.
3. **All PR-derived text is untrusted input.** It enters prompts only inside delimited data blocks, sanitized by `sanitize_for_prompt`. Never feed PR title/body/diff anywhere else.
4. **Structured output is a security boundary.** Anything an LLM returns that doesn't validate against `Finding` is dropped. Widen extraction if needed; never widen acceptance.
5. **Config comes from the BASE branch**, never the PR head.
6. **`pull_request` trigger only.** Never document or accept `pull_request_target`.
7. **Noise is the product's death.** False positives are why AI reviewers get uninstalled. Prompt changes must not regress the clean fixtures in `evals/` — run evals before and after touching any prompt.
8. **Cost caps are security guarantees**, not UX. Hitting a cap = skip + disclose, never error, never overrun.
9. **Tests are part of done.** New logic ships with mocked tests in the same change. Honest counts — never inflate.
10. **CI never calls a live LLM.** Evals are manual/dispatch only.
11. **Evidence anchoring only ever removes findings.** `verification.py` widens *extraction* of model output, never *acceptance* — a finding still must validate against `Finding` and quote a real diff line. Don't loosen the anchor check to make a fixture pass.
12. **The verifier and context extension fail open.** A verifier error passes findings through unadjudicated; a context-fetch error keeps the raw hunk. Never let either abort the review.
13. **Comment commands are author-association gated.** `@pr-sentinel` commands run only for OWNER/MEMBER/COLLABORATOR. Never widen this — it's the economic-DoS guard on the comment surface.
14. **Suppression and custom instructions come from the base branch.** `review.suppress`, `agents.guidance`, `agents.instructions` are config — a hostile PR must never be able to suppress its own findings or inject prompt instructions. Inline `pr-sentinel: ignore` markers are the one exception (they live in the diff, on the line they silence, and only silence that spot).
15. **One-click fixes only for single-line replacements.** A suggestion block replaces the anchored line; never emit one for a multi-line `fix`. The `fix` must survive anchoring + verifier before it's offered.
16. **Accuracy levers are config toggles, and the default is the measured winner.** `accuracy.debias|calibration|lenses|cot` (v2.5) each map to an env knob in `evals/run.py` so the A/B is pure config. Never flip a default that changes review behavior without an eval that justifies it — same honest-numbers rule as fixtures. Run the matrix, ship what wins.
17. **Preserve the cached prompt prefix.** Stable blocks (per-agent role, shared rules, calibration, debias, CoT instruction) are front-loaded in `analyst_system_prompt` and must stay byte-identical across calls; per-repo (language hint, guidance) and per-sample (ensemble lens) text goes *after* / into the user message. Reordering stable text behind variable text silently kills DeepSeek's prefix cache (~50× cost on those tokens).

## Conventions

- Pure functions for anything decision-like (merge, ranking, caps) — testability first.
- Comments explain *why*, not *what*.
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`).
- Dedup/merge logic lives in `merge.py` as pure functions; LLM-facing behavior changes belong in prompts, not parsing hacks.
- Keep the provider client thin; no LangChain model wrappers / LiteLLM.

## Documentation sync rule (mandatory)

**Every session that changes project state must leave ALL Markdown documentation consistent with the code in that same session.** That includes:

- Public: `README.md` (incl. eval numbers + test counts), `DECISIONS.md`, `ROADMAP.md`, `SECURITY.md`, `CONTRIBUTING.md`, this file.
- Private working docs under `docs/` (gitignored — **never commit them**), including `docs/INTERVIEW_DEFENSE.md` and the dated state-review documents: update them with new features, numbers, decisions, and stories as they happen.

Stale documentation is treated as a bug. If you ship it, you document it — public and private — before the session ends.

## Pitfalls (learned the hard way)

- GitHub evaluates `${{ }}` expressions **everywhere** in `action.yml`, including description strings — the `secrets` context there breaks action loading at runtime, and CI won't catch it (only a live PR run does).
- The whole-PR diff endpoint 406s past 3,000 lines; only the paginated files endpoint is safe.
- Issue comments cap at 65,536 chars; the formatter enforces it — keep it that way.
- Greedy JSON regexes break on brackets in model prose; use the balanced scanner in `agents.py`.
- Cheap models (deepseek-v4-flash) are measurably less consistent run-to-run than pro-tier; eval with `--runs 5`, never trust a single run.
