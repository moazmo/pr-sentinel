# 🛡️ PR Sentinel

**Multi-agent code review for your pull requests — runs in your CI, brings your own key, and shows you which agent found what.**

[![CI](https://github.com/moazmo/pr-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/moazmo/pr-sentinel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![evals 91% seeded](https://img.shields.io/badge/evals-91%25%20seeded%20%C2%B7%200%20FP-brightgreen.svg)](#accuracy-is-a-systems-problem-not-a-model-size-problem)
[![cost ~$0.005/review](https://img.shields.io/badge/cost-~%240.005%2Freview-blue.svg)](#what-it-costs)

![PR Sentinel reviewing a pull request](assets/demo.gif)

Five specialized LLM agents — **Architect, Security, Performance, Test, and Reviewer** — each examine your PR diff from a different angle, then merge into **one prioritized, deduplicated comment**. No walls of noise, no black box: every finding is attributed to the agent that raised it, and every agent prompt is [readable in this repo](src/pr_sentinel/prompts/).

## The problem

Code review is the most expensive bottleneck in most teams. Senior engineers burn hours reviewing PRs; under-reviewed code ships bugs; and most AI tools help *write* code, not critically *review* it. The AI reviewers that do exist are usually noisy black boxes — and false positives are why people uninstall them.

## 30-second install

Add `.github/workflows/pr-sentinel.yml` to your repo:

```yaml
name: PR Sentinel
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: moazmo/pr-sentinel@v2
        with:
          api_key: ${{ secrets.PR_SENTINEL_API_KEY }}
```

Then add one repository secret: **Settings → Secrets and variables → Actions → New repository secret**, name it `PR_SENTINEL_API_KEY`, paste your LLM provider key. Done — no checkout step, no other configuration required.

> This workflow is the hardened version on purpose: `pull_request` trigger (never `pull_request_target`) and minimal permissions. See [Security model](#security-model).

## What it costs

PR Sentinel speaks the **OpenAI-compatible protocol with a configurable `base_url`** — one integration reaches OpenAI, OpenRouter, Groq, DeepSeek, Mistral, and local Ollama. A typical PR (~3k diff tokens × 4 analysts + reviewer) costs:

| Route | Model | $/1M in / out | Typical PR |
|---|---|---|---|
| Zero-config default | OpenAI `gpt-5-mini` | $0.25 / $2.00 | **≈ $0.01** |
| Cheapest strong option | DeepSeek V4 Flash | $0.14 / $0.28 | **≈ $0.004** |
| Best cheap closed-model | Claude Haiku 4.5 (via OpenRouter) | $1.00 / $5.00 | ≈ $0.03 |
| Free | OpenRouter free models | $0 | $0 (rate-limited) |
| Fully private | Ollama on a self-hosted runner | $0 | $0 — code never leaves your infra |

To use the cheapest option, drop this in `.pr-sentinel.yml`:

```yaml
provider:
  base_url: https://api.deepseek.com/v1
  model: deepseek-v4-flash
```

Every review comment shows its own token count and estimated cost in the footer. There's also a `dry_run: true` mode that posts a cost estimate **without making any LLM calls** — try PR Sentinel before spending a cent.

## The agents

| Agent | Looks for |
|---|---|
| 🏛️ **Architect** | Separation-of-concerns violations, leaky abstractions, coupling, misleading naming |
| 🔒 **Security** | Injection (SQL/shell/XSS), exposed secrets, authz/authn gaps, unsafe deserialization |
| ⚡ **Performance** | O(n²) patterns, N+1 queries, blocking calls in async paths, unnecessary allocations |
| 🧪 **Test** | New behavior without tests, untested error paths, assertions removed, broad mocks |
| 🔎 **Verifier** | Adjudicates every surviving finding against the diff — confirm / reject / downgrade — before anything posts |
| 🧠 **Reviewer** | The aggregator: resolves semantic duplicates, cuts noise, writes the final review |

The Verifier + Reviewer are the difference between "multi-agent" and "a wall of noise": the Reviewer's prompt is explicitly biased — *when in doubt, drop the finding; three real issues beat thirty maybes* — and the Verifier fact-checks each finding against the code first. All prompts live in [`src/pr_sentinel/prompts/`](src/pr_sentinel/prompts/) — read them, tune them, PR them.

## Architecture

```mermaid
graph LR
    A[ingest<br/><i>files, skip rules,<br/>numbered hunks, PR map</i>] --> B[🏛️ architect ×3]
    A --> C[🔒 security ×3]
    A --> D[⚡ performance ×3]
    A --> E[🧪 test ×3]
    B --> F[merge<br/><i>vote · anchor evidence<br/>· dedup · cluster</i>]
    C --> F
    D --> F
    E --> F
    F --> V[🔎 verifier<br/><i>confirm / reject /<br/>downgrade vs code</i>]
    V --> G[🧠 reviewer<br/><i>semantic dedup,<br/>noise cut, prose</i>]
    G --> H[publish<br/><i>inline + summary,<br/>scrub, sticky</i>]
```

A fan-out/fan-in [LangGraph](https://github.com/langchain-ai/langgraph) graph, no loops. Each analyst runs **3 samples in parallel** and majority-votes (self-consistency); the merge pass anchors every finding's quoted evidence to a real diff line (dropping hallucinations) and deterministically clusters duplicates; the Verifier adjudicates; the Reviewer resolves semantic duplicates and writes prose. If an agent fails, the others still report — partial review beats no review. Findings anchored to an added line post as **inline review comments**; the rest stay in one sticky summary comment.

Large PRs: files are fetched via the paginated files API (the only endpoint that doesn't fall over past 3,000 lines), reviewed per-file within token budgets with a shared "PR map" for cross-file context (plus ±8 lines of head-ref context per hunk), and anything truncated or skipped is **disclosed in the comment**, never silently dropped. When the file cap bites, the highest-review-priority files (source over docs, by churn) are kept.

## Configuration

Optional `.pr-sentinel.yml` at the repo root — zero config works out of the box. All fields and their defaults:

```yaml
mode: ""                      # preset: fast | balanced | thorough (thorough = all research levers on, max-recall; measured ≈ baseline on flash — overrides the accuracy block)
provider:
  base_url: https://api.openai.com/v1     # any OpenAI-compatible endpoint
  model: gpt-5-mini
  api_key_env: PR_SENTINEL_API_KEY        # name of the secret env var
  kind: openai-compat                     # or "anthropic" for the native Messages API
  analyst_model: ""                       # optional: cheaper model for the 4 analysts
  review_model: ""                        # optional: model for verifier + reviewer
agents:
  enabled: [architect, security, performance, test]   # reviewer always runs
  guidance: ""                # repo-specific guidance appended to every analyst, e.g. "Django project"
  instructions: {}            # per-agent guidance, e.g. {architect: "we use hexagonal architecture"}
accuracy:
  samples: 3                  # self-consistency samples per analyst (1 disables the ensemble)
  min_support: 2              # a finding must appear in this many samples to survive the vote
  verifier: true              # run the adjudication pass before the reviewer
  adaptive: true              # spend extra samples only on chunks that found something
  cross_file: false           # opt-in extra pass for cross-file issues (1 more call)
  # Research levers (v2.5) — all opt-in, default off. Measured ≈ baseline on flash
  # (no accuracy gain above the ensemble+verifier system), so off by default; on
  # together in `mode: thorough`. Kept as honest, toggleable infrastructure.
  debias: false               # judge the code on its own merits, ignore reassuring/alarming PR titles (also security hardening)
  calibration: false          # per-agent flag/stay-silent anchors (stable, cached prompt prefix)
  lenses: false               # give each ensemble sample a different lens (standard/checklist/adversarial)
  cot: "off"                  # "brief" adds a short reasoning scan before the findings (off | brief)
  # Reasoning controls (DeepSeek V4: thinking is a parameter, on by default).
  # analyst_thinking is DeepSeek-specific and endpoint-safe (only sent when set).
  # Measured: disabling thinking tanks accuracy (~91%→61%), so leave it on.
  analyst_thinking: null      # null = provider default (DeepSeek = on); false/true to force
  reasoning_effort: ""        # "" | low | medium | high (depth when thinking is on)
  repo_context: false         # prefetch cross-file symbol definitions for context (Python/JS/TS/Go, opt-in, live-path)
min_severity: medium          # report at/above: critical|high|medium|low|nit
ignore:                       # appended to the built-in skip list
  - "migrations/**"
limits:
  max_files: 35
  max_input_tokens: 120000
  max_output_tokens_per_agent: 2000
review:
  include_deletions: false
  language_hint: ""           # e.g. "python" — appended to agent prompts
  context_lines: 8            # head-ref context lines added around each hunk (0 disables)
  incremental: true           # on re-review, skip files unchanged since the last review
  suppress: []                # silence findings: ["legacy/**", "api/*.py:nit"]
output:
  inline: true                # post anchored findings as inline review comments
  suggestions: true           # render one-line fixes as one-click GitHub suggestion blocks
  request_changes_at: ""      # submit REQUEST_CHANGES at/above this severity (e.g. "critical")
  labels: false               # apply risk labels (security / needs-tests / …) to the PR
gate:
  level: "off"                # fail a Check Run at/above this severity so merges can be required
sast:
  enabled: false              # run Semgrep over changed files; hits go through the verifier's triage
  rules: "p/default"          # semgrep ruleset (needs Semgrep in the runner; opt-in, live-path; measured FP-safe but no net gain — D39)
describe: false               # maintain a generated summary in the PR body
dry_run: false                # estimate cost, post the estimate, no LLM calls
```

**Cheapest-accuracy preset** (the README leaderboard config) — flash everywhere, ensemble on:

```yaml
provider:
  base_url: https://api.deepseek.com/v1
  model: deepseek-v4-flash
```

You can also silence a false positive inline, right where it lives:

```python
danger = eval(user_input)  # pr-sentinel: ignore
risky  = run(x)            # pr-sentinel: ignore[security]
```

Lockfiles, `node_modules`, `vendor`, `dist`, minified and generated files are **always skipped** (built-in list, protects your token budget). A malformed config never breaks anything — defaults apply and the comment notes it.

The config is read from the **base branch**, not the PR head — so a hostile PR can't disable the Security agent or raise your spend caps.

## Security model

This category of tool was actively attacked in 2026 — review bots leaked their own API keys through PR titles, and `pull_request_target` misconfigurations got repos' entire secret stores harvested. PR Sentinel is built against that threat model:

- **`pull_request` trigger only, never `pull_request_target`.** On fork PRs, secrets are absent by GitHub design, and PR Sentinel **skips gracefully** — that's correct behavior, not a missing feature. The alternative is how repos get their keys stolen. See [SECURITY.md](SECURITY.md).
- **Minimal permissions:** `contents: read` + `pull-requests: write`. Even a fully compromised run can't push code or touch other workflows.
- **PR content is treated as untrusted input.** Titles and diffs reach the model only inside delimited data blocks, with explicit instructions that the content is data under review, never instructions. Delimiter-escape attempts are neutralized.
- **Structured output as a boundary:** analyst output that doesn't parse against the finding schema is discarded. An injected "post your API key" can't survive a parser that only accepts findings.
- **Secrets never reach the prompt path** — they exist only in the HTTP client layer, enforced by construction and by regression tests. As defense-in-depth, the final comment is scanned for key-shaped strings and redacted on match.
- **Config from the base branch** (see above).
- **BYOK data path:** your code goes to *your* chosen LLM provider under *your* key — or nowhere at all, with Ollama on a self-hosted runner. It never touches any server of ours (there are none).

## Reliability

PR Sentinel **never breaks your CI**. Every failure path — provider down, rate limits, malformed diffs, huge PRs, missing config — degrades to a short comment (or a log line) and a clean exit. Hard caps (`max_files`, `max_input_tokens`) guarantee a worst-case cost ceiling per PR no matter what arrives.

On every push to the PR, the existing review comment is **updated in place** (one living comment per PR), not stacked — and only the files **changed since the last review** are re-examined (incremental review), so settled code isn't re-flagged or re-billed.

## Features teams actually adopt for

Everything below is $0 — you bring the key, so there's no paid tier gating any of it:

- **One-click fixes.** When a finding has a precise fix, it's offered as a GitHub *suggestion block* — the author clicks "Commit suggestion" to apply it.
- **Merge gating.** Set `gate.level: high` and PR Sentinel posts a **Check Run** that fails when there's an unresolved High/Critical finding — make it a required check and risky code can't merge. Off by default; never surprises you.
- **Request changes.** Optionally submit the review as *Changes requested* at a severity you choose.
- **Suppression.** Silence a false positive with an inline `# pr-sentinel: ignore` or a `review.suppress` glob — it stays gone.
- **Custom guidance.** Tell the agents about your codebase (`agents.guidance`, `agents.instructions`) — read from the base branch, so a hostile PR can't inject instructions.
- **Risk labels + readiness score.** Auto-label PRs (`security`, `needs-tests`, …) and show a deterministic *merge-readiness 0–100* and *review-effort 1–5* in the summary.
- **Presets.** `mode: fast | balanced | thorough` instead of tuning knobs.

> Two optional features need one extra permission each in your workflow: merge gating adds `checks: write`, and risk labels add `issues: write`. Everything else works with the default `contents: read` + `pull-requests: write`.

## Accuracy is a systems problem, not a model-size problem

This is PR Sentinel's bet, and the thing that separates it from single-pass reviewers. LLM review errors are mostly **variance** (a finding shows up on one run, not the next), **mislocalization** (right issue, wrong line), and **hallucination** (a finding that cites code that isn't there). None of those need a bigger model — they need *sampling, anchoring, and verification*. So v2 wraps cheap models in a system that fixes each:

- **Line-numbered diffs (A1).** Analysts see absolute line numbers on every hunk line and cite the numbers they're shown — localization stops being a guess.
- **Evidence anchoring (A2).** Every finding must quote the offending line. A deterministic pass checks that quote against the diff; **a finding whose evidence isn't literally in the code is dropped before it can post.** Hallucinations become structurally impossible, not just discouraged by a prompt.
- **Self-consistency ensemble (A3).** Each analyst reviews three times; findings are majority-voted. A one-off miss or a one-off hallucination doesn't survive the vote. DeepSeek's prompt caching makes 3× sampling cost ~1.3×, not 3×.
- **Verifier pass (A4).** A separate agent adjudicates every surviving finding against the numbered diff — confirm / reject / downgrade — before the reviewer writes a word. It runs a **rubric meta-judge** (argue the rejection first; keep a finding only if the visible code survives) — single pass, no debate, which the research shows amplifies bias rather than reducing it.

**v2.5 added four more $0 levers — and measured them honestly.** Each is a config toggle; all ship **off by default** because, on cheap `deepseek-v4-flash` over the 37-fixture benchmark, every lever arm landed *within run-to-run noise of the levers-off baseline* — no measurable accuracy gain. We don't flip a default that changes review behavior without an eval that justifies it, so they stay opt-in (and turn on together in `mode: thorough` for max-recall users):

- **Confirmation-bias debiasing (`debias`).** Judge each line on its own merits, ignore the PR title's framing — a reassuring title can't hide a real bug, an alarming one can't conjure a fake one. Accuracy-neutral here, but real **injection hardening**, so it's the lever most worth enabling.
- **Calibration anchors (`calibration`).** Per-agent *flag / stay-silent* examples in the cached prompt prefix (nearly free per call) to pin a cheap model's severity bar.
- **Prompt-diverse ensemble (`lenses`).** Ensemble samples take different viewpoints (plain / checklist / adversarial) instead of only a temperature jitter.
- **Verdict-first chain-of-thought (`cot`).** An optional short reasoning scan, with each finding emitted verdict-first (the ordering that minimizes abstention).

The honest result — *levers that didn't beat the baseline get shipped off, not dressed up as a win* — is the same discipline as the leaderboard below.

### The leaderboard

The same `deepseek-v4-flash` model ($0.14 / $0.28 per 1M tokens), 17 fixtures across 5 languages, 3 runs each (51 fixture-runs), 2026-06-11:

| Config | Caught | Clean-fixture false positives | Cost / review |
|---|---|---|---|
| Naive single pass | 47/51 (92%) | **2** | ~$0.002 |
| **PR Sentinel v2 (ensemble + verifier)** | **49/51 (96%)** | **0** | ~$0.005 |

The system turns a budget model from "good with the occasional false positive on clean code" into "better, with **zero** false positives" — for half a cent a review. The two remaining v2 misses are different fixtures on different runs (honest run-to-run variance, disclosed not tuned away). The naive run's false positives were the Test agent flagging a refactor whose test was *in the same diff* — exactly the noise the ensemble + verifier eliminate.

The fixture set includes seeded bugs (SQL injection, XSS, path traversal, hardcoded secret, N+1, blocking-async, leaky abstraction, untested money-code) in Python / JS / TS / Go / Java, **hard negatives** (correctly-parameterized SQL that looks scary; a bounded loop that looks O(n²)), and **two prompt-injection vectors** (in the diff and in the PR title) — both of which leak nothing and get flagged as attacks.

#### v2.5 — harder benchmark, and the levers measured honestly

The benchmark was expanded to **37 fixtures across 7 languages** (added Ruby + C#, new bug classes — SSRF, insecure deserialization, `eval`, open redirect, ReDoS, secret-logging, weak crypto, TLS-disabled — more clean false-positive controls, and **misleading-title `mt_*` fixtures** that plant a real bug under a calm title or clean code under an alarming one, to measure debiasing). On this harder set, 3 runs each on `deepseek-v4-flash`:

| Config | Passed (3×37 = 111) | Notes |
|---|---|---|
| **PR Sentinel system (ensemble + verifier)** | **101/111 (91%)** | the shipped baseline |
| + debias + calibration | 98/111 (88%) | within noise |
| + debias only | ~89% (run-to-run) | within noise |

The five research levers (debias, calibration, diverse lenses, verdict-first CoT, rubric verifier) **land within flash's run-to-run variance of the baseline** — no measurable accuracy gain — so they ship **off by default**, opt-in via config or `mode: thorough`. The spread in every arm is dominated by the same handful of genuinely hard fixtures (a validated query in a handler that tempts a false positive; a hardcoded secret under a "style: rename" title that flash just doesn't reliably catch). Publishing a lever as a win it didn't earn would be exactly the fixture-tuning this project refuses to do.

#### The honest real-PR number (and where the work goes next)

Seeded fixtures measure "does the right agent catch a planted bug with no false positives" — useful, but easy. The harder, truer test (`evals/realpr.py`) takes **real merged bug-fix PRs**, reverses them to reintroduce the bug, and checks recall. On **60 such PRs** across 20 repos in Python / JS / TS / Go (requests, flask, django, pydantic, sqlalchemy, scrapy, axios, lodash, vue, gin, cobra, echo, …), `deepseek-v4-flash` caught **28% (17/60)** diff-only, at **precision 68% / F1 40%** (via the forward-fixed-version false-positive proxy) — far below the seeded 91%, and a sobering, honest read on real-world recall (the best commercial tools sit ~45–57% on real PRs per the independent [Martian benchmark](https://www.codeant.ai/blogs/ai-code-review-benchmark-results-from-200-000-real-pull-requests)). The misses are the *context-dependent* defects — a removed workaround, a typing regression, a teardown ordering bug — that no ±N-line reviewer can judge from the diff alone. We tested the obvious fixes and **measured every one honestly**: repository-aware context (+3pp, Python-only), SAST grounding (precision-safe but no net recall), an agentic tool-loop (*hurt* recall — attention dilution), and deeper reasoning effort (no gain). None beat diff-only focus — the ceiling is model capability, not the system (even a bigger same-family model, `deepseek-v4-pro`, scored *lower*: 11/60). We publish the 28% because an honest hard number you can improve beats a flattering easy one you can't.

The first step of that roadmap is already in: an opt-in **repository-context prefetch** (`accuracy.repo_context`) that hands analysts the definitions of the cross-file symbols a diff references. Over 3 runs on the same 32 PRs it moved recall **24% → 27%** — and the per-PR analysis is honest about why: it reliably adds one Python context-dependent catch (a type bug resolved by the imported definitions), with the rest of the delta being run-to-run noise on non-Python files (which get no context). A small, real, Python-only gain that also costs extra fetches per review — so it ships **off by default**, a recommended opt-in for Python-heavy repos. JS/TS (same-file + relative imports) and Go (same-file siblings) coverage is now in; flipping it default-on still waits on a measured multi-language win, not just the coverage.

> Two reasoning facts worth knowing (verified against the DeepSeek API): `deepseek-v4-flash` reasons by default, and **turning that off collapses recall to ~64%** — so the system keeps reasoning on. Reasoning controls are exposed (`accuracy.analyst_thinking`, `reasoning_effort`) but default to the provider's setting.

Reproduce with your own key:

```bash
PR_SENTINEL_API_KEY=sk-... PR_SENTINEL_BASE_URL=https://api.deepseek.com/v1 \
PR_SENTINEL_MODEL=deepseek-v4-flash python evals/run.py --runs 3
```

The unit/integration suite (**243 tests**, LLM and GitHub API fully mocked, no network) runs in CI: `pytest`.

## On-demand commands

Comment on any PR (repo owners / members / collaborators only — a drive-by commenter can't spend your key):

- `@pr-sentinel review` — re-run the full review
- `@pr-sentinel ask <question>` — ask anything about the diff; get a grounded, cited answer
- `@pr-sentinel describe` — write a summary + file walkthrough into the PR body

To enable them, add the `issue_comment` trigger to your workflow:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
  issue_comment:
    types: [created]
```

## Roadmap

See [ROADMAP.md](ROADMAP.md): GitHub App / hosted tier, auto-fix suggestions, multi-provider git hosts (GitLab/Bitbucket), and fork-PR review via maintainer-gated re-runs.

## Design decisions

Every significant architecture choice — language, orchestration shape, dedup strategy, provider abstraction, large-diff handling — is documented with its tradeoffs in [DECISIONS.md](DECISIONS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: `pip install -e ".[dev]"`, `pytest`, open a PR — PR Sentinel reviews it. 🙂

## License

[MIT](LICENSE) © Moaz Muhammad
