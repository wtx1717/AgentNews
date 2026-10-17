from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentnews.tools import bash as bash_module
from agentnews.tools.bash import (
    BashExecutor,
    BashInput,
    PowerShellInterpreter,
    create_bash_tool,
    resolve_powershell,
)


WINDOWS_POWERSHELL = r"C:\windows\System32\WindowsPowerShell\v1.0\powershell.EXE"
POWERSHELL_SEVEN = r"C:\Program Files\PowerShell\7\pwsh.exe"

REAL_INTERPRETER = resolve_powershell()


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode = returncode
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self):
        self._done.set()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def _isolate_interpreter_probes(monkeypatch, which):
    """Keep resolver tests independent of the machine running the suite."""

    monkeypatch.setattr(bash_module, "_known_pwsh_paths", lambda: ())
    monkeypatch.setattr(bash_module, "_windows_powershell_path", lambda: None)
    monkeypatch.setattr(bash_module.shutil, "which", which)


def test_bash_schema_is_strict_and_explanation_is_required():
    tool = create_bash_tool(lambda _command, _explanation: asyncio.sleep(0, result='{"ok":true}'))

    missing = json.loads(tool.invoke({"command": "Get-Date"}))
    assert missing["error"]["code"] == "invalid_arguments"

    extra = json.loads(
        tool.invoke({"command": "Get-Date", "explanation": "读取时间", "extra": True})
    )
    assert extra["error"]["code"] == "invalid_arguments"


def test_bash_description_prioritizes_report_actions():
    tool = create_bash_tool(lambda _command, _explanation: asyncio.sleep(0, result='{"ok":true}'))

    assert "agentnews reports delete --id" in tool.description
    assert "ReportService" in tool.description
    assert "Report generation is not a Bash action" in tool.description
    assert "Windows PowerShell 5.1" in tool.description


def test_resolver_prefers_powershell_seven(monkeypatch):
    resolved = {
        "pwsh": POWERSHELL_SEVEN,
        "powershell": WINDOWS_POWERSHELL,
    }
    _isolate_interpreter_probes(monkeypatch, resolved.get)

    interpreter = resolve_powershell()

    assert interpreter is not None
    assert interpreter.executable == POWERSHELL_SEVEN
    assert interpreter.label == "PowerShell 7"
    assert interpreter.is_core is True


def test_resolver_falls_back_to_windows_powershell(monkeypatch):
    def which(name: str) -> str | None:
        return WINDOWS_POWERSHELL if name == "powershell" else None

    _isolate_interpreter_probes(monkeypatch, which)

    interpreter = resolve_powershell()

    assert interpreter is not None
    assert interpreter.executable == WINDOWS_POWERSHELL
    assert interpreter.label == "Windows PowerShell 5.1"
    assert interpreter.is_core is False
    assert interpreter.display == f"Windows PowerShell 5.1 ({WINDOWS_POWERSHELL})"


def test_resolver_returns_none_when_no_interpreter_exists(monkeypatch):
    _isolate_interpreter_probes(monkeypatch, lambda _name: None)

    assert resolve_powershell() is None


def test_resolver_uses_an_explicit_existing_path(tmp_path):
    executable = tmp_path / "pwsh.exe"
    executable.write_text("", encoding="utf-8")

    interpreter = resolve_powershell(str(executable))

    assert interpreter is not None
    assert Path(interpreter.executable) == executable
    assert interpreter.label == "PowerShell 7"


def test_resolver_does_not_fall_back_from_an_explicit_path(tmp_path, monkeypatch):
    def which(name: str) -> str | None:
        return WINDOWS_POWERSHELL if name == "powershell" else None

    _isolate_interpreter_probes(monkeypatch, which)

    assert resolve_powershell(str(tmp_path / "missing" / "pwsh.exe")) is None


