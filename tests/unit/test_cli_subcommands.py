"""
Tests for Phase 3.7 CLI feature parity subcommands:

- ``codingagent session list|show|export``
- ``codingagent status``
- ``codingagent mcp list|status|add``
- ``codingagent diff``
- ``--provider`` / ``--model`` headless routing overrides
- ``--continue`` re-run of the last task
"""

import json
import subprocess
from pathlib import Path

import pytest

from src.cli import \
    diff_cmd, mcp_cmd, session_cmd, status_cmd
from src.core.orchestration.session_store import save_session  # noqa: E402
from src.main import _parse_args  # noqa: E402


@pytest.fixture
def session_dir(tmp_path: Path, monkeypatch):
    """Point the session store at a fresh temp dir."""
    import src.core.orchestration.session_store as ss

    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path)
    return tmp_path


def _seed_session(session_dir: Path, session_id: str, task_name: str, messages) -> None:
    from src.core.orchestration.session_store import StoredSession

    s = StoredSession(
        version=1,
        session_id=session_id,
        task_name=task_name,
        working_dir=str(session_dir),
        messages=messages,
        message_count=len(messages),
        turn_count=1,
        input_tokens=10,
        output_tokens=5,
        created_at="2026-01-01T00:00:00Z",
    )
    save_session(s)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class TestParser:
    def test_provider_model_flags(self):
        args = _parse_args(
            ["--task", "hi", "--provider", "ollama", "--model", "llama3", "--output-format", "raw"]
        )
        assert args.provider == "ollama"
        assert args.model == "llama3"
        assert args.continue_task is False

    def test_continue_flag(self):
        args = _parse_args(["--continue"])
        assert args.continue_task is True

    def test_session_subcommand(self):
        args = _parse_args(["session", "list", "--limit", "5", "--json"])
        assert args.subcommand == "session"
        assert args.session_action == "list"
        assert args.json is True

    def test_mcp_add_subcommand(self):
        args = _parse_args(["mcp", "add", "ctx", "npx", "-y", "@x/mcp"])
        assert args.subcommand == "mcp"
        assert args.mcp_action == "add"
        assert args.name == "ctx"
        assert args.cmd == ["npx", "-y", "@x/mcp"]

    def test_diff_path_flag(self):
        args = _parse_args(["diff", "--path", "src/", "--json"])
        assert args.subcommand == "diff"
        assert args.path == "src/"
        assert args.json is True


# --------------------------------------------------------------------------
# session subcommand
# --------------------------------------------------------------------------


class TestSessionSubcommand:
    def test_list_empty(self, session_dir, capsys):
        assert session_cmd.run_session(_parse_args(["session", "list"])) == 0
        assert "No saved sessions" in capsys.readouterr().out

    def test_list_shows_sessions(self, session_dir, capsys):
        _seed_session(session_dir, "abc123", "My Task", [])
        assert session_cmd.run_session(_parse_args(["session", "list"])) == 0
        out = capsys.readouterr().out
        assert "abc123" in out
        assert "My Task" in out

    def test_list_json(self, session_dir, capsys):
        _seed_session(session_dir, "abc123", "My Task", [])
        assert session_cmd.run_session(_parse_args(["session", "list", "--json"])) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 1
        assert payload["sessions"][0]["session_id"] == "abc123"

    def test_show_timeline(self, session_dir, capsys):
        _seed_session(
            session_dir,
            "abc123",
            "My Task",
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        )
        assert session_cmd.run_session(_parse_args(["session", "show", "abc123"])) == 0
        out = capsys.readouterr().out
        assert "USER" in out and "hello" in out
        assert "ASSISTANT" in out and "hi there" in out

    def test_show_missing(self, session_dir, capsys):
        assert session_cmd.run_session(_parse_args(["session", "show", "nope"])) == 1
        assert "not found" in capsys.readouterr().err

    def test_export_writes_markdown(self, session_dir, tmp_path, capsys):
        _seed_session(
            session_dir,
            "abc123",
            "My Task",
            [{"role": "user", "content": "hello world"}],
        )
        out_file = tmp_path / "export.md"
        assert (
            session_cmd.run_session(
                _parse_args(["session", "export", "abc123", "--out", str(out_file)])
            )
            == 0
        )
        assert out_file.exists()
        assert "hello world" in out_file.read_text()
        assert "My Task" in out_file.read_text() or "abc123" in out_file.read_text()


# --------------------------------------------------------------------------
# status subcommand
# --------------------------------------------------------------------------


