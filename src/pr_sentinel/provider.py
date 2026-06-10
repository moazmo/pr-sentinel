"""LLM provider abstraction (D6).

The OpenAI-compatible chat-completions protocol IS the v1 provider: a thin
async httpx client with a configurable base_url reaches OpenAI, OpenRouter,
Groq, DeepSeek, Mistral, and local Ollama with one integration.

Deliberately ~100 lines and self-written instead of LangChain model wrappers
or LiteLLM: every line is understood, the Docker image stays slim, and the
failure surface is ours. Secrets live ONLY here, in the HTTP client layer —
no prompt template or formatter can reach them (threat model, Threat 2.3).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletionResult:
    text: str
    prompt_tokens: int
    completion_tokens: int


class LLMProvider(Protocol):
    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> CompletionResult: ...


class ProviderError(Exception):
    """Raised when a completion ultimately fails after retries.

    The message must never contain the API key; httpx errors don't include
    request headers, and we never interpolate the key ourselves.
    """


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class OpenAICompatProvider:
    """Async client for any OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-5-mini",
        *,
        max_concurrent: int = 8,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = timeout_seconds

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.1
    ) -> CompletionResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error = "unknown error"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._semaphore:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(
                            f"{self._base_url}/chat/completions",
                            json=payload,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                        )
            except httpx.HTTPError as exc:
                last_error = f"network error: {type(exc).__name__}"
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return self._parse(response)

            last_error = f"HTTP {response.status_code} from provider"
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                await self._backoff(attempt)
                continue
            break

        raise ProviderError(f"completion failed after {_MAX_ATTEMPTS} attempt(s): {last_error}")

    @staticmethod
    def _parse(response: httpx.Response) -> CompletionResult:
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            return CompletionResult(
                text=text,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected response shape: {type(exc).__name__}") from exc

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # 1s, 2s, 4s with jitter — enough to ride out a 429 burst from
        # four parallel analysts without stalling the whole run.
        delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        logger.info("Retrying provider call in %.1fs (attempt %d)", delay, attempt)
        await asyncio.sleep(delay)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token for code+English). Used only for
    budgeting/batching decisions and the dry-run estimate — real billing
    numbers come from the provider's usage field."""
    return max(1, len(text) // 4)


# $ per 1M tokens (input, output) for the cost line in the comment footer.
# Unknown models fall back to gpt-5-mini rates with a "~" marker.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "deepseek-v4-flash": (0.14, 0.28),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """Return (estimated_cost, is_exact_rate)."""
    key = model.lower()
    exact = key in MODEL_PRICES
    in_rate, out_rate = MODEL_PRICES.get(key, MODEL_PRICES["gpt-5-mini"])
    cost = (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000
    return cost, exact
