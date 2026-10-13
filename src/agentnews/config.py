from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from the environment or project .env."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    deepseek_api_key: SecretStr | None = None
    deepseek_model: str | None = None
    mcp_search_server_url: AnyHttpUrl | None = None
    mcp_search_api_key: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    app_timezone: str = "Asia/Shanghai"
    report_root: Path = Path("data/reports")
    sqlite_path: Path = Path("data/agentnews.sqlite3")

    @field_validator(
        "deepseek_model",
        "deepseek_api_key",
        "mcp_search_server_url",
        "mcp_search_api_key",
        "smtp_host",
        "smtp_password",
        "smtp_user",
        "smtp_from",
        mode="before",
    )
    @classmethod
    def empty_strings_are_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("APP_TIMEZONE must be a valid IANA timezone") from None
        return value

    def require_deepseek(self) -> tuple[SecretStr, str]:
        missing = []
        if self.deepseek_api_key is None:
            missing.append("DEEPSEEK_API_KEY")
        if self.deepseek_model is None:
            missing.append("DEEPSEEK_MODEL")
        self._raise_if_missing("DeepSeek", missing)
        assert self.deepseek_api_key is not None and self.deepseek_model is not None
        return self.deepseek_api_key, self.deepseek_model

    def require_search(self) -> str:
        self._raise_if_missing(
            "MCP search",
            [] if self.mcp_search_server_url is not None else ["MCP_SEARCH_SERVER_URL"],
        )
        assert self.mcp_search_server_url is not None
        return str(self.mcp_search_server_url)

    def require_smtp(self) -> tuple[str, int, str, SecretStr, str]:
        fields = (
            ("SMTP_HOST", self.smtp_host),
            ("SMTP_USER", self.smtp_user),
            ("SMTP_PASSWORD", self.smtp_password),
            ("SMTP_FROM", self.smtp_from),
        )
        self._raise_if_missing("SMTP", [name for name, value in fields if value is None])
        assert self.smtp_host is not None
        assert self.smtp_user is not None
        assert self.smtp_password is not None
        assert self.smtp_from is not None
        return (
            self.smtp_host,
            self.smtp_port,
            self.smtp_user,
            self.smtp_password,
            self.smtp_from,
        )

    @staticmethod
    def _raise_if_missing(service: str, missing: list[str]) -> None:
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"{service} configuration is incomplete; set: {names}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
