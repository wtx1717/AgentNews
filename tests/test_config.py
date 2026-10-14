import pytest
from pydantic import ValidationError

from agentnews.config import Settings


def test_local_defaults_load_without_service_credentials():
    settings = Settings(_env_file=None)

    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.report_root.as_posix() == "data/reports"
    assert settings.sqlite_path.as_posix() == "data/agentnews.sqlite3"
    assert settings.smtp_port == 587
    assert settings.deepseek_api_key is None
    assert settings.deepseek_timeout == 30
    assert settings.deepseek_max_retries == 2
    assert settings.mcp_search_timeout == 20


def test_environment_values_override_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_TIMEZONE=UTC\nSMTP_PORT=2525\n", encoding="utf-8")
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "45")
    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "4")

    settings = Settings(_env_file=dotenv)

    assert settings.app_timezone == "Asia/Tokyo"
    assert settings.smtp_port == 2525
    assert settings.deepseek_timeout == 45
    assert settings.deepseek_max_retries == 4


@pytest.mark.parametrize(
    "values",
    [
        {"deepseek_timeout": 0},
        {"deepseek_timeout": -1},
        {"deepseek_max_retries": -1},
        {"deepseek_max_retries": 11},
    ],
)
def test_deepseek_timeout_and_retry_configuration_is_bounded(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_blank_secrets_are_treated_as_unset():
    settings = Settings(
        _env_file=None,
        deepseek_api_key=" ",
        mcp_search_api_key="",
        smtp_password="",
    )

    assert settings.deepseek_api_key is None
    assert settings.mcp_search_api_key is None
    assert settings.smtp_password is None


def test_require_deepseek_reports_missing_names_only():
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY, DEEPSEEK_MODEL"):
        settings.require_deepseek()


def test_require_search_reports_missing_url_and_api_key():
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="MCP_SEARCH_SERVER_URL, MCP_SEARCH_API_KEY"):
        settings.require_search()


def test_require_search_reports_missing_api_key_when_url_is_configured():
    settings = Settings(
        _env_file=None,
        mcp_search_server_url="https://mcp.tavily.com/mcp",
    )

    with pytest.raises(ValueError, match="MCP_SEARCH_API_KEY"):
        settings.require_search()


def test_require_smtp_reports_missing_fields():
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="SMTP_HOST.*SMTP_USER.*SMTP_PASSWORD.*SMTP_FROM"):
        settings.require_smtp()


def test_secrets_are_masked_in_repr_and_validation_errors():
    secret = "do-not-print-this-secret"
    settings = Settings(_env_file=None, deepseek_api_key=secret)

    assert secret not in repr(settings)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, smtp_port="not-a-port", smtp_password=secret)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize("port", [0, 65536, "not-a-port"])
def test_invalid_smtp_port_is_rejected(port):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, smtp_port=port)


def test_invalid_timezone_is_rejected_without_echoing_value():
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        Settings(_env_file=None, app_timezone="Not/A_Real_Zone")


def test_invalid_search_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_search_server_url="not-a-url")


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_search_timeout_is_rejected(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_search_timeout=timeout)


def test_service_configurations_return_validated_values():
    settings = Settings(
        _env_file=None,
        deepseek_api_key="deepseek-secret",
        deepseek_model="deepseek-chat",
        mcp_search_server_url="https://search.example/mcp",
        mcp_search_api_key="search-secret",
        smtp_host="smtp.example.com",
        smtp_user="user",
        smtp_password="smtp-secret",
        smtp_from="agent@example.com",
    )

    key, model = settings.require_deepseek()
    assert key.get_secret_value() == "deepseek-secret"
    assert model == "deepseek-chat"
    search_url, search_key = settings.require_search()
    assert search_url == "https://search.example/mcp"
    assert search_key.get_secret_value() == "search-secret"
    host, port, user, password, sender = settings.require_smtp()
    assert (host, port, user, sender) == ("smtp.example.com", 587, "user", "agent@example.com")
    assert password.get_secret_value() == "smtp-secret"