class TestStatusSubcommand:
    def test_status_pretty(self, monkeypatch, capsys):
        monkeypatch.setattr(
            status_cmd,
            "_active_provider",
            lambda: {"provider": "lm_studio", "model": "qwen/qwen3.5-9b"},
        )
        assert status_cmd.run_status(_parse_args(["status", "--workdir", "."])) == 0
        out = capsys.readouterr().out
        assert "lm_studio" in out
        assert "qwen/qwen3.5-9b" in out

    def test_status_json(self, monkeypatch, capsys):
        monkeypatch.setattr(
            status_cmd,
            "_active_provider",
            lambda: {"provider": "ollama", "model": "llama3.1"},
        )
        assert (
            status_cmd.run_status(_parse_args(["status", "--json", "--workdir", "."]))
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["provider"] == "ollama"
        assert payload["model"] == "llama3.1"
        assert payload["git_branch"] != ""

    def test_active_provider_reads_providers_json(self, monkeypatch, tmp_path):
        cfg = tmp_path / "providers.json"
        cfg.write_text(
            json.dumps(
                [
                    {"name": "ollama", "type": "ollama", "active": False, "default_model": "x"},
                    {"name": "lm_studio", "type": "lm_studio", "active": True, "default_model": "qwen/qwen3.5-9b"},
                ]
            )
        )
        import src.core.inference.provider_config as _pc

        monkeypatch.setattr(_pc, "resolve_providers_config_path", lambda *a, **k: cfg)
        assert status_cmd._active_provider() == {
            "provider": "lm_studio",
            "model": "qwen/qwen3.5-9b",
        }


# --------------------------------------------------------------------------
# mcp subcommand
# --------------------------------------------------------------------------


class TestMcpSubcommand:
    def test_mcp_list(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "src.core.config_loader.get_mcp_servers",
            lambda **k: [
                {"name": "ctx", "cmd": ["npx", "-y", "@x/mcp"], "transport": "stdio"}
            ],
        )
        assert mcp_cmd.run_mcp(_parse_args(["mcp", "list"])) == 0
        out = capsys.readouterr().out
        assert "ctx" in out and "npx" in out

    def test_mcp_list_json(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "src.core.config_loader.get_mcp_servers",
            lambda **k: [{"name": "ctx", "cmd": ["npx"], "transport": "stdio"}],
        )
        assert mcp_cmd.run_mcp(_parse_args(["mcp", "list", "--json"])) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["servers"][0]["name"] == "ctx"

    def test_mcp_add_rejects_unsafe_cmd(self, tmp_path, capsys):
        assert (
            mcp_cmd.run_mcp(
                _parse_args(
                    ["mcp", "--workdir", str(tmp_path), "add", "bad", "bash", "-c", "evil;rm"]
                )
            )
            == 1
        )
        assert "unsafe" in capsys.readouterr().err

    def test_mcp_add_writes_config(self, tmp_path, capsys):
        assert (
            mcp_cmd.run_mcp(
                _parse_args(
                    ["mcp", "--workdir", str(tmp_path), "add", "my_srv", "npx", "-y", "@x/mcp"]
                )
            )
            == 0
        )
        cfg_path = tmp_path / ".agent" / "config.json"
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text())
        assert cfg["mcp"]["servers"][0]["name"] == "my_srv"


# --------------------------------------------------------------------------
# diff subcommand
# --------------------------------------------------------------------------


class TestDiffSubcommand:
    def _init_git_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        f = tmp_path / "a.txt"
        f.write_text("v1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
        return tmp_path

    def test_diff_shows_changes(self, tmp_path, capsys):
        repo = self._init_git_repo(tmp_path)
        (repo / "a.txt").write_text("v2\n")
        assert diff_cmd.run_diff(_parse_args(["diff", "--workdir", str(repo)])) == 0
        out = capsys.readouterr().out
        assert "+v2" in out

    def test_diff_json(self, tmp_path, capsys):
        repo = self._init_git_repo(tmp_path)
        assert diff_cmd.run_diff(_parse_args(["diff", "--workdir", str(repo), "--json"])) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["base"] == "HEAD"
        assert "no changes" in payload["diff"]

    def test_diff_not_a_repo(self, tmp_path, capsys):
        assert diff_cmd.run_diff(_parse_args(["diff", "--workdir", str(tmp_path)])) == 1
        assert "not a git repository" in capsys.readouterr().err


# --------------------------------------------------------------------------
# --continue / last-task loading
# --------------------------------------------------------------------------


class TestContinue:
    def test_load_last_user_task_from_last_plan(self, tmp_path, monkeypatch):
        import src.main as main_mod

        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir()
        (agent_dir / "last_plan.json").write_text(
            json.dumps({"task": "fix the bug", "plan": [], "current_step": 0, "working_dir": "."})
        )
        assert main_mod._load_last_user_task(str(tmp_path)) == "fix the bug"

    def test_load_last_user_task_from_session(self, session_dir, monkeypatch):
        import src.main as main_mod

        _seed_session(session_dir, "sess1", "t", [{"role": "user", "content": "do work"}])
        monkeypatch.setattr(
            "src.core.orchestration.session_store._SESSIONS_DIR", session_dir
        )
        # monkeypatch the default workdir to a dir whose .codingAgent has no plan
        monkeypatch.setattr(main_mod.os, "getcwd", lambda: str(session_dir))
        assert main_mod._load_last_user_task(str(session_dir)) == "do work"

    def test_load_last_user_task_empty(self, session_dir, monkeypatch):
        import src.main as main_mod

        _seed_session(session_dir, "sess1", "t", [])
        monkeypatch.setattr(
            "src.core.orchestration.session_store._SESSIONS_DIR", session_dir
        )
        monkeypatch.setattr(main_mod.os, "getcwd", lambda: str(session_dir))
        assert main_mod._load_last_user_task(str(session_dir)) == ""


# --------------------------------------------------------------------------
# headless --provider/--model routing
# --------------------------------------------------------------------------


class TestHeadlessRouting:
    def test_model_routing_published(self, tmp_path, monkeypatch, capsys):
        import src.core.orchestration.orchestrator as orch_mod
        from src.core.messaging.event_types import ModelRouting

        published = []

        class FakeBus:
            def publish_typed(self, event):
                published.append(event)

        class FakeAdapter:
            default_model = ""

        class FakeOrch:
            event_bus = FakeBus()
            _adapter = FakeAdapter()
            working_dir = str(tmp_path)

            def run_agent_once(self, **kw):
                return {"assistant_message": "ok", "work_summary": ""}

        monkeypatch.setattr(orch_mod, "Orchestrator", lambda **k: FakeOrch())

        import src.main as main_mod

        rc = main_mod._run_headless(
            "hi", "raw", str(tmp_path), provider="ollama", model="llama3"
        )
        assert rc == 0
        assert len(published) == 1
        assert isinstance(published[0], ModelRouting)
        assert published[0].provider == "ollama"
        assert published[0].selected == "llama3"
        assert "ok" in capsys.readouterr().out
