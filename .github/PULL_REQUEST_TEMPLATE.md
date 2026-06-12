<!-- PR Sentinel will review this PR automatically. -->

## What & why

<!-- What does this change do, and why? -->

## Checklist

- [ ] Tests added/updated and `pytest` is green (LLM + GitHub mocked, no network)
- [ ] `ruff check src tests evals` is clean
- [ ] If I touched a prompt, I ran the evals before/after and didn't regress the clean fixtures
- [ ] Docs updated if behavior/config/usage changed (README, DECISIONS, AGENTS)
- [ ] No secret can reach a prompt, state field, or log
