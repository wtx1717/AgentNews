"""LangChain adapter for the OpenAI-compatible DeepSeek chat API."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Sequence

from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError

from .config import Settings, get_settings

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


def map_deepseek_error(error: Exception) -> DeepSeekModelError:
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


class DeepSeekChatAdapter(Runnable[Any, Any]):
    """Runnable wrapper that logs invocation metadata without message content."""

    def __init__(self, model: Runnable[Any, Any], model_name: str) -> None:
        self._model = model
        self.model_name = model_name

    def invoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        started = perf_counter()
        try:
            result = self._model.invoke(input, config=config, **kwargs)
        except Exception as error:
            mapped_error = map_deepseek_error(error)
            logger.warning(
                "DeepSeek invocation failed model=%s duration_ms=%d error_type=%s",
                self.model_name,
                self._elapsed_ms(started),
                type(mapped_error).__name__,
            )
            raise mapped_error from None

        logger.info(
            "DeepSeek invocation completed model=%s duration_ms=%d",
            self.model_name,
            self._elapsed_ms(started),
        )
        return result

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        started = perf_counter()
        try:
            result = await self._model.ainvoke(input, config=config, **kwargs)
        except Exception as error:
            mapped_error = map_deepseek_error(error)
            logger.warning(
                "DeepSeek invocation failed model=%s duration_ms=%d error_type=%s",
                self.model_name,
                self._elapsed_ms(started),
                type(mapped_error).__name__,
            )
            raise mapped_error from None

        logger.info(
            "DeepSeek invocation completed model=%s duration_ms=%d",
            self.model_name,
            self._elapsed_ms(started),
        )
        return result

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> DeepSeekChatAdapter:
        """Bind tools on the underlying chat model and keep sanitized logging."""
        bind_tools = getattr(self._model, "bind_tools", None)
        if bind_tools is None:
            raise TypeError("The configured model does not support tool calling")
        return DeepSeekChatAdapter(bind_tools(tools, **kwargs), self.model_name)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((perf_counter() - started) * 1000)


def create_deepseek_model(settings: Settings | None = None) -> DeepSeekChatAdapter:
    """Build the DeepSeek chat model from validated environment configuration."""
    settings = settings or get_settings()
    api_key, model_name = settings.require_deepseek()
    try:
        model = ChatOpenAI(
            model=model_name,
            api_key=api_key.get_secret_value(),
            base_url="https://api.deepseek.com",
            timeout=settings.deepseek_timeout,
            max_retries=settings.deepseek_max_retries,
        )
    except Exception as error:
        mapped_error = map_deepseek_error(error)
        logger.warning(
            "DeepSeek client initialization failed error_type=%s",
            type(mapped_error).__name__,
        )
        raise mapped_error from None
    return DeepSeekChatAdapter(model, model_name)
