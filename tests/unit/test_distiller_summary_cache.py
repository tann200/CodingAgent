from src.core.memory.distiller import distill_context
import json


def test_distiller_writes_file_summaries(tmp_path, monkeypatch):
    # create a fake repo structure
    src = tmp_path / "src"
    src.mkdir()
    f = src / "big.py"
    # write > 250 lines
    content = "\n".join([f"line {i}" for i in range(400)])
    f.write_text(content)
    # create repo_index.json expected layout
    ac = tmp_path / ".agent-context"
    ac.mkdir()
    repo_index = {"files": [{"path": "src/big.py"}], "symbols": []}
    (ac / "repo_index.json").write_text(json.dumps(repo_index))

    # Monkeypatch call_model to avoid calling a real LLM
    def dummy_call_model(*args, **kwargs):
        return {"choices": [{"message": {"content": json.dumps({"current_task": "X","completed_steps": [],"next_step": "Y"})}}]}

    monkeypatch.setattr('src.core.memory.distiller._call_llm_sync',
                        lambda msgs, format_json=False: dummy_call_model()["choices"][0]["message"]["content"])

    # call distill_context with a minimal non-empty message so the function proceeds
    res = distill_context([{"role": "user", "content": "summarize"}], working_dir=tmp_path)
    # check that file_summaries.json exists
    s_path = ac / "file_summaries.json"
    assert s_path.exists()
    js = json.loads(s_path.read_text())
    assert "src/big.py" in js
    assert "[...skipped...]" in js["src/big.py"]
