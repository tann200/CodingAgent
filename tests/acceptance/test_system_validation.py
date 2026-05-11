from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.core.orchestration.event_bus import EventBus
from src.main import main
from src.server.app import app, register_event_bus


def _make_workdir(tmp_path: Path) -> Path:
    (tmp_path / ".codingAgent").mkdir()
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    return tmp_path


def test_acceptance_headless_cli_returns_assistant_message(tmp_path, capsys):
    workdir = _make_workdir(tmp_path)

    with patch("src.core.orchestration.orchestrator.Orchestrator") as mock_orchestrator:
        orch = mock_orchestrator.return_value
        orch.run_agent_once.return_value = {
            "ok": True,
            "assistant_message": "Acceptance CLI summary",
            "work_summary": "Validated headless path",
        }

        exit_code = main(
            [
                "--task",
                "Summarize the repository",
                "--output-format",
                "json",
                "--workdir",
                str(workdir),
            ]
        )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assistant_message"] == "Acceptance CLI summary"
    mock_orchestrator.assert_called_once_with(working_dir=str(workdir), dry_run=False)


def test_acceptance_server_task_and_websocket_flow(tmp_path, recv_json_ws):
    workdir = _make_workdir(tmp_path)
    bus = EventBus()

    dummy_orch = MagicMock()
    dummy_orch.get_tools_for_role.return_value = {}

    def _run_agent_once(*args, **kwargs):
        bus.publish(
            "tool.start",
            {"session_id": "acceptance-session", "tool": "read_file", "path": "notes.txt"},
        )
        return {"ok": True, "assistant_message": "task finished"}

    dummy_orch.run_agent_once.side_effect = _run_agent_once

    with TestClient(app) as client:
        register_event_bus(bus)
        with patch("src.server.app._get_or_create_orchestrator", return_value=dummy_orch):
            with client.websocket_connect(
                "/ws/session/acceptance-session?events=agent.start,tool.start,agent.end"
            ) as ws:
                response = client.post(
                    "/task",
                    json={
                        "task": "Read notes.txt and confirm completion",
                        "session_id": "acceptance-session",
                        "working_dir": str(workdir),
                    },
                )
                assert response.status_code == 202
                body = response.json()
                task_id = body["task_id"]
                assert body["status"] == "accepted"

                deadline = time.time() + 2.0
                final_status = body["status"]
                final_result = None
                while time.time() < deadline:
                    status_response = client.get(f"/task/{task_id}")
                    assert status_response.status_code == 200
                    status_body = status_response.json()
                    final_status = status_body["status"]
                    final_result = status_body.get("result")
                    if final_status == "completed":
                        break
                    time.sleep(0.05)

                assert final_status == "completed"
                assert final_result == "task finished"

                ws_events = []
                while len(ws_events) < 2:
                    msg = recv_json_ws(ws, timeout=2.0)
                    if msg.get("event") in {"agent.start", "agent.end"}:
                        ws_events.append(msg)

                assert ws_events[0]["event"] == "agent.start"
                assert ws_events[1]["event"] == "agent.end"
                assert ws_events[1]["data"]["status"] == "completed"


def test_acceptance_sse_endpoint_streams_session_events():
    async def _event_gen():
        yield (
            'data: {"event": "agent.end", "data": {'
            '"session_id": "acceptance-session", "status": "completed"}}\n\n'
        )

    with patch("src.server.app.sse_adapter") as mock_sse_adapter:
        mock_sse_adapter.event_generator.return_value = _event_gen()
        client = TestClient(app)
        response = client.get("/session/acceptance-session/events")

    assert response.status_code == 200
    assert '"event": "agent.end"' in response.text


def test_acceptance_write_guardrails_block_unsafe_writes(tmp_path):
    from src.core.orchestration.orchestrator import Orchestrator

    workdir = _make_workdir(tmp_path)
    orch = Orchestrator(working_dir=str(workdir))

    read_before_write = orch.execute_tool(
        {
            "name": "edit_file",
            "arguments": {
                "path": "notes.txt",
                "old_content": "hello\n",
                "new_content": "updated\n",
            },
        }
    )
    assert read_before_write.get("ok") is False
    assert "read" in read_before_write.get("error", "").lower()

    outside_path = orch.execute_tool(
        {
            "name": "write_file",
            "arguments": {
                "path": "../outside.txt",
                "content": "blocked\n",
            },
        }
    )
    assert outside_path.get("ok") is False
    assert "outside working directory" in outside_path.get("error", "").lower()
