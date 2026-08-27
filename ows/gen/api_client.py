"""OpenAI-compatible API client with per-request metrics for ows.gen.

Design constraints (approved plan):

- ``OWS_GEN_API_BASE_URL`` / ``OWS_GEN_API_KEY`` / ``OWS_GEN_MODEL`` come from
  environment variables only, falling back to ``OPENAI_BASE_URL`` /
  ``OPENAI_API_KEY`` / ``AWM_SYN_OVERRIDE_MODEL``. Keys are never accepted as
  CLI arguments, never logged, never written to disk.
- Retries use exponential backoff with jitter, and ONLY for retryable errors
  (429 / 5xx / timeout / connection). Other 4xx is not retried — retrying a
  prompt error wastes money.
- A refusal / empty completion is a failure, not a silent success.
- Every call produces one metrics event (latency, token usage, error type,
  retry count) handed to :class:`ows.gen.stats.RunStats`. Events never
  contain secrets.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import openai
import tiktoken
from loguru import logger

RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


def count_tokens(text: str, model: str | None = None) -> int:
    """Token count via tiktoken; cl100k_base fallback for unknown model names."""
    enc = None
    if model:
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = None
    if enc is None:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return len(text)
    return len(enc.encode(text))


@dataclass
class APICallResult:
    request_id: str
    stage: str  # "scenario" | "task"
    status: str  # http_ok | refusal_or_empty | http_error | client_error
    error_type: Optional[str]  # e.g. "http_429", "timeout", "connection"
    content: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    token_source: str  # "usage" | "estimate"
    latency_ms: int
    retry_count: int
    estimated_input_tokens: int
    attempts: list[dict] = field(default_factory=list)


class BudgetExceeded(Exception):
    """Raised when the configured cost/token budget is exceeded."""


class GenAPIClient:
    """Async OpenAI-compatible client. One call = one generation request.

    Parameters
    ----------
    concurrency : int
        Max in-flight requests (asyncio semaphore).
    max_retries : int
        Max retries per request, only for retryable errors.
    base_delay : float
        Seconds before the first retry; doubles per attempt with jitter.
    stats : optional callable ``(dict event) -> None``
        Receives one event dict per finished request.
    budget_check : optional callable ``() -> None``
        Called after each request; should raise :class:`BudgetExceeded` if
        the accumulated cost/token budget is exhausted.
    """

    def __init__(
        self,
        *,
        concurrency: int,
        max_retries: int = 3,
        base_delay: float = 2.0,
        timeout: float = 300.0,
        disable_thinking: bool = True,
        stats: Optional[Callable[[dict], None]] = None,
        budget_check: Optional[Callable[[], None]] = None,
    ) -> None:
        base_url = os.environ.get("OWS_GEN_API_BASE_URL") or os.environ.get(
            "OPENAI_BASE_URL"
        )
        api_key = os.environ.get("OWS_GEN_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = (
            os.environ.get("OWS_GEN_MODEL")
            or os.environ.get("AWM_SYN_OVERRIDE_MODEL")
            or None
        )
        if not api_key:
            raise ValueError(
                "API key not found: set OWS_GEN_API_KEY (or OPENAI_API_KEY) in the environment"
            )
        if not base_url:
            raise ValueError(
                "API base URL not found: set OWS_GEN_API_BASE_URL (or OPENAI_BASE_URL)"
            )
        if not model:
            raise ValueError(
                "Model not found: set OWS_GEN_MODEL (or AWM_SYN_OVERRIDE_MODEL)"
            )

        self.model = model
        self.host = base_url.split("//", 1)[-1].split("/", 1)[0]  # for logging only
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._base_delay = base_delay
        # Reasoning models (e.g. GLM-5.1) think by default and burn the whole
        # output budget on internal reasoning. Disable for structured JSON.
        self._extra_body = {"thinking": {"type": "disabled"}} if disable_thinking else None
        self._disable_thinking = disable_thinking
        self._stats = stats
        self._budget_check = budget_check

    async def call(
        self,
        messages: list[dict[str, str]],
        *,
        stage: str,
        temperature: float = 1.0,
        max_output_tokens: int = 4096,
        json_mode: bool = True,
    ) -> APICallResult:
        """Send one chat-completion request with retries; return metrics."""
        request_id = uuid.uuid4().hex[:12]
        estimated_input = sum(
            count_tokens(str(m.get("content", "")), self.model) for m in messages
        )
        attempts: list[dict] = []
        last_status = "client_error"
        last_error_type = "unknown"
        retry_delay = self._base_delay
        started = time.monotonic()

        for attempt in range(self._max_retries + 1):
            t0 = time.monotonic()
            # If the endpoint rejects the thinking param with 400, retry once
            # without it (bounded: only when we sent it and it was rejected).
            extra_body = self._extra_body
            if last_error_type == "http_400" and extra_body is not None:
                extra_body = None
                logger.info(
                    f"[gen:{stage}] {request_id} endpoint rejected thinking param; "
                    "retrying without extra_body"
                )
            try:
                async with self._sem:
                    resp = await self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=temperature,
                        max_completion_tokens=max_output_tokens,
                        response_format={"type": "json_object"} if json_mode else None,
                        extra_body=extra_body,
                    )
                latency_ms = int((time.monotonic() - t0) * 1000)
                usage = getattr(resp, "usage", None)
                prompt_tokens = (
                    usage.prompt_tokens
                    if usage is not None and usage.prompt_tokens is not None
                    else estimated_input
                )
                completion_tokens = (
                    usage.completion_tokens
                    if usage is not None and usage.completion_tokens is not None
                    else 0
                )
                token_source = (
                    "usage"
                    if usage is not None and usage.prompt_tokens is not None
                    else "estimate"
                )
                content = resp.choices[0].message.content if resp.choices else None
                finish_reason = (
                    resp.choices[0].finish_reason if resp.choices else None
                )
                status = "http_ok" if (content and content.strip()) else "refusal_or_empty"
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": status,
                        "error_type": None,
                        "latency_ms": latency_ms,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "token_source": token_source,
                        "finish_reason": finish_reason,
                    }
                )
                result = APICallResult(
                    request_id=request_id,
                    stage=stage,
                    status=status,
                    error_type=None,
                    content=content if status == "http_ok" else None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    token_source=token_source,
                    latency_ms=latency_ms,
                    retry_count=attempt,
                    estimated_input_tokens=estimated_input,
                    attempts=attempts,
                )
                logger.info(
                    f"[gen:{stage}] {request_id} {status} "
                    f"{latency_ms}ms retries={attempt} "
                    f"tokens={prompt_tokens}+{completion_tokens} ({token_source})"
                )
                self._emit(result)
                return result
            except openai.APITimeoutError as e:
                last_status, last_error_type, retryable = "http_error", "timeout", True
            except openai.APIConnectionError as e:
                last_status, last_error_type, retryable = "http_error", "connection", True
            except openai.RateLimitError as e:
                last_status, last_error_type, retryable = "http_error", "http_429", True
            except openai.APIStatusError as e:
                code = e.status_code
                last_status = "http_error"
                last_error_type = f"http_{code}"
                # 400 while sending the thinking param: retry once without it
                retryable = code in RETRYABLE_HTTP_STATUS or (
                    code == 400 and extra_body is not None
                )
            except Exception as e:
                last_status, last_error_type, retryable = (
                    "client_error",
                    type(e).__name__,
                    False,
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            attempts.append(
                {
                    "attempt": attempt,
                    "status": last_status,
                    "error_type": last_error_type,
                    "latency_ms": latency_ms,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "token_source": "estimate",
                }
            )
            logger.warning(
                f"[gen:{stage}] {request_id} attempt {attempt} {last_error_type} "
                f"retryable={retryable}"
            )
            if attempt < self._max_retries and retryable:
                await asyncio.sleep(retry_delay)
                retry_delay = retry_delay * 2 * (1 + random.uniform(0, 0.3))
                continue
            break

        result = APICallResult(
            request_id=request_id,
            stage=stage,
            status=last_status,
            error_type=last_error_type,
            content=None,
            prompt_tokens=0,
            completion_tokens=0,
            token_source="estimate",
            latency_ms=int((time.monotonic() - started) * 1000),
            retry_count=len(attempts) - 1,
            estimated_input_tokens=estimated_input,
            attempts=attempts,
        )
        logger.error(
            f"[gen:{stage}] {request_id} FAILED after {len(attempts)} attempts: {last_error_type}"
        )
        self._emit(result)
        return result

    def _emit(self, result: APICallResult) -> None:
        if self._stats is not None:
            self._stats(
                {
                    "type": "api",
                    "request_id": result.request_id,
                    "stage": result.stage,
                    "status": result.status,
                    "error_type": result.error_type,
                    "latency_ms": result.latency_ms,
                    "retry_count": result.retry_count,
                    "model": self.model,
                    "host": self.host,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "token_source": result.token_source,
                    "estimated_input_tokens": result.estimated_input_tokens,
                    "attempts": result.attempts,
                }
            )
        if self._budget_check is not None:
            self._budget_check()
