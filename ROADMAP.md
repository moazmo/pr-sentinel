# Roadmap

v1 is deliberately scoped: a shipped five-agent reviewer with one provider done excellently beats a half-built one with three providers and a dashboard. Everything below is deferred **on purpose**.

## Next (fast follows)

- **Native Anthropic provider** — a clean drop-in behind the existing `LLMProvider` protocol (Anthropic models are already reachable today via OpenRouter).
- **Inline line-by-line comments** — post findings as PR review comments anchored to the diff, with the summary comment as the index.
- **Injection-aware Security agent tuning** — keep improving the "the reviewer catches people trying to jailbreak the reviewer" behavior measured by the injection eval fixture.

## Later

- **Deep context mode** — optional `actions/checkout` integration to pull surrounding code (not just hunks) for the analysts.
- **Fork-PR review via maintainer-gated re-runs** — a documented label-gated workflow for maintainers who want fork reviews without `pull_request_target` foot-guns.
- **Auto-fix suggestions as suggested commits** — GitHub's suggestion blocks, applied with one click.
- **Multi-language prompt packs** — per-language analyst tuning beyond the `language_hint` knob.
- **Image signing / SLSA provenance** for the release pipeline.

## Much later (only after traction evidence)

- **GitHub App** — one-click install, no workflow file.
- **Hosted tier** — managed keys, team dashboard, org-wide policies. The engine stays open-source either way (open-core, honestly drawn: paid is convenience, not capability).
- **Learning from past reviews** — memory across PRs in a repo.
