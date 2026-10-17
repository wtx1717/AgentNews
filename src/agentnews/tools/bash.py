"""Human-approved PowerShell execution for the Agent.

The tool deliberately does not use ``shell=True``.  PowerShell is the
explicitly selected command interpreter, and the approval callback is the
only way a model-generated command can reach the executor.
"""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
import json
import locale
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Awaitable, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, field_validator


logger = logging.getLogger(__name__)

DEFAULT_BASH_TIMEOUT_SECONDS = 60.0
DEFAULT_BASH_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_BASH_COMMAND_LENGTH = 32_768
MAX_BASH_EXPLANATION_LENGTH = 2_000
POWERSHELL_UNAVAILABLE_MESSAGE = (
    "未找到可用的 PowerShell。请安装 PowerShell 7 (pwsh)，"
    "或确认系统自带的 Windows PowerShell 5.1 (powershell.exe) 可用。"
)
POWERSHELL_START_FAILED_MESSAGE = "PowerShell 进程启动失败："


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
class PowerShellInterpreter:
    """One resolved PowerShell command interpreter."""

    executable: str
    label: str
    is_core: bool

    @property
    def display(self) -> str:
        return f"{self.label} ({self.executable})"


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
    message: str | None = None
    interpreter: str | None = None

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
        if self.message is not None:
            result["message"] = self.message
        if self.interpreter is not None:
            result["interpreter"] = self.interpreter
        return result


def resolve_powershell(executable: str = "pwsh") -> PowerShellInterpreter | None:
    """Resolve the PowerShell entry point, preferring PowerShell 7.

    A bare name (the default ``pwsh``) is only a preference: when it cannot be
    found the resolver falls back to the Windows-provided Windows PowerShell
    5.1 entry point.  An explicit path is used exactly as given.
    """
    for candidate in _interpreter_candidates(executable):
        resolved = _locate_executable(candidate)
        if resolved is not None:
            return _describe_interpreter(resolved)
    return None


