from src.core.context.context_builder import ContextBuilder
import json
from unittest.mock import patch
from pathlib import Path


def test_context_builder_uses_summary_cache(tmp_path):
    # prepare .agent-context with file_summaries.json
    ac = tmp_path / ".agent-context"
    ac.mkdir(parents=True, exist_ok=True)
    summaries = {"src/main.py": "SHORT SUMMARY FROM CACHE"}
    (ac / "file_summaries.json").write_text(json.dumps(summaries))

    builder = ContextBuilder()
    identity = "I am agent"
    role = "assistant"
    skills = []
    task = "Do task"
    tools = []
    # retrieved snippet provides a different snippet; builder should prefer cache
    retrieved = [{"file_path": "src/main.py", "snippet": "RAW LONG FILE CONTENT"}]

    # Mock Path.cwd() to return tmp_path so the builder can find the cache
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        msgs = builder.build_prompt(
            identity, role, skills, task, tools, [], retrieved_snippets=retrieved
        )
        system = msgs[0]["content"]
        assert "SHORT SUMMARY FROM CACHE" in system
        assert "RAW LONG FILE CONTENT" not in system
