"""LLM provider abstraction (D6, upgraded in V2).

The OpenAI-compatible chat-completions protocol is the primary provider: a
thin async httpx client with a configurable base_url reaches OpenAI,
OpenRouter, Groq, DeepSeek, and local Ollama with one integration. V2 adds a
native Anthropic Messages client behind the same Protocol (the oldest roadmap
promise), per-call model override (two-tier routing), JSON response-format
with graceful fallback, a shared pooled connection, and cached-token
accounting (DeepSeek bills cached input at ~1/50th — worth surfacing).

Deliberately self-written instead of LangChain wrappers or LiteLLM: every
line is understood, the Docker image stays slim, and the failure surface is
ours. Secrets live ONLY here, in the HTTP client layer — no prompt template
or formatter can reach them (threat model, Threat 2.3).
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
    cached_tokens: int = 0  # portion of prompt_tokens served from prompt cache


class LLMProvider(Protocol):
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        model: str | None = None,
        json_mode: bool = False,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
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
        # One pooled connection per provider instance (V2 D1) — four parallel
        # analysts shouldn't pay a TLS handshake per call.
        self._client: httpx.AsyncClient | None = None
        # Set False after an endpoint rejects response_format (V2 A6).
        self._json_mode_supported: bool | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        model: str | None = None,
        json_mode: bool = False,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> CompletionResult:
        payload: dict = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Reasoning control (DeepSeek V4: thinking is a request parameter, default
        # on; temperature is a no-op while thinking is enabled). Only sent when
        # explicitly set, so non-DeepSeek OpenAI-compatible endpoints that don't
        # know the field are unaffected unless the user opts in.
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking and reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
        use_json = json_mode and self._json_mode_supported is not False
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        last_error = "unknown error"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._semaphore:
                    response = await self._get_client().post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
            except httpx.HTTPError as exc:
                last_error = f"network error: {type(exc).__name__}"
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                if use_json:
                    self._json_mode_supported = True
                return self._parse(response)

            # Endpoint doesn't speak response_format -> remember and retry bare.
            if response.status_code == 400 and use_json:
                logger.info("Endpoint rejected response_format; falling back without it.")
                self._json_mode_supported = False
                payload.pop("response_format", None)
                use_json = False
                continue

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
            details = usage.get("prompt_tokens_details") or {}
            cached = int(
                usage.get("prompt_cache_hit_tokens")  # DeepSeek's field
                or details.get("cached_tokens")        # OpenAI's field
                or 0
            )
            return CompletionResult(
                text=text,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cached_tokens=cached,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected response shape: {type(exc).__name__}") from exc

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # 1s, 2s, 4s with jitter — enough to ride out a 429 burst from
        # parallel analysts without stalling the whole run.
        delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        logger.info("Retrying provider call in %.1fs (attempt %d)", delay, attempt)
        await asyncio.sleep(delay)


class AnthropicProvider:
    """Native Anthropic Messages API client behind the same Protocol (V2 D2).

    Same thin-client philosophy: ~70 lines of httpx, no SDK dependency.
    `json_mode` is accepted and ignored (Anthropic has no response_format;
    the prompt + parser tolerance carry it).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-haiku-4-5-20251001",
        *,
        max_concurrent: int = 8,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        model: str | None = None,
        json_mode: bool = False,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> CompletionResult:
        # `thinking`/`reasoning_effort` are accepted for Protocol parity and
        # ignored — Anthropic exposes extended thinking through a different field
        # we don't wire here (the prompt + parser tolerance carry reasoning).
        payload = {
            "model": model or self._model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error = "unknown error"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._semaphore:
                    response = await self._get_client().post(
                        f"{self._base_url}/v1/messages",
                        json=payload,
                        headers={
                            "x-api-key": self._api_key,
                            "anthropic-version": "2023-06-01",
                        },
                    )
            except httpx.HTTPError as exc:
                last_error = f"network error: {type(exc).__name__}"
                await OpenAICompatProvider._backoff(attempt)
                continue
            if response.status_code == 200:
                return self._parse(response)
            last_error = f"HTTP {response.status_code} from provider"
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                await OpenAICompatProvider._backoff(attempt)
                continue
            break
        raise ProviderError(f"completion failed after {_MAX_ATTEMPTS} attempt(s): {last_error}")

    @staticmethod
    def _parse(response: httpx.Response) -> CompletionResult:
        try:
            data = response.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", [])
                if block.get("type") == "text"
            )
            usage = data.get("usage") or {}
            return CompletionResult(
                text=text,
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                cached_tokens=int(usage.get("cache_read_input_tokens", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected response shape: {type(exc).__name__}") from exc


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
    "deepseek-v4-pro": (0.28, 0.42),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """Return (estimated_cost, is_exact_rate)."""
    key = model.lower()
    exact = key in MODEL_PRICES
    in_rate, out_rate = MODEL_PRICES.get(key, MODEL_PRICES["gpt-5-mini"])
    cost = (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000
    return cost, exact
