from __future__ import annotations

import asyncio
from datetime import date
import json

from agentnews.agent import AgentRunResult
from agentnews.repositories import InMemoryRepository, ReportStorage, SourceArchiveStorage
from agentnews.services.bash import BashAudit, BashService, ChatRunManager
from agentnews.services.chat import ChatResult
from agentnews.services.report import ReportService
from agentnews.tools.bash import BashExecutionResult


INTERPRETER_LABEL = (
    r"Windows PowerShell 5.1 "
    r"(C:\windows\System32\WindowsPowerShell\v1.0\powershell.EXE)"
)


def _settings(tmp_path):
    return type(
        "Settings",
        (),
        {
            "report_root": tmp_path / "reports",
            "source_archive_root": tmp_path / "source_archive",
        },
    )()


def _services(tmp_path, repository=None, *, audit=None):
    repository = repository or InMemoryRepository(report_id_factory=lambda: 1)
    settings = _settings(tmp_path)
    report_storage = ReportStorage(settings.report_root)
    source_storage = SourceArchiveStorage(settings.source_archive_root)
    report_service = ReportService(repository, report_storage, source_storage)
    service = BashService(
        repository,
        report_service,
        report_storage,
        source_storage,
        temp_root=tmp_path / "tmp",
        working_directory=tmp_path,
        executor=type(
            "Executor",
            (),
            {
                "working_directory": tmp_path.resolve(),
                "timeout_seconds": 60.0,
                "output_limit_bytes": 1024,
                "resolve_interpreter": lambda self: type(
                    "Interpreter",
                    (),
                    {"display": INTERPRETER_LABEL},
                )(),
                "execute": lambda self, command: asyncio.sleep(
                    0, result=type(
                        "Result",
                        (),
                        {
                            "ok": True,
                            "command": command,
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                            "truncated": False,
                            "duration_ms": 1,
                            "error_code": None,
                            "to_dict": lambda self: {
                                "ok": self.ok,
                                "command": self.command,
                                "stdout": self.stdout,
                                "stderr": self.stderr,
                                "exit_code": self.exit_code,
                                "timed_out": self.timed_out,
                                "truncated": self.truncated,
                                "duration_ms": self.duration_ms,
                            },
                        },
                    )(),
                ),
            },
        )(),
        audit=audit,
    )
    return repository, service, report_storage, source_storage


def _create_report(tmp_path):
    repository, service, report_storage, source_storage = _services(tmp_path)
    reservation = repository.reserve_report(date(2026, 10, 16), "日报")
    stored = report_storage.save_report(
        reservation.id, reservation.report_date, reservation.title, "# 日报\n\nAI news"
    )
    source = source_storage.save_source(
        reservation.id,
        1,
        reservation.report_date,
        "来源",
        "https://example.com",
        "example.com",
        "2026-10-16T00:00:00Z",
        None,
        "source body",
    )
    repository.add_report_source(
        reservation.id,
        "来源",
        "https://example.com",
        "example.com",
        archive_file_path=source.relative_path,
    )
    report = repository.finalize_report(reservation.id, stored.relative_path, "AI 摘要", stored.content_hash)
    return repository, service, report_storage, source_storage, report


