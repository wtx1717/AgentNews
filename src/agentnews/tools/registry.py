"""LangChain schemas and registration for the four managed file tools."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .file_tools import FileToolError, LocalFileTools
from .path_security import ManagedPathResolver


class _StrictToolInput(BaseModel):
    """工具入参基类：禁止未声明的额外字段。"""
    model_config = ConfigDict(extra="forbid")


class ListDirInput(_StrictToolInput):
    """Arguments for listing one directory below the managed data root."""

    path: str = Field(
        min_length=1,
        pattern=r"^[^\x00]+$",
        strict=True,
        description=(
            "Directory path beneath the managed data root. Relative paths are "
            "preferred; '.' means the root. Paths outside the root are rejected."
        )
    )


class ReadFileInput(_StrictToolInput):
    """Arguments for reading one bounded UTF-8 text file."""

    path: str = Field(
        min_length=1,
        pattern=r"^[^\x00]+$",
        strict=True,
        description=(
            "Text file path beneath the managed data root. Only regular UTF-8 "
            "text files within the configured size limit can be read."
        )
    )


class SearchContentInput(_StrictToolInput):
    """Arguments for searching bounded text files beneath a directory."""

    keyword: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00]+$",
        strict=True,
        description="Non-empty keyword to find in UTF-8 text files (maximum 256 characters).",
    )
    dir: str = Field(
        min_length=1,
        pattern=r"^[^\x00]+$",
        strict=True,
        description=(
            "Directory beneath the managed data root to search. Search stays "
            "inside this root; traversal is recursive by default."
        )
    )
    recursive: bool = Field(
        default=True,
        strict=True,
        description="Whether to search nested directories; defaults to true.",
    )
    case_sensitive: bool = Field(
        default=True,
        strict=True,
        description="Whether keyword matching is case-sensitive; defaults to true.",
    )


class WriteFileInput(_StrictToolInput):
    """Arguments for creating a bounded UTF-8 text file."""

    path: str = Field(
        min_length=1,
        pattern=r"^[^\x00]+$",
        strict=True,
        description=(
            "New file path beneath the managed data root. Its parent directory "
            "must already exist."
        )
    )
    content: str = Field(
        strict=True,
        description="UTF-8 text content to write (maximum 1 MiB).",
    )
    overwrite: bool = Field(
        default=False,
        strict=True,
        description=(
            "Explicit confirmation to replace an existing file. Set true only "
            "when the user directly authorized overwriting that file; otherwise "
            "leave false. Existing files are never overwritten by default."
        ),
    )


def create_local_file_tools(
    resolver: ManagedPathResolver | None = None,
) -> list[StructuredTool]:
    """Create the four supported LangChain tools.

    The returned registry contains only list_dir, read_file, search_content,
    and write_file. No shell or command-execution tool is registered or called.
    Each tool returns a JSON envelope so success and failure results have a
    stable shape for an Agent to consume.
    """
    local_tools = LocalFileTools(resolver)

    def run_list_dir(path: str) -> str:
        return _run_tool(lambda: local_tools.list_dir(path).to_dict())

    def run_read_file(path: str) -> str:
        return _run_tool(lambda: local_tools.read_file(path).to_dict())

    def run_search_content(
        keyword: str,
        dir: str,
        recursive: bool = True,
        case_sensitive: bool = True,
    ) -> str:
        return _run_tool(
            lambda: local_tools.search_content(
                keyword,
                dir=dir,
                recursive=recursive,
                case_sensitive=case_sensitive,
            ).to_dict()
        )

    def run_write_file(
        path: str,
        content: str,
        overwrite: bool = False,
    ) -> str:
        return _run_tool(
            lambda: local_tools.write_file(
                path,
                content,
                overwrite=overwrite,
            ).to_dict()
        )

    return [
        StructuredTool.from_function(
            func=run_list_dir,
            name="list_dir",
            description=(
                "List immediate files and directories beneath the managed data root. "
                "Returns stable JSON metadata; protected and out-of-root paths are rejected."
            ),
            args_schema=ListDirInput,
            handle_validation_error=_handle_validation_error,
        ),
        StructuredTool.from_function(
            func=run_read_file,
            name="read_file",
            description=(
                "Read one bounded UTF-8 text file beneath the managed data root. "
                "Returns its content and byte size; binary, oversized and protected files fail safely."
            ),
            args_schema=ReadFileInput,
            handle_validation_error=_handle_validation_error,
        ),
        StructuredTool.from_function(
            func=run_search_content,
            name="search_content",
            description=(
                "Search UTF-8 text files beneath a managed directory and return matching "
                "snippets with relative paths and line numbers. Recursion is bounded."
            ),
            args_schema=SearchContentInput,
            handle_validation_error=_handle_validation_error,
        ),
        StructuredTool.from_function(
            func=run_write_file,
            name="write_file",
            description=(
                "Create a bounded UTF-8 text file beneath the managed data root. "
                "Existing files require explicit user authorization via overwrite=true."
            ),
            args_schema=WriteFileInput,
            handle_validation_error=_handle_validation_error,
        ),
    ]


def _run_tool(operation: Any) -> str:
    """执行文件操作，并把结果或异常统一包装成 JSON 信封。"""
    try:
        result = operation()
    except FileToolError as error:
        return _json_envelope(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "path": error.path,
                    "details": [],
                },
            }
        )
    # 兜底异常不向 Agent 暴露内部细节。
    except Exception:
        return _json_envelope(
            {
                "ok": False,
                "error": {
                    "code": "tool_error",
                    "message": "工具执行失败，请检查参数或稍后重试。",
                    "path": None,
                    "details": [],
                },
            }
        )
    return _json_envelope({"ok": True, "result": result})


def _handle_validation_error(error: ValidationError) -> str:
    """把参数校验失败转换成带字段明细的错误信封。"""
    details = []
    for item in error.errors(include_url=False):
        field = ".".join(str(part) for part in item.get("loc", ()))
        details.append(
            {
                "field": field,
                "message": str(item.get("msg", "invalid value")),
            }
        )
    return _json_envelope(
        {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": "工具参数校验失败。",
                "path": None,
                "details": details,
            },
        }
    )


def _json_envelope(value: dict[str, Any]) -> str:
    """序列化为键有序的紧凑 JSON，保证输出稳定可比。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