class BashExecutor:
    """Execute one command with a resolved PowerShell process boundary."""

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
        self._fallback_warned = False

    def resolve_interpreter(self) -> PowerShellInterpreter | None:
        """Return the interpreter this executor would launch, if any."""
        return resolve_powershell(self.executable)

    async def execute(self, command: str) -> BashExecutionResult:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must not be empty")
        try:
            return await self._execute_on_current_loop(command)
        except NotImplementedError:
            if self.process_factory is not None:
                raise
            # uvicorn --reload (or any SelectorEventLoop on Windows) cannot
            # create asyncio subprocesses, so run the command on a dedicated
            # Proactor loop instead of failing the approved call.
            logger.info(
                "bash: the running event loop cannot spawn subprocesses; "
                "retrying on a dedicated Proactor loop"
            )
            return await asyncio.to_thread(self._execute_on_dedicated_loop, command)

    def _execute_on_dedicated_loop(self, command: str) -> BashExecutionResult:
        with asyncio.Runner(loop_factory=_new_subprocess_loop) as runner:
            return runner.run(self._execute_on_current_loop(command))

    async def _execute_on_current_loop(self, command: str) -> BashExecutionResult:
        if len(command) > MAX_BASH_COMMAND_LENGTH:
            return _failure_result(
                command,
                "command_too_long",
                "命令过长，未执行。",
            )
        if not self.working_directory.is_dir():
            return _failure_result(
                command,
                "working_directory_unavailable",
                "工作目录不可用，未执行命令。",
            )

        started = time.perf_counter()
        if self.process_factory is None:
            interpreter = self.resolve_interpreter()
            if interpreter is None:
                logger.warning("bash interpreter unavailable: pwsh and powershell not found")
                return _failure_result(
                    command,
                    "powershell_unavailable",
                    POWERSHELL_UNAVAILABLE_MESSAGE,
                    duration_ms=_elapsed_ms(started),
                )
            launch_executable = interpreter.executable
            interpreter_display: str | None = interpreter.display
            if not interpreter.is_core and not self._fallback_warned:
                logger.warning("bash uses fallback interpreter %s", interpreter_display)
                self._fallback_warned = True
        else:
            launch_executable = self.executable
            interpreter_display = self.executable

        try:
            factory = self.process_factory or asyncio.create_subprocess_exec
            process = await factory(
                launch_executable,
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
        except FileNotFoundError:
            logger.warning("bash interpreter disappeared: %s", launch_executable)
            return _failure_result(
                command,
                "powershell_unavailable",
                POWERSHELL_UNAVAILABLE_MESSAGE,
                duration_ms=_elapsed_ms(started),
                interpreter=interpreter_display,
            )
        except OSError as error:
            detail = _describe_launch_error(error)
            logger.warning("bash interpreter failed to start: %s", detail)
            return _failure_result(
                command,
                "powershell_start_failed",
                POWERSHELL_START_FAILED_MESSAGE + detail,
                duration_ms=_elapsed_ms(started),
                interpreter=interpreter_display,
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
            message=(
                f"命令执行超时（{self.timeout_seconds:g} 秒），进程已终止。"
                if timed_out
                else None
            ),
            interpreter=interpreter_display,
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
        return (
            _decode_output(b"".join(self.parts["stdout"])),
            _decode_output(b"".join(self.parts["stderr"])),
        )


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
            "Run a PowerShell command when the dedicated tools cannot complete the job. "
            "Use it as the general fallback for tasks such as downloading files or images "
            "from the web (`Invoke-WebRequest` or `curl`), unpacking or converting files, "
            "running CLIs such as `python`, `node`, `tar` or `ffmpeg`, inspecting the "
            "environment, or combining several steps in one command. The working directory "
            "is the AgentNews project root, so prefer relative paths such as "
            "`data/downloads/photo.jpg`. PowerShell 7 (pwsh) is preferred; Windows "
            "PowerShell 5.1 is used when pwsh is not installed. The user must approve every "
            "call before it executes; include a concise, user-facing explanation of the "
            "command's purpose. For report operations, prefer these built-in actions "
            "instead of raw Remove-Item: "
            "`agentnews reports list`, `agentnews reports read --id <positive integer>`, "
            "`agentnews reports search --keyword <text>`, "
            "`agentnews reports delete --id <positive integer>`, and "
            "`agentnews temp cleanup`. The report delete action also removes the "
            "database record and associated source archives through ReportService. "
            "Report generation is not a Bash action; do not invent or run "
            "`agentnews reports generate`."
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


def _failure_result(
    command: str,
    error_code: str,
    message: str,
    *,
    duration_ms: int = 0,
    interpreter: str | None = None,
) -> BashExecutionResult:
    return BashExecutionResult(
        ok=False,
        command=command,
        stdout="",
        stderr="",
        exit_code=None,
        timed_out=False,
        truncated=False,
        duration_ms=duration_ms,
        error_code=error_code,
        message=message,
        interpreter=interpreter,
    )


def _new_subprocess_loop() -> asyncio.AbstractEventLoop:
    """Create an event loop that supports asyncio subprocesses."""
    if os.name == "nt":
        from asyncio import windows_events

        return windows_events.ProactorEventLoop()
    return asyncio.new_event_loop()


def _describe_launch_error(error: OSError) -> str:
    detail = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    return detail[:200]


def _decode_output(data: bytes) -> str:
    """Decode captured bytes that may use the console code page instead of UTF-8."""
    if not data:
        return ""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    preferred = locale.getpreferredencoding(False)
    if preferred:
        try:
            return data.decode(preferred, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


def _interpreter_candidates(executable: str) -> tuple[str, ...]:
    explicit = executable.strip() if isinstance(executable, str) else ""
    if explicit and (
        Path(explicit).is_absolute() or "/" in explicit or "\\" in explicit
    ):
        return (explicit,)

    candidates: list[str] = [explicit or "pwsh"]
    if candidates[0].lower() not in {"pwsh", "pwsh.exe"}:
        candidates.append("pwsh")
    candidates.extend(str(path) for path in _known_pwsh_paths())
    if os.name == "nt":
        candidates.append("powershell")
        system_powershell = _windows_powershell_path()
        if system_powershell:
            candidates.append(system_powershell)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return tuple(deduplicated)


def _locate_executable(candidate: str) -> str | None:
    if not candidate:
        return None
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    path = Path(candidate).expanduser()
    if path.is_file():
        return str(path)
    return None


def _known_pwsh_paths() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    paths: list[Path] = []
    for root in (
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles"),
    ):
        if not root:
            continue
        paths.append(Path(root) / "PowerShell" / "7" / "pwsh.exe")
        paths.append(Path(root) / "PowerShell" / "7-preview" / "pwsh.exe")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        paths.append(Path(local_app_data) / "Microsoft" / "PowerShell" / "7" / "pwsh.exe")
        paths.append(Path(local_app_data) / "Microsoft" / "WindowsApps" / "pwsh.exe")
    return tuple(paths)


def _windows_powershell_path() -> str | None:
    if os.name != "nt":
        return None
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not system_root:
        return None
    path = (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    return str(path) if path.is_file() else None


def _describe_interpreter(path: str) -> PowerShellInterpreter:
    name = Path(path).name.lower()
    if name.startswith("pwsh"):
        return PowerShellInterpreter(path, "PowerShell 7", True)
    if name.startswith("powershell"):
        return PowerShellInterpreter(path, "Windows PowerShell 5.1", False)
    return PowerShellInterpreter(path, "PowerShell", False)
