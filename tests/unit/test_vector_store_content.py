from src.core.indexing.vector_store import VectorStore
import json


def test_vector_store_embeds_symbol_summaries(tmp_path):
    # create a fake .agent-context and repo_index.json
    agent_ctx = tmp_path / ".agent-context"
    agent_ctx.mkdir(parents=True, exist_ok=True)
    repo_index = {
        "files": ["src/main.py"],
        "symbols": [
            {"file_path": "src/main.py", "symbol_type": "function", "symbol_name": "main", "docstring": "Does work", "start_line": 1}
        ],
    }
    (agent_ctx / "repo_index.json").write_text(json.dumps(repo_index))

    vs = VectorStore(str(tmp_path))
    # index_code should not raise and should produce table
    vs.index_code(repo_index)
    # open table and ensure 'text' field contains only symbol-summary text, not full file content
    tbl = vs.db.open_table("code_symbols")
    df = tbl.to_pandas()
    assert "text" in df.columns
    assert "File: src/main.py" in df.iloc[0]["text"]
    assert "Docstring:" in df.iloc[0]["text"]

