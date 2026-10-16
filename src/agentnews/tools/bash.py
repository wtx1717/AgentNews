"""Human-approved PowerShell execution for the Agent.

The tool deliberately does not use ``shell=True``.  PowerShell is the
explicitly selected command interpreter, and the approval callback is the
only way a model-generated command can reach the executor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Awaitable, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_BASH_TIMEOUT_SECONDS = 60.0
DEFAULT_BASH_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_BASH_COMMAND_LENGTH = 32_768
MAX_BASH_EXPLANATION_LENGTH = 2_000


class BashInput(BaseModel):
    """Strict model-facing schema for one approved shell request."""

    model_config = ConfigDict(extra="forbid")
    command: str = Field(
        min_length=1,
        max_length=MAX_BASH_COMMAND_LENGTH,
        strict=True,
        description="PowerShell command to execute after the user approves it.",
    )
    explanation: str = Field(
        min_length=1,
        max_length=MAX_BASH_EXPLANATION_LENGTH,
        strict=True,
        description="Short user-facing explanation of why this command is needed.",
    )

    @field_validator("command", "explanation")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


@dataclass(frozen=True, slots=True)
class BashExecutionResult:
    """Bounded result from one PowerShell process."""

    ok: bool
    command: str
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "ok": self.ok,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


class BashExecutor:
    """Execute one command with a fixed PowerShell 7 process boundary."""

    def __init__(
        self,
        working_directory: str | os.PathLike[str],
        *,
        executable: str = "pwsh",
        timeout_seconds: float = DEFAULT_BASH_TIMEOUT_SECONDS,
        output_limit_bytes: int = DEFAULT_BASH_OUTPUT_LIMIT_BYTES,
        process_factory: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.working_directory = Path(working_directory).resolve()
        self.executable = executable
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_limit_bytes < 1:
            raise ValueError("output_limit_bytes must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.output_limit_bytes = int(output_limit_bytes)
        self.process_factory = process_factory

    async def execute(self, command: str) -> BashExecutionResult:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must not be empty")
        if len(command) > MAX_BASH_COMMAND_LENGTH:
            return BashExecutionResult(
                ok=False,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                timed_out=False,
                truncated=False,
                duration_ms=0,
                error_code="command_too_long",
            )
        if not self.working_directory.is_dir():
            return BashExecutionResult(
                ok=False,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                timed_out=False,
                truncated=False,
                duration_ms=0,
                error_code="working_directory_unavailable",
            )
        if self.process_factory is None and shutil.which(self.executable) is None:
            return BashExecutionResult(
                ok=False,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                timed_out=False,
                truncated=False,
                duration_ms=0,
                error_code="powershell_unavailable",
            )

        started = time.perf_counter()
        try:
            factory = self.process_factory or asyncio.create_subprocess_exec
            process = await factory(
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                cwd=str(self.working_directory),
                env=os.environ.copy(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            return BashExecutionResult(
                ok=False,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                timed_out=False,
                truncated=False,
                duration_ms=_elapsed_ms(started),
                error_code="powershell_unavailable",
            )

        capture = _OutputCapture(self.output_limit_bytes)
        stdout_task = asyncio.create_task(capture.read(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(capture.read(process.stderr, "stderr"))
        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, process.wait()),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        exit_code = None if timed_out else process.returncode
        stdout, stderr = capture.text()
        return BashExecutionResult(
            ok=not timed_out and exit_code == 0,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=capture.truncated,
            duration_ms=_elapsed_ms(started),
            error_code="timeout" if timed_out else None,
        )


class _OutputCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.parts: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        self.truncated = False
        self._lock = asyncio.Lock()

    async def read(self, stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            async with self._lock:
                allowed = max(0, min(len(chunk), self.limit - self.total))
                if allowed:
                    self.parts[name].append(chunk[:allowed])
                    self.total += allowed
                if allowed < len(chunk):
                    self.truncated = True

    def text(self) -> tuple[str, str]:
        return tuple(
            b"".join(self.parts[name]).decode("utf-8", errors="replace")
            for name in ("stdout", "stderr")
        )  # type: ignore[return-value]


def create_bash_tool(
    approval_handler: Callable[[str, str], Awaitable[str]],
) -> StructuredTool:
    """Create the model-facing Bash tool around an approval callback."""

    async def run_bash(command: str, explanation: str) -> str:
        return await approval_handler(command, explanation)

    return StructuredTool.from_function(
        coroutine=run_bash,
        name="bash",
        description=(
            "Request a PowerShell 7 command when the other tools cannot complete the task. "
            "The user must approve every call before it executes. Include a concise, "
            "user-facing explanation of the command's purpose."
        ),
        args_schema=BashInput,
        handle_validation_error=_handle_validation_error,
    )


def _handle_validation_error(error: Any) -> str:
    details = []
    for item in error.errors(include_url=False):
        details.append(
            {
                "field": ".".join(str(part) for part in item.get("loc", ())),
                "message": str(item.get("msg", "invalid value")),
            }
        )
    return _json({"ok": False, "error": {"code": "invalid_arguments", "details": details}})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
