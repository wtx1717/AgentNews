from __future__ import annotations

import time

from fastapi.testclient import TestClient

from agentnews.agent import AgentRunResult
from agentnews.config import Settings
from agentnews.repositories import InMemoryRepository
from agentnews.api import create_app


class BashRunner:
    def __init__(self, *, reject: bool = False):
        self.reject = reject
        self.calls = 0
        self.tool_results = []

    async def ainvoke(self, prompt, *, task_context=None, bash_approval_handler=None):
        self.calls += 1
        result = await bash_approval_handler(
            "Write-Output 'approved-by-user'",
            "执行一个无副作用的测试命令",
        )
        self.tool_results.append(result)
        return AgentRunResult("模型已处理命令结果", 1, "completed")


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        deepseek_api_key=None,
        deepseek_model=None,
        mcp_search_server_url=None,
        mcp_search_api_key=None,
        report_root=tmp_path / "reports",
        source_archive_root=tmp_path / "source_archive",
        temp_root=tmp_path / "tmp",
        sqlite_path=tmp_path / "agentnews.sqlite3",
    )


def test_chat_bash_requires_approval_and_resumes(tmp_path):
    runner = BashRunner()
    repository = InMemoryRepository()
    with TestClient(create_app(repository, agent_runner=runner, settings=_settings(tmp_path))) as client:
        pending = client.post("/api/chat", json={"message": "执行测试命令"})
        assert pending.status_code == 202
        approval = pending.json()["approval"]
        run_id = pending.json()["run_id"]
        assert approval["command"] == "Write-Output 'approved-by-user'"
        assert runner.calls == 1

        before = client.get(f"/api/chat/runs/{run_id}").json()
        assert before["status"] == "pending_approval"

        decided = client.post(
            f"/api/chat/runs/{run_id}/approvals/{approval['id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 202

        for _ in range(30):
            current = client.get(f"/api/chat/runs/{run_id}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.02)
        assert current["status"] == "completed"
        assert current["result"]["answer"] == "模型已处理命令结果"
        assert repository.list_messages(current["session_id"])[-1].content == "模型已处理命令结果"


def test_chat_bash_rejection_is_returned_to_model(tmp_path):
    runner = BashRunner(reject=True)
    with TestClient(create_app(InMemoryRepository(), agent_runner=runner, settings=_settings(tmp_path))) as client:
        pending = client.post("/api/chat", json={"message": "执行测试命令"})
        assert pending.status_code == 202
        run_id = pending.json()["run_id"]
        approval_id = pending.json()["approval"]["id"]
        client.post(
            f"/api/chat/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "reject"},
        )
        for _ in range(30):
            current = client.get(f"/api/chat/runs/{run_id}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.02)
        assert current["status"] == "completed"
        assert '"bash_denied"' in runner.tool_results[0]
