"""LangChain model configuration for the OpenAI-compatible DeepSeek API."""

from __future__ import annotations

import logging
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError

from ...config import Settings, get_settings

logger = logging.getLogger(__name__)


class DeepSeekModelError(RuntimeError):
    """Base class for sanitized DeepSeek request failures."""


class DeepSeekTimeoutError(DeepSeekModelError):
    """The DeepSeek request exceeded its configured timeout."""


class DeepSeekAuthenticationError(DeepSeekModelError):
    """DeepSeek rejected the configured credentials."""


class DeepSeekRateLimitError(DeepSeekModelError):
    """DeepSeek rejected the request due to rate limiting."""


class DeepSeekUnavailableError(DeepSeekModelError):
    """DeepSeek could not be reached or returned a server error."""


def map_deepseek_error(error: BaseException) -> DeepSeekModelError:
    """Map provider failures to stable messages that contain no request data."""
    if isinstance(error, DeepSeekModelError):
        return error
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return DeepSeekTimeoutError("DeepSeek request timed out")
    if isinstance(error, (APIConnectionError, ConnectionError)):
        return DeepSeekUnavailableError("DeepSeek service is unavailable")

    status_code = getattr(error, "status_code", None)
    if status_code in (401, 403):
        return DeepSeekAuthenticationError("DeepSeek authentication failed")
    if status_code == 429:
        return DeepSeekRateLimitError("DeepSeek rate limit exceeded")
    if isinstance(status_code, int) and status_code >= 500:
        return DeepSeekUnavailableError("DeepSeek service is unavailable")
    return DeepSeekModelError("DeepSeek request failed")


class DeepSeekLoggingCallback(BaseCallbackHandler):
    """Log request metadata only; never inspect prompts or generated content."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._started_at: dict[UUID, float] = {}
        self._lock = Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            self._started_at[run_id] = perf_counter()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        logger.info(
            "DeepSeek invocation completed model=%s duration_ms=%d",
            self.model_name,
            self._elapsed_ms(run_id),
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        mapped_error = map_deepseek_error(error)
        logger.warning(
            "DeepSeek invocation failed model=%s duration_ms=%d error_type=%s",
            self.model_name,
            self._elapsed_ms(run_id),
            type(mapped_error).__name__,
        )

    def _elapsed_ms(self, run_id: UUID) -> int:
        with self._lock:
            started = self._started_at.pop(run_id, None)
        if started is None:
            return 0
        return round((perf_counter() - started) * 1000)


def create_deepseek_model(settings: Settings | None = None) -> ChatOpenAI:
    """Build the native LangChain chat model from validated configuration."""
    settings = settings or get_settings()
    api_key, model_name = settings.require_deepseek()
    try:
        return ChatOpenAI(
            model=model_name,
            api_key=api_key.get_secret_value(),
            base_url="https://api.deepseek.com",
            timeout=settings.deepseek_timeout,
            max_retries=settings.deepseek_max_retries,
            callbacks=[DeepSeekLoggingCallback(model_name)],
        )
    except Exception as error:
        mapped_error = map_deepseek_error(error)
        logger.warning(
            "DeepSeek client initialization failed error_type=%s",
            type(mapped_error).__name__,
        )
        raise mapped_error from None
