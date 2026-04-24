from pathlib import Path

from src.core.orchestration.graph.nodes.perception_node import (
    _build_perception_messages,
)


class DummyBuilder:
    def __init__(self, base_messages):
        self._base = base_messages

    def build_prompt(self, **kwargs):
        # Return a shallow copy so tests can inspect mutations
        return [dict(m) for m in self._base]

    def inject_prior_session_memories(self, task: str, limit: int = 3):
        return f"PRIOR_MEMORIES for {task}"


class DummySessionStore:
    def read_recent_decisions(self, max_entries=5):
        return [
            {"decision": "decide A", "created_at": "2023-01-01"},
            {"decision": "decide B", "created_at": "2023-01-02"},
        ]


def test_build_perception_messages_injections(tmp_path, monkeypatch):
    # Prepare a fake max_steps.txt template
    repo_root = Path(__file__).resolve().parents[3]
    tpl_dir = repo_root / "src" / "core" / "prompts" / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    tpl_path = tpl_dir / "max_steps.txt"
    tpl_path.write_text("MAX STEPS WARNING", encoding="utf-8")

    base_messages = [{"role": "system", "content": "SYSTEM BASE"}]
    builder = DummyBuilder(base_messages)

    orchestrator = type("O", (), {})()
    orchestrator.session_store = DummySessionStore()

    state = {"rounds": 0, "task": "do something", "turn_count": 49, "max_turns": 50}
    adapter = None
    retrieved_snippets = []
    active_skills = []
    tools_list = []
    history_for_prompt = []

    messages = _build_perception_messages(
        builder=builder,
        state=state,
        orchestrator=orchestrator,
        adapter=adapter,
        retrieved_snippets=retrieved_snippets,
        active_skills=active_skills,
        tools_list=tools_list,
        history_for_prompt=history_for_prompt,
        perception_role="operational",
        active_model_name="model-x",
    )

    # Assert prior memories injected
    assert any("PRIOR_MEMORIES" in m.get("content", "") for m in messages)
    # Assert recent decisions injected
    assert any("Recent task decisions" in m.get("content", "") for m in messages)
    # Assert max_steps template appended (match either test stub or real template)
    assert any(
        ("MAX STEPS WARNING" in m.get("content", ""))
        or ("approaching the maximum number of steps" in m.get("content", ""))
        for m in messages
    )
