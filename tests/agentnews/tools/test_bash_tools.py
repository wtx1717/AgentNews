from __future__ import annotations

import asyncio
import json

import pytest

from agentnews.tools.bash import BashExecutor, BashInput, create_bash_tool


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


def test_bash_schema_is_strict_and_explanation_is_required():
    tool = create_bash_tool(lambda _command, _explanation: asyncio.sleep(0, result='{"ok":true}'))

    missing = json.loads(tool.invoke({"command": "Get-Date"}))
    assert missing["error"]["code"] == "invalid_arguments"

    extra = json.loads(
        tool.invoke({"command": "Get-Date", "explanation": "读取时间", "extra": True})
    )
    assert extra["error"]["code"] == "invalid_arguments"


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
    assert process.killed is True
