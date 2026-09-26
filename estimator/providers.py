"""The AI model behind MenuLens, made swappable.

Every provider does one job: take a system prompt, a user prompt and a JSON
schema, and return the model's answer as a dict. Everything MenuLens-specific
(the prompt wording, the range rules, suppression) stays in estimator.py, so
switching model never changes what the product promises.

    gemini     Google Gemini API. Has a free tier, so this is the default.
    anthropic  Claude API. Paid only.
    mock       No model at all: deterministic fake answers for testing the plumbing
               (server, extension, frontend) at zero cost. Its accuracy means nothing.

Pick one with get_provider("gemini"), or set MENULENS_PROVIDER for the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional: keys can still come from the shell
    load_dotenv = None

# API keys and settings from estimator/.env. Anything already set in the shell wins.
if load_dotenv:
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

DEFAULT_MODELS = {
    "gemini": "gemini-3.6-flash",
    "anthropic": "claude-opus-5",
    "mock": "mock",
}


class ProviderError(Exception):
    """The model could not be reached or returned something unusable."""


class ProviderAuthError(ProviderError):
    """The API key is missing, wrong, or not allowed to use this model."""


class ProviderModelUnavailable(ProviderError):
    """The requested model doesn't exist or isn't offered to this account. Pick another with --model."""


class ProviderRateLimited(ProviderError):
    """The provider's rate limit or free-tier quota is used up.

    retry_after: seconds the provider asked us to wait, when it said.
    daily: True when a per-day quota is exhausted, so retrying today is pointless.
    """

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


class ProviderTimeout(ProviderError):
    """The model took longer than the time limit to answer."""


class ProviderBusy(ProviderError):
    """A temporary server-side failure (overloaded, 5xx). Usually worth retrying shortly."""


class Suppressed(Exception):
    """The item can't be given a range worth acting on (declined, or too vague)."""


