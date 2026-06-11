# AGENTS.md — guide for AI coding agents working on PR Sentinel

PR Sentinel is a multi-agent code-review GitHub Action: five LLM agents (Architect, Security, Performance, Test, Reviewer) review a PR diff through a LangGraph fan-out/fan-in pipeline and post one prioritized, agent-attributed comment. Python 3.12, Docker action, OpenAI-compatible BYOK provider.

## Commands

```bash
pip install -e ".[dev]"        # setup (use a venv)
pytest                         # full suite — LLM and GitHub API fully mocked, no network, no key
ruff check src tests evals     # lint (line length 100)
python evals/run.py --runs 5   # evals — hit a REAL LLM; needs PR_SENTINEL_API_KEY
                               # (+ PR_SENTINEL_BASE_URL, PR_SENTINEL_MODEL); never run in CI
docker build -t pr-sentinel:dev .
```

On Windows, set `PYTHONUTF8=1` before running evals (emoji output).

## Architecture map

| File | Owns |
|---|---|
| `src/pr_sentinel/models.py` | **Finding schema — the single source of truth** — plus ReviewState (LangGraph state) |
| `src/pr_sentinel/graph.py` | The pipeline: ingest → 4 parallel analysts → merge_findings → reviewer → publish |
| `src/pr_sentinel/agents.py` | Analyst/reviewer runtime, prompt assembly, JSON extraction (balanced-bracket scanner) |
| `src/pr_sentinel/prompts/*.md` | Agent system prompts — **product surface**, readable markdown |
| `src/pr_sentinel/merge.py` | Deterministic dedup/clustering — pure functions, most heavily tested code |
| `src/pr_sentinel/provider.py` | Thin OpenAI-compatible client; the ONLY place secrets live |
| `src/pr_sentinel/github_client.py` | Paginated files API, sticky comment upsert, base-branch config fetch |
| `src/pr_sentinel/chunking.py` | PR map, per-file chunks, token budgets, disclosed truncation |
| `src/pr_sentinel/formatter.py` | Comment markdown, 65,536-char cap, severity grouping |
| `src/pr_sentinel/security.py` | Prompt sanitizer + output secret scrubbing |
| `src/pr_sentinel/config.py` | `.pr-sentinel.yml` (Pydantic, defaults-first, parsed from the BASE branch) |
| `src/pr_sentinel/main.py` | Action entrypoint — every path exits 0 |
| `tests/` | 102 tests; `conftest.py` has MockProvider / SequenceProvider / FailingProvider |
| `evals/` | Seeded-bug + clean + injection fixtures; `run.py` aggregate runner |

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
