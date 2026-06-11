# Roadmap

Scoped deliberately. v1 shipped a five-agent reviewer; **v2 shipped the accuracy-per-dollar core** (line-numbered diffs, evidence anchoring, self-consistency ensemble, verifier pass, inline comments, `@pr-sentinel` commands, native Anthropic provider, a 17-fixture / 5-language eval leaderboard). Everything below is deferred **on purpose**.

## Shipped in v2 (no longer roadmap)

- Inline line-by-line review comments
- Native Anthropic provider (Messages API)
- Self-consistency ensemble + deterministic evidence verification + verifier adjudication
- On-demand commands: `@pr-sentinel review | ask | describe`
- File ranking under the file cap; head-ref context extension; structured-output (JSON) mode
- Published model × strategy eval leaderboard with false-positive rates

## Next

- **Flagship head-to-head row** in the leaderboard (GPT-5 / Claude Opus single pass) once a flagship key is wired — the "cheap ensemble vs expensive single pass" comparison the architecture is built to win.
- **Auto-fix suggestions** as GitHub suggestion blocks (one-click apply).
- **Deep-context mode** — optional `actions/checkout` to pull whole-file/dependency context beyond ±N lines.
- **Per-language prompt packs** beyond the `language_hint` knob.

## Later

- **Multi git-provider support** — GitLab, Bitbucket, Azure DevOps.
- **Image signing / SLSA provenance** for the release pipeline; digest-pinned base image.
- **Fork-PR review** via a documented maintainer-gated label workflow (never `pull_request_target`).

## Much later (only after traction evidence)

- **GitHub App** — one-click install, no workflow file.
- **Hosted tier** — managed keys, team dashboard, org-wide policy. Engine stays open-source (open-core: paid is convenience, not capability).
- **Learning across PRs** — repo-level memory of past reviews.
