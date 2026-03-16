from typing import Dict, Any
from pathlib import Path
import ast
import json


def analyze_repository(workdir: str) -> Dict[str, Any]:
    """
    Analyzes the repository and creates a repo_memory.json file with summaries and dependencies.
    """
    try:
        workdir_path = Path(workdir)
        repo_memory = {
            "module_summaries": {},
            "dependency_relationships": {},
        }

        files = list(workdir_path.glob("**/*.py"))

        for file in files:
            relative_path = str(file.relative_to(workdir_path))
            summary, imports = _analyze_file(file)
            repo_memory["module_summaries"][relative_path] = summary
            repo_memory["dependency_relationships"][relative_path] = imports

        repo_memory_path = workdir_path / ".agent-context" / "repo_memory.json"
        repo_memory_path.parent.mkdir(parents=True, exist_ok=True)
        repo_memory_path.write_text(json.dumps(repo_memory, indent=2))

        return {
            "status": "ok",
            "message": f"Repository analysis complete. Found {len(files)} files.",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _analyze_file(file_path: Path):
    summary = {
        "classes": [],
        "functions": [],
    }
    imports = []

    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    summary["classes"].append(node.name)
                elif isinstance(node, ast.FunctionDef):
                    summary["functions"].append(node.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module)
        except Exception:
            pass  # Ignore files that can't be parsed

    return summary, imports