class Provider:
    name = "base"

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODELS[self.name]

    def generate(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        """Return (parsed JSON answer, id of the model that actually answered)."""
        raise NotImplementedError


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str | None = None, timeout_s: float = 90.0):
        super().__init__(model)
        import httpx
        from google import genai
        from google.genai import errors, types

        self._errors, self._types, self._httpx = errors, types, httpx
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise ProviderAuthError("GEMINI_API_KEY is not set")
        # No hidden retries: the SDK's own backoff waited minutes with no output, which looks
        # like a hang. Callers decide whether to wait (bench.py does, visibly; the server
        # answers 429 straight away). The timeout is in milliseconds.
        self.client = genai.Client(http_options=types.HttpOptions(
            timeout=int(timeout_s * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ))

    def generate(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    # MenuLens passes no tools, so switch the SDK's tool-calling loop off.
                    automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except self._errors.APIError as exc:
            message = str(exc.message or exc)
            if exc.code in (401, 403) or (exc.code == 400 and "API key" in message):
                raise ProviderAuthError(message) from exc
            if exc.code == 404:
                raise ProviderModelUnavailable(message) from exc
            if exc.code == 429:
                raw = json.dumps(exc.details)
                delay = re.search(r'"retryDelay":\s*"([\d.]+)s"', raw)
                raise ProviderRateLimited(
                    message,
                    retry_after=float(delay.group(1)) if delay else None,
                    daily="PerDay" in raw,
                ) from exc
            if exc.code and exc.code >= 500:
                raise ProviderBusy(f"{exc.code} {message}") from exc
            raise ProviderError(f"{exc.code} {message}") from exc
        except self._httpx.TimeoutException as exc:
            raise ProviderTimeout("no answer within the time limit") from exc
        except self._httpx.TransportError as exc:
            raise ProviderError(f"network error: {exc}") from exc

        text = response.text
        if not text:
            # Blocked by a safety filter or otherwise empty: no answer to give.
            raise Suppressed("the model returned no answer")
        try:
            return json.loads(text), self.model
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model returned invalid JSON: {text[:120]!r}") from exc


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str | None = None):
        super().__init__(model)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def generate(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise ProviderAuthError(str(exc)) from exc
        except a.NotFoundError as exc:
            raise ProviderModelUnavailable(str(exc)) from exc
        except a.RateLimitError as exc:
            raise ProviderRateLimited(str(exc)) from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:  # includes 529 "overloaded"
                raise ProviderBusy(str(exc)) from exc
            raise ProviderError(str(exc)) from exc
        except a.APIConnectionError as exc:
            raise ProviderError(str(exc)) from exc

        if response.stop_reason == "refusal":
            raise Suppressed("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise ProviderError("response was cut off at max_tokens")
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text), response.model


class MockProvider(Provider):
    """Fake, repeatable answers derived from a hash of the prompt. Costs nothing."""

    name = "mock"

    def generate(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        if "estimates" in schema.get("properties", {}):  # a batch: one fake answer per dish
            dishes = re.findall(r"^(\d+)\. (Item: .*)$", prompt, re.M)
            return {"estimates": [{"index": int(n), **self.generate(system, dish, {})[0]}
                                  for n, dish in dishes]}, "mock"
        h = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
        midpoint = 150 + h % 1200
        band = ("high", "medium", "low")[h % 3]
        half = {"high": 0.15, "medium": 0.25, "low": 0.4}[band]
        return {
            "low": round(midpoint * (1 - half)),
            "high": round(midpoint * (1 + half)),
            "midpoint": midpoint,
            "portion_assumption_g": 200 + h % 300,
            "band": band,
            "rationale": "Mock estimate for testing - not a real calorie figure.",
        }, "mock"


class FallbackProvider(Provider):
    """Several models of one provider, tried in order.

    Free tiers limit each model separately, per minute and per day. When a model is rate
    limited, busy or slow, it rests for a while and the next model answers instead, so
    the combined free allowance is the sum of all of them. Only when every model is
    resting does the caller see the error.
    """

    DAILY_REST_S = 3600      # re-check a model whose daily quota ran out once an hour
    DEFAULT_REST_S = 30      # rate limited without a suggested wait, busy, or timed out

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.name = providers[0].name
        self.model = ",".join(p.model for p in providers)
        self._resting_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _rest(self, model: str, seconds: float) -> None:
        with self._lock:
            self._resting_until[model] = time.monotonic() + seconds

    def generate(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        now = time.monotonic()
        with self._lock:
            ready = [p for p in self.providers if self._resting_until.get(p.model, 0) <= now]
        last: ProviderError | None = None
        for provider in ready:
            try:
                return provider.generate(system, prompt, schema)
            except ProviderRateLimited as exc:
                rest = self.DAILY_REST_S if exc.daily else (exc.retry_after or self.DEFAULT_REST_S)
                self._rest(provider.model, rest)
                last = exc
            except (ProviderBusy, ProviderTimeout, ProviderModelUnavailable) as exc:
                self._rest(provider.model, self.DEFAULT_REST_S)
                last = exc
        if last is None:  # every model is resting from an earlier failure
            with self._lock:
                wait = min(self._resting_until.values()) - now
            last = ProviderRateLimited("all models are resting", retry_after=max(1.0, wait))
        # Daily only if every model is out for the day; otherwise it's a short wait.
        if isinstance(last, ProviderRateLimited) and last.daily:
            with self._lock:
                soonest = min(self._resting_until.values()) - now
            if soonest < self.DAILY_REST_S - 60:
                last = ProviderRateLimited(str(last), retry_after=max(1.0, soonest))
        raise last


PROVIDERS = {p.name: p for p in (GeminiProvider, AnthropicProvider, MockProvider)}


def get_provider(name: str | None = None, model: str | None = None) -> Provider:
    """MENULENS_MODEL may list several models, comma-separated, to fall back between."""
    name = (name or os.environ.get("MENULENS_PROVIDER") or "gemini").lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; choose from {sorted(PROVIDERS)}")
    models = [m.strip() for m in (model or os.environ.get("MENULENS_MODEL") or "").split(",") if m.strip()]
    if len(models) > 1:
        return FallbackProvider([PROVIDERS[name](m) for m in models])
    return PROVIDERS[name](models[0] if models else None)
