# Contributing

Thanks for considering it. Short rules:

## Dev setup

```bash
git clone https://github.com/moazmo/pr-sentinel
cd pr-sentinel
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Running tests

```bash
pytest          # 256 tests, LLM and GitHub API fully mocked — no network, no key needed
ruff check src tests
```

Evals (these DO hit a real LLM, bring your own key):

```bash
PR_SENTINEL_API_KEY=sk-... python evals/run.py
```

## Ground rules

- **Tests are part of done.** New logic ships with tests that run without a live LLM (`tests/conftest.py` has the `MockProvider`).
- **No noise.** This is a code reviewer; its credibility dies on false positives. Prompt changes must not regress the clean fixtures in `evals/`.
- **Secrets never reach prompts, state, or logs.** The provider key and GitHub token live in the HTTP client layer only.
- **Agent prompts are product surface.** They live in `src/pr_sentinel/prompts/` as readable markdown — improvements very welcome, but run the evals before and after.
- If you found a real bug, add a fixture or test reproducing it in the same PR.

## Security issues

See [SECURITY.md](SECURITY.md) — please report privately first.