def test_executor_launches_the_resolved_fallback_interpreter(tmp_path, monkeypatch):
    calls = {}

    async def factory(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return FakeProcess(b"ok")

    monkeypatch.setattr(
        bash_module,
        "resolve_powershell",
        lambda executable="pwsh": PowerShellInterpreter(
            WINDOWS_POWERSHELL, "Windows PowerShell 5.1", False
        ),
    )
    monkeypatch.setattr(bash_module.asyncio, "create_subprocess_exec", factory)

    result = asyncio.run(BashExecutor(tmp_path).execute("Write-Output ok"))

    assert calls["args"][0] == WINDOWS_POWERSHELL
    assert calls["args"][1:5] == (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    )
    assert calls["args"][5] == "Write-Output ok"
    assert result.ok is True
    assert result.interpreter == f"Windows PowerShell 5.1 ({WINDOWS_POWERSHELL})"


def test_executor_reports_unavailable_interpreter_with_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(bash_module, "resolve_powershell", lambda executable="pwsh": None)

    result = asyncio.run(BashExecutor(tmp_path).execute("Get-Date"))

    assert result.ok is False
    assert result.error_code == "powershell_unavailable"
    assert result.message is not None and "PowerShell" in result.message
    assert result.interpreter is None
    assert result.to_dict()["message"] == result.message
    assert "interpreter" not in result.to_dict()


def test_executor_reports_a_start_failure(tmp_path):
    async def factory(*_args, **_kwargs):
        raise PermissionError(13, "denied")

    result = asyncio.run(
        BashExecutor(tmp_path, process_factory=factory).execute("Get-Date")
    )

    assert result.ok is False
    assert result.error_code == "powershell_start_failed"
    assert result.message is not None and "PermissionError" in result.message


def test_executor_reports_a_vanished_interpreter_binary(tmp_path):
    async def factory(*_args, **_kwargs):
        raise FileNotFoundError()

    result = asyncio.run(
        BashExecutor(tmp_path, process_factory=factory).execute("Get-Date")
    )

    assert result.ok is False
    assert result.error_code == "powershell_unavailable"


def test_executor_uses_fixed_process_boundary_and_bounded_output(tmp_path):
    calls = {}

    async def factory(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return FakeProcess(b"a" * 20, b"stderr")

    result = asyncio.run(
        BashExecutor(
            tmp_path,
            process_factory=factory,
            output_limit_bytes=10,
        ).execute("Write-Output test")
    )

    assert result.ok is True
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8") + result.stderr.encode("utf-8")) <= 10
    assert calls["args"][:5] == (
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    )
    assert calls["args"][5] == "Write-Output test"
    assert calls["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert calls["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert "env" in calls["kwargs"]


def test_executor_reports_nonzero_exit_and_stderr(tmp_path):
    async def factory(*_args, **_kwargs):
        return FakeProcess(b"out", b"failure", returncode=3)

    result = asyncio.run(BashExecutor(tmp_path, process_factory=factory).execute("throw"))

    assert result.ok is False
    assert result.exit_code == 3
    assert result.stderr == "failure"


def test_executor_decodes_non_utf8_console_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bash_module.locale, "getpreferredencoding", lambda do_setlocale=True: "cp936"
    )
    captured = "中文测试\r\n".encode("cp936")

    async def factory(*_args, **_kwargs):
        return FakeProcess(captured)

    result = asyncio.run(
        BashExecutor(tmp_path, process_factory=factory).execute("Write-Output '测试'")
    )

    assert result.stdout == "中文测试\r\n"


def test_executor_keeps_utf8_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bash_module.locale, "getpreferredencoding", lambda do_setlocale=True: "cp936"
    )
    captured = "中文测试\r\n".encode("utf-8")

    async def factory(*_args, **_kwargs):
        return FakeProcess(captured)

    result = asyncio.run(
        BashExecutor(tmp_path, process_factory=factory).execute("Write-Output '测试'")
    )

    assert result.stdout == "中文测试\r\n"


def test_executor_timeout_kills_process(tmp_path):
    class HangingProcess(FakeProcess):
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.killed = False

        async def wait(self):
            while not self.killed:
                await asyncio.sleep(0.01)
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    async def scenario():
        process = HangingProcess()

        async def factory(*_args, **_kwargs):
            return process

        result = await BashExecutor(
            tmp_path, process_factory=factory, timeout_seconds=0.02
        ).execute("hang")
        return result, process

    result, process = asyncio.run(scenario())

    assert result.timed_out is True
    assert result.error_code == "timeout"
    assert result.message is not None and "超时" in result.message
    assert process.killed is True


@pytest.mark.skipif(REAL_INTERPRETER is None, reason="no PowerShell interpreter is available")
def test_executor_runs_the_resolved_interpreter(tmp_path):
    result = asyncio.run(
        BashExecutor(tmp_path, timeout_seconds=30.0).execute(
            "Write-Output 'agentnews-bash-ok'"
        )
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert "agentnews-bash-ok" in result.stdout
    assert result.interpreter == REAL_INTERPRETER.display


@pytest.mark.skipif(
    REAL_INTERPRETER is None or not hasattr(asyncio, "SelectorEventLoop"),
    reason="needs a real interpreter and asyncio.SelectorEventLoop",
)
def test_executor_runs_on_a_selector_event_loop(tmp_path):
    """uvicorn --reload on Windows serves requests from a SelectorEventLoop."""

    result = asyncio.run(
        BashExecutor(tmp_path, timeout_seconds=30.0).execute(
            "Write-Output 'selector-ok'"
        ),
        loop_factory=asyncio.SelectorEventLoop,
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert "selector-ok" in result.stdout