def test_report_builtin_delete_reuses_report_service(tmp_path):
    repository, service, report_storage, source_storage, report = _create_report(tmp_path)

    result = asyncio.run(
        service.execute(
            f"agentnews reports delete --id {report.id}",
            "用户明确要求删除这份日报",
            run_id="run-1",
            session_id="session-1",
            approval_id="approval-1",
        )
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert repository.get_report(report.id) is None
    assert not (report_storage.root / report.file_path).exists()


def test_temp_cleanup_only_removes_old_files(tmp_path):
    _repository, service, _report_storage, _source_storage = _services(tmp_path)
    service.temp_root.mkdir(parents=True)
    old_file = service.temp_root / "old.tmp"
    new_file = service.temp_root / "new.tmp"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    old_file.touch()
    import os
    os.utime(old_file, (0, 0))

    result = json.loads(
        asyncio.run(
            service.execute(
                "agentnews temp cleanup",
                "清理应用临时文件",
                run_id="run-1",
                session_id="session-1",
                approval_id="approval-1",
            )
        )
    )

    assert result["result"]["count"] == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_run_manager_does_not_execute_before_approval_and_replays_rejection(tmp_path):
    repository, bash_service, _report_storage, _source_storage = _services(tmp_path)
    executions = []

    async def fake_execute(command, explanation, **kwargs):
        executions.append(command)
        return json.dumps({"ok": True})

    bash_service.execute = fake_execute

    class FakeChatService:
        async def run(self, message, session_id, *, bash_approval_handler, run_id):
            result = await bash_approval_handler("Write-Output test", "执行一个测试命令")
            assert json.loads(result)["error"]["code"] == "bash_denied"
            return ChatResult(session_id, "已根据拒绝结果继续", 1)

    manager = ChatRunManager(repository, FakeChatService(), bash_service)

    async def scenario():
        starting = asyncio.create_task(manager.start("请执行命令"))
        record = await starting
        assert record.status == "pending_approval"
        assert executions == []
        approval_id = record.approval.id
        await manager.decide(record.id, approval_id, "reject")
        for _ in range(100):
            if manager.snapshot(record.id)["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        return manager.snapshot(record.id)

    snapshot = asyncio.run(scenario())
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["answer"] == "已根据拒绝结果继续"
    assert executions == []


def test_audit_redacts_sensitive_values(tmp_path):
    events = []
    audit = BashAudit(events.append)
    _repository, service, _report_storage, _source_storage = _services(tmp_path, audit=audit)

    asyncio.run(
        service.execute(
            "Write-Output api_key=secret-value",
            "测试 password=secret-password",
            run_id="run-1",
            session_id="session-1",
            approval_id="approval-1",
        )
    )

    assert events
    assert "secret-value" not in events[0]["command"]
    assert "secret-password" not in events[0]["explanation"]
    assert events[0]["command_hash"]


def test_run_manager_preserves_expired_error_state(tmp_path):
    repository, bash_service, _report_storage, _source_storage = _services(tmp_path)

    class WaitingChatService:
        async def run(self, message, session_id, *, bash_approval_handler, run_id):
            await bash_approval_handler("Write-Output test", "执行一个测试命令")
            raise AssertionError("expired approval must stop the agent run")

    manager = ChatRunManager(
        repository,
        WaitingChatService(),
        bash_service,
        approval_timeout_seconds=0.01,
    )

    async def scenario():
        record = await manager.start("请执行命令")
        await asyncio.wait_for(record.task, timeout=1)
        return manager.snapshot(record.id)

    snapshot = asyncio.run(scenario())
    assert snapshot["status"] == "expired"
    assert snapshot["error"]["code"] == "approval_expired"
    assert snapshot["error"]["message"] == "Bash 审核已过期。"


def test_approval_snapshot_reports_the_interpreter(tmp_path):
    repository, bash_service, _report_storage, _source_storage = _services(tmp_path)

    class WaitingChatService:
        async def run(self, message, session_id, *, bash_approval_handler, run_id):
            await bash_approval_handler("Write-Output test", "执行一个测试命令")
            return ChatResult(session_id, "已拒绝", 1)

    manager = ChatRunManager(repository, WaitingChatService(), bash_service)

    async def scenario():
        record = await manager.start("请执行命令")
        snapshot = manager.snapshot(record.id)
        await manager.decide(record.id, record.approval.id, "reject")
        await asyncio.wait_for(record.task, timeout=1)
        return snapshot

    snapshot = asyncio.run(scenario())

    assert snapshot["approval"]["interpreter"] == INTERPRETER_LABEL


def test_execute_returns_an_error_object_when_the_process_fails(tmp_path):
    _repository, service, _report_storage, _source_storage = _services(tmp_path)

    class FailingExecutor:
        working_directory = tmp_path.resolve()
        timeout_seconds = 60.0
        output_limit_bytes = 1024

        async def execute(self, command):
            return BashExecutionResult(
                ok=False,
                command=command,
                stdout="",
                stderr="boom",
                exit_code=1,
                timed_out=False,
                truncated=False,
                duration_ms=5,
                error_code="powershell_start_failed",
                message="PowerShell 进程启动失败：PermissionError",
            )

    service.executor = FailingExecutor()

    payload = json.loads(
        asyncio.run(
            service.execute(
                "Get-Date",
                "读取当前时间",
                run_id="run-1",
                session_id="session-1",
                approval_id="approval-1",
            )
        )
    )

    assert payload["ok"] is False
    assert payload["result"]["stderr"] == "boom"
    assert payload["error"] == {
        "code": "powershell_start_failed",
        "message": "PowerShell 进程启动失败：PermissionError",
    }
