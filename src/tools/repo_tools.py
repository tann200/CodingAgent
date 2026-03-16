from typing import Dict, Any

from src.core.indexing.repo_indexer import index_repository
from src.core.indexing.vector_store import VectorStore
from pathlib import Path

def initialize_repo_intelligence(workdir: str) -> Dict[str, Any]:
    """
    Initializes or updates the repository index and vector store.
    """
    try:
        repo_index = index_repository(workdir)
        
        vs = VectorStore(workdir)
        vs.index_code(repo_index)
        
        return {"status": "ok", "indexed_files": len(repo_index.get("files", [])), "indexed_symbols": len(repo_index.get("symbols", []))}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def search_code(query: str, workdir: str) -> Dict[str, Any]:
    """
    Performs a semantic search over the codebase.
    """
    try:
        vs = VectorStore(workdir)
        results = vs.search(query)
        return {"status": "ok", "results": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def find_symbol(name: str, workdir: str) -> Dict[str, Any]:
    """
    Finds a symbol (class or function) by its exact name.
    """
    import json
    from pathlib import Path
    
    index_path = Path(workdir) / ".agent-context" / "repo_index.json"
    if not index_path.exists():
        return {"status": "error", "error": "Repo index not found. Run initialize_repo_intelligence first."}
        
    with open(index_path, "r") as f:
        repo_index = json.load(f)
        
    results = [s for s in repo_index["symbols"] if s["symbol_name"] == name]
    return {"status": "ok", "results": results}


def find_references(name: str, workdir: str) -> Dict[str, Any]:
    """
    Find references to a symbol by scanning indexed files for occurrences of the symbol name.
    This is a simple substring match over file contents.
    """
    from pathlib import Path
    import json
    try:
        base = Path(workdir)
        index_path = base / ".agent-context" / "repo_index.json"
        if not index_path.exists():
            return {"status": "error", "error": "Repo index not found. Run initialize_repo_intelligence first."}
        with open(index_path, 'r', encoding='utf-8') as f:
            repo_index = json.load(f)
        files = [f.get('path') for f in repo_index.get('files', [])]
        refs = []
        for rel in files:
            p = base / rel
            try:
                text = p.read_text(encoding='utf-8')
                if name in text:
                    refs.append({"file": str(rel), "snippet": text[:300]})
            except Exception:
                continue
        return {"status": "ok", "results": refs}
    except Exception as e:
        return {"status": "error", "error": str(e)}
