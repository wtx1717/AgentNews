import asyncio
import logging
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda

from agentnews.config import Settings
from agentnews.deepseek import (
    DeepSeekAuthenticationError,
    DeepSeekChatAdapter,
    DeepSeekModelError,
    DeepSeekRateLimitError,
    DeepSeekTimeoutError,
    DeepSeekUnavailableError,
    create_deepseek_model,
    map_deepseek_error,
)


def _settings(**overrides):
    return Settings(
        _env_file=None,
        deepseek_api_key="test-api-key",
        deepseek_model="deepseek-chat",
        **overrides,
    )


def test_factory_initializes_openai_compatible_client_and_invokes(monkeypatch, caplog):
    calls = {}

    class MockChatOpenAI:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def invoke(self, value, config=None, **kwargs):
            calls["input"] = value
            return "mock response"

    monkeypatch.setattr("agentnews.deepseek.ChatOpenAI", MockChatOpenAI)
    caplog.set_level(logging.INFO, logger="agentnews.deepseek")

    adapter = create_deepseek_model(
        _settings(deepseek_timeout=12, deepseek_max_retries=3)
    )
    result = adapter.invoke("private prompt")

    assert result == "mock response"
    assert calls == {
        "model": "deepseek-chat",
        "api_key": "test-api-key",
        "base_url": "https://api.deepseek.com",
        "timeout": 12,
        "max_retries": 3,
        "input": "private prompt",
    }
    assert "DeepSeek invocation completed" in caplog.text
    assert "private prompt" not in caplog.text
    assert "mock response" not in caplog.text
    assert "test-api-key" not in caplog.text


def test_factory_rejects_missing_credentials_before_client_initialization(monkeypatch):
    def unexpected_client_initialization(**kwargs):
        pytest.fail("client must not be initialized without credentials")

    monkeypatch.setattr(
        "agentnews.deepseek.ChatOpenAI", unexpected_client_initialization
    )

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY, DEEPSEEK_MODEL"):
        create_deepseek_model(Settings(_env_file=None))


def test_factory_maps_initialization_error_without_leaking_details(monkeypatch):
    secret = "initialization secret"

    def broken_client(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("agentnews.deepseek.ChatOpenAI", broken_client)

    with pytest.raises(DeepSeekModelError, match="DeepSeek request failed") as exc_info:
        create_deepseek_model(_settings())

    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_timeout_is_mapped_and_failure_logs_are_redacted(caplog):
    secret = "sensitive prompt and API key"

    def timeout(_):
        raise TimeoutError(secret)

    adapter = DeepSeekChatAdapter(RunnableLambda(timeout), "deepseek-chat")
    caplog.set_level(logging.WARNING, logger="agentnews.deepseek")

    with pytest.raises(DeepSeekTimeoutError, match="request timed out"):
        adapter.invoke(secret)

    assert secret not in caplog.text
    assert "DeepSeekTimeoutError" in caplog.text


def test_adapter_preserves_tool_binding():
    class BindableModel:
        def bind_tools(self, tools, **kwargs):
            assert tools == ["mock-tool"]
            assert kwargs == {"tool_choice": "auto"}
            return RunnableLambda(lambda _: "tool-enabled response")

    adapter = DeepSeekChatAdapter(BindableModel(), "deepseek-chat")
    bound = adapter.bind_tools(["mock-tool"], tool_choice="auto")

    assert isinstance(bound, DeepSeekChatAdapter)
    assert bound.invoke("prompt") == "tool-enabled response"


def test_adapter_supports_async_model_invocation():
    async def mock_async_model(value):
        return f"async: {value}"

    adapter = DeepSeekChatAdapter(
        RunnableLambda(mock_async_model), "deepseek-chat"
    )

    assert asyncio.run(adapter.ainvoke("request")) == "async: request"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SimpleNamespace(status_code=401), DeepSeekAuthenticationError),
        (SimpleNamespace(status_code=403), DeepSeekAuthenticationError),
        (SimpleNamespace(status_code=429), DeepSeekRateLimitError),
        (SimpleNamespace(status_code=503), DeepSeekUnavailableError),
        (ConnectionError("private endpoint"), DeepSeekUnavailableError),
        (RuntimeError("private provider response"), DeepSeekModelError),
    ],
)
def test_provider_errors_map_to_safe_application_errors(error, expected):
    mapped = map_deepseek_error(error)

    assert type(mapped) is expected
    assert "private" not in str(mapped)
