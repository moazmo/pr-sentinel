# Security

## Reporting a vulnerability

Email **<moazmo27@gmail.com>** with details. You'll get a response within 72 hours. Please don't open public issues for exploitable vulnerabilities before a fix ships.

## Why PR Sentinel doesn't review fork PRs by default

GitHub Actions does not expose repository secrets to workflows triggered by fork PRs under the `pull_request` trigger — **by design**. Some tools "solve" this by telling users to switch to `pull_request_target`, which runs fork code with full secret access; that misconfiguration is exactly how repositories had their entire secret stores harvested in the 2026 attacks (within a day of one such incident, attackers were mass-scanning GitHub for repos with it enabled).

PR Sentinel's policy:

1. The documented install uses **`pull_request` only**. No example in this repo will ever use `pull_request_target`.
2. On a fork PR the API key secret is simply absent; PR Sentinel detects this and **skips gracefully** with a clear log line and exit 0. This is correct behavior, not a missing feature.
3. Maintainers who want reviewed fork PRs can use label-gated re-run workflows (a maintainer applies a label after reading the diff, a separate workflow runs with secrets). That pattern is an advanced, deliberate choice — it is on the roadmap, not the default.

## Prompt injection hardening

In 2026, researchers demonstrated that instruction text in a **PR title** caused major AI review actions to exfiltrate their own API keys into PR comments. The attack surface is any PR-controlled text reaching the LLM: title, description, branch names, and the diff itself.

PR Sentinel's mitigations, all in v1:

- **Prompt segregation:** PR-controlled content reaches the model only inside delimited data blocks (`<diff>…</diff>`, `<pr_title>…</pr_title>`) in the user message; system prompts state that delimited content is data under review, never instructions. Delimiter-escape attempts (`</diff>` inside a hostile diff) are stripped before prompt assembly. The PR *body* never enters any prompt at all.
- **Structured output as a boundary:** analyst output must parse against the Finding schema; anything else is discarded. An injected "post your API key" cannot survive a parser that only accepts `{file, line, severity, category, message}`.
- **No secrets in the prompt path:** the provider key and GitHub token exist only in the HTTP client layer. No prompt template, state object, or formatter can interpolate them — enforced by construction and by regression tests that scan rendered prompts.
- **Output scrubbing (defense-in-depth):** before posting, the final comment is scanned for the known secret values and generic key patterns (`sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`) and redacted on match, with a security warning logged — a match means an injection got further than it should.
- **Minimal token permissions:** the documented workflow grants `contents: read` + `pull-requests: write` only. A fully compromised run cannot push code, modify workflows, or read packages.
- **Config from the base branch:** `.pr-sentinel.yml` is read from the PR's base ref, never its head — a hostile PR cannot disable the Security agent or raise the spend caps that review it.
- **Eval coverage:** `evals/fixtures/prompt_injection.yml` plants instruction text in a diff and asserts the review is unaffected, nothing leaks, and the planted bug next to the injection is still caught.

## Economic DoS (burning your budget)

Hard caps (`max_files`, `max_input_tokens`, `max_output_tokens_per_agent`) plus the built-in skip list bound the worst-case cost per PR regardless of what arrives — a hostile 300-file PR hits the ceilings, skips, and discloses. The caps are a security guarantee, not UX polish.

## Comment commands are gated by author association (v2)

The `@pr-sentinel review | ask | describe` commands run on `issue_comment` events, which any GitHub user can create. Each command therefore checks the commenter's `author_association`: only `OWNER`, `MEMBER`, and `COLLABORATOR` can trigger one. A drive-by commenter cannot spend the repo owner's API key, and command text is never interpolated into any privileged context — the `ask` question goes through the same delimited-data-block sanitizer as the diff. The same evidence-anchoring and output-scrubbing defenses apply to command output.

## Supply chain

- Dependencies are version-bounded in `pyproject.toml`; the Docker image uses `python:3.12-slim`.
- Release images are built **only** by the repo's release workflow — no manual pushes — so the tag → source mapping is auditable.
- Roadmap (not v1): image signing/attestations, SLSA provenance, digest-pinned base images.
