"""Model-facing tool for mailing a previously persisted news report."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field


class SendReportEmailInput(BaseModel):
    """Strict arguments that never accept a recipient, SMTP setting, or body."""

    model_config = ConfigDict(extra="forbid")

    report_id: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        description="Persisted report ID. Omit only when the user asks for the latest report.",
    )
    resend: bool = Field(
        default=False,
        strict=True,
        description="Set true only when the user explicitly asks to resend a previously sent or failed report.",
    )


def create_send_report_email_tool(
    handler: Callable[[int | None, bool], Awaitable[str]],
) -> StructuredTool:
    """Create the controlled natural-language report email tool."""

    async def run_send_report_email(
        report_id: int | None = None,
        resend: bool = False,
    ) -> str:
        return await handler(report_id, resend)

    return StructuredTool.from_function(
        coroutine=run_send_report_email,
        name="send_report_email",
        description=(
            "Send an already generated and persisted AgentNews report to the saved user email. "
            "Call this tool only when the user clearly and directly asks to send a report email; "
            "mentioning email, discussing email, quoted requests, or ambiguous wording is not consent. "
            "Never invent or pass report content, recipient addresses, or SMTP settings. "
            "Omit report_id only for an explicit request to send the latest completed report. "
            "Set resend=true only for an explicit request to resend a report that was already attempted."
        ),
        args_schema=SendReportEmailInput,
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
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": "邮件工具参数校验失败。",
                "details": details,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
