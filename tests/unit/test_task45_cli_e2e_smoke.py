"""TASK-45: Offline headless CLI E2E smoke suite & README/SPEC consistency checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch


def test_cli_init_then_context_report_all_tiers_offline(tmp_path: Path, capsys):
    """`codingagent init` followed by `codingagent context-report --json` across all 3 flat tiers."""
    from src.main import main

    rc_init = main(["init", "--dir", str(tmp_path)])
    assert rc_init == 0
    assert (tmp_path / ".codingAgent").is_dir()
    capsys.readouterr()

    expected_counts = {
        "compact-flat": 10,
        "standard-flat": 14,
        "frontier-flat": 18,
    }
    from src.config.toolsets.loader import get_tools_for_toolset

    for tier, expected_count in expected_counts.items():
        rc = main(["context-report", "--workdir", str(tmp_path), "--tier", tier, "--json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["tier"] == tier
        tool_block = payload["sections"]["tool_schemas"]
        assert tool_block["count"] == expected_count
        assert tool_block["tokens"] > 0
        assert len(tool_block["top_tools"]) == 5
        assert payload["totals"]["upfront_tokens"] > 0
        tool_names = set(get_tools_for_toolset(tier))
        assert {"read_file", "edit_file_atomic", "bash"}.issubset(tool_names)
        if tier in ("standard-flat", "frontier-flat"):
            assert "git_read" in tool_names
            assert "find_symbol" in tool_names
        if tier == "frontier-flat":
            assert "lsp" in tool_names
            assert "load_tool_group" in tool_names


def test_cli_doctor_json_offline_mocked(tmp_path: Path, capsys, monkeypatch):
    """`codingagent doctor --json` produces strictly machine-readable JSON offline."""
    from src.cli import doctor_cmd
    from src.main import main

    monkeypatch.setattr(
        doctor_cmd,
        "check_endpoint_reachability",
        lambda url, timeout=2.0: (False, None, "offline mock"),
    )
    monkeypatch.setattr(
        doctor_cmd,
        "discover_provider_models",
        lambda *args, **kwargs: [],
        raising=False,
    )
    # Prevent any stray requests.get/post from hitting localhost during unit tests
    import requests

    def _offline_net(*args, **kwargs):
        raise requests.exceptions.ConnectionError("offline mock")

    monkeypatch.setattr(requests, "get", _offline_net)
    monkeypatch.setattr(requests, "post", _offline_net)

    rc = main(["doctor", "--workdir", str(tmp_path), "--json"])
    assert rc in (0, 1)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert "system" in payload or "providers" in payload or "checks" in payload


def test_cli_headless_private_stream_json_offline(tmp_path: Path, capsys):
    """Headless `--private --output stream-json` emits NDJSON, sets private mode, and skips disk artifacts."""
    from src.core.context.context_builder import ContextBuilder
    from src.main import main

    # Clean workspace with one source file
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    prev_private = os.environ.pop("CODINGAGENT_PRIVATE", None)

    fake_result = {
        "ok": True,
        "status": "complete",
        "assistant_message": "Repository contains hello.py.",
        "usage": {"input_tokens": 120, "output_tokens": 24},
    }

    class _FakeBus:
        def __init__(self) -> None:
            self._subs: dict[str, list] = {}

        def subscribe(self, event: str, cb) -> None:
            self._subs.setdefault(event, []).append(cb)

        def unsubscribe(self, event: str, cb) -> None:
            if event in self._subs and cb in self._subs[event]:
                self._subs[event].remove(cb)

        def publish_typed(self, _evt) -> None:
            pass

    class _FakeOrchestrator:
        def __init__(self, *args, **kwargs) -> None:
            self.working_dir = str(tmp_path)
            self.event_bus = _FakeBus()
            self.private = bool(kwargs.get("private", False))
            self._usage_buffer = {"input_tokens": 120, "output_tokens": 24}

        def run_agent_once(self, *args, **kwargs):
            return dict(fake_result)

    try:
        with patch("src.core.orchestration.orchestrator.Orchestrator", _FakeOrchestrator):
            rc = main(
                [
                    "--task",
                    "Inspect hello.py",
                    "--workdir",
                    str(tmp_path),
                    "--private",
                    "--output",
                    "stream-json",
                ]
            )

        assert rc == 0
        assert os.environ.get("CODINGAGENT_PRIVATE") == "1"

        # Verify private prompt line is emitted when CODINGAGENT_PRIVATE=1
        from src.core.context.tool_prompt_sections import PRIVATE_PROMPT_LINE

        builder = ContextBuilder(working_dir=str(tmp_path))
        prompt = builder.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="Inspect hello.py",
            tools=[],
            conversation=[],
        )
        system_text = prompt[0]["content"]
        assert PRIVATE_PROMPT_LINE in system_text

        # Verify strict NDJSON on stdout ending in authoritative result envelope
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert lines, "Expected at least one NDJSON line on stdout"
        parsed = [json.loads(line) for line in lines]
        final = parsed[-1]
        assert final["type"] == "result"
        assert final["status"] == "complete"
        assert final["result"] == "Repository contains hello.py."
        assert final["usage"]["input_tokens"] == 120

        # No persistence artifacts created under .codingAgent/
        agent_dir = tmp_path / ".codingAgent"
        assert not (agent_dir / "sessions.db").exists()
        assert not (agent_dir / "execution_trace.json").exists()
        assert not (agent_dir / "timings.json").exists()
    finally:
        if prev_private is None:
            os.environ.pop("CODINGAGENT_PRIVATE", None)
        else:
            os.environ["CODINGAGENT_PRIVATE"] = prev_private


def test_docs_spec_and_readme_factual_consistency():
    """Ensure SPEC.md has no stale LangGraph reference and README matches 10/14/18 flat toolsets."""
    repo_root = Path(__file__).resolve().parents[2]
    spec_text = (repo_root / "docs" / "SPEC.md").read_text(encoding="utf-8")
    readme_text = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "LangGraph node boundaries" not in spec_text
    assert "`AgentRunner` node boundaries" in spec_text
    assert "10 flat tools" in readme_text
    assert "14 flat tools" in readme_text
    assert "18 flat tools" in readme_text
    assert "load_tool_group" in readme_text
