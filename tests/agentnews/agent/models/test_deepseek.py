import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.outputs import Generation, LLMResult
from langchain_openai import ChatOpenAI

from agentnews.config import Settings
from agentnews.agent.models.deepseek import (
    DeepSeekAuthenticationError,
    DeepSeekLoggingCallback,
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


def test_factory_returns_native_langchain_chat_model(monkeypatch):
    calls = {}

    class SpyChatOpenAI(ChatOpenAI):
        def __init__(self, **kwargs):
            calls.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(
        "agentnews.agent.models.deepseek.ChatOpenAI", SpyChatOpenAI
    )
    model = create_deepseek_model(
        _settings(deepseek_timeout=12, deepseek_max_retries=3)
    )

    assert isinstance(model, ChatOpenAI)
    assert callable(model.bind_tools)
    assert callable(model.stream)
    assert callable(model.astream)
    assert callable(model.with_structured_output)
    assert calls == {
        "model": "deepseek-chat",
        "api_key": "test-api-key",
        "base_url": "https://api.deepseek.com",
        "timeout": 12,
        "max_retries": 3,
        "callbacks": model.callbacks,
    }
    assert len(model.callbacks) == 1
    assert isinstance(model.callbacks[0], DeepSeekLoggingCallback)
    assert model.callbacks[0].model_name == "deepseek-chat"


def test_factory_rejects_missing_credentials_before_client_initialization(monkeypatch):
    def unexpected_client_initialization(**kwargs):
        pytest.fail("client must not be initialized without credentials")

    monkeypatch.setattr(
        "agentnews.agent.models.deepseek.ChatOpenAI",
        unexpected_client_initialization,
    )

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY, DEEPSEEK_MODEL"):
        create_deepseek_model(Settings(_env_file=None))


def test_factory_maps_initialization_error_without_leaking_details(monkeypatch):
    secret = "initialization secret"

    def broken_client(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        "agentnews.agent.models.deepseek.ChatOpenAI", broken_client
    )

    with pytest.raises(DeepSeekModelError, match="DeepSeek request failed") as exc_info:
        create_deepseek_model(_settings())

    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_logging_callback_redacts_success_and_error_details(caplog):
    callback = DeepSeekLoggingCallback("deepseek-chat")
    input_secret = "private prompt"
    output_secret = "private response"
    error_secret = "private provider error and test-api-key"
    caplog.set_level(
        logging.INFO, logger="agentnews.agent.models.deepseek"
    )

    run_id = uuid4()
    callback.on_chat_model_start(
        {"id": ["private-client"]}, [[input_secret]], run_id=run_id
    )
    callback.on_llm_end(
        LLMResult(generations=[[Generation(text=output_secret)]]), run_id=run_id
    )

    error_run_id = uuid4()
    callback.on_chat_model_start({}, [[input_secret]], run_id=error_run_id)
    callback.on_llm_error(RuntimeError(error_secret), run_id=error_run_id)

    assert "invocation completed" in caplog.text
    assert "invocation failed" in caplog.text
    assert "DeepSeekModelError" in caplog.text
    assert "duration_ms=" in caplog.text
    assert input_secret not in caplog.text
    assert output_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "private-client" not in caplog.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("private timeout"), DeepSeekTimeoutError),
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
