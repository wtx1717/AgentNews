"""Model-facing tool for generating one persisted news digest."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_DIGEST_TEXT_LENGTH = 256


class GenerateDigestInput(BaseModel):
    """Strict arguments for one topic/keyword digest request."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=1,
        max_length=MAX_DIGEST_TEXT_LENGTH,
        strict=True,
        description="The news topic for this one digest.",
    )
    keyword: str = Field(
        min_length=1,
        max_length=MAX_DIGEST_TEXT_LENGTH,
        strict=True,
        description="The keyword to focus on for this one digest.",
    )

    @field_validator("topic", "keyword")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """拒绝纯空白参数，并统一去除首尾空格。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


def create_digest_tool(
    handler: Callable[[str, str], Awaitable[str]],
) -> StructuredTool:
    """Create the model-facing one-digest tool around an application handler."""

    async def run_generate_digest(topic: str, keyword: str) -> str:
        return await handler(topic, keyword)

    return StructuredTool.from_function(
        coroutine=run_generate_digest,
        name="generate_digest",
        description=(
            "Generate and persist exactly one news digest for the supplied topic and keyword. "
            "The tool performs the complete search, webpage fetching, Markdown generation, "
            "source archiving, and report indexing workflow. Call it once per requested digest; "
            "when the user requests multiple different digests, decide independently how many "
            "calls are needed and use the matching topic and keyword for each call. "
            "Do not use Bash for digest generation."
        ),
        args_schema=GenerateDigestInput,
        handle_validation_error=_handle_validation_error,
    )


def _handle_validation_error(error: object) -> str:
    details = []
    errors = getattr(error, "errors", None)
    if callable(errors):
        for item in errors(include_url=False):
            details.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "message": str(item.get("msg", "invalid value")),
                }
            )
    return _json(
        {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": "日报工具参数校验失败。",
                "details": details,
            },
        }
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
