import ast
import json
from pathlib import Path
from typing import Dict, Any, List
"TODO: the repo indexer is only working on python projects right now. "

def get_imports(node: ast.AST) -> List[str]:
    imports = []
    for item in node.body:
        if isinstance(item, ast.Import):
            for alias in item.names:
                imports.append(alias.name)
        elif isinstance(item, ast.ImportFrom):
            if item.module:
                for alias in item.names:
                    imports.append(f"{item.module}.{alias.name}")
    return imports

def parse_python_file(path: Path) -> Dict[str, Any]:
    """
    Parses a Python file to extract file-level info, classes, functions, and imports.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            tree = ast.parse(content)
    except Exception:
        return {}

    classes = []
    functions = []
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "type": "class",
                "start_line": node.lineno,
                "end_line": node.end_lineno,
                "docstring": ast.get_docstring(node)
            })
        elif isinstance(node, ast.FunctionDef):
            functions.append({
                "name": node.name,
                "type": "function",
                "start_line": node.lineno,
                "end_line": node.end_lineno,
                "docstring": ast.get_docstring(node)
            })
            
    return {
        "file_path": str(path),
        "imports": get_imports(tree),
        "classes": classes,
        "functions": functions
    }

def index_repository(workdir: str) -> Dict[str, Any]:
    """
    Walks through the repository, parses Python files, and builds an index.
    """
    repo_index = {
        "files": [],
        "symbols": []
    }
    
    base_path = Path(workdir)
    
    ignore_dirs = {".git", "__pycache__", "node_modules", "build", "dist", ".venv"}
    
    for path in base_path.rglob("*.py"):
        # Check against ignore patterns
        if any(part in ignore_dirs for part in path.parts):
            continue
            
        file_data = parse_python_file(path)
        if file_data:
            repo_index["files"].append({
                "path": str(path.relative_to(base_path)),
                "imports": file_data["imports"]
            })
            for class_data in file_data["classes"]:
                repo_index["symbols"].append({
                    "symbol_name": class_data["name"],
                    "symbol_type": "class",
                    "file_path": str(path.relative_to(base_path)),
                    "start_line": class_data["start_line"],
                    "end_line": class_data["end_line"],
                    "docstring": class_data["docstring"]
                })
            for func_data in file_data["functions"]:
                 repo_index["symbols"].append({
                    "symbol_name": func_data["name"],
                    "symbol_type": "function",
                    "file_path": str(path.relative_to(base_path)),
                    "start_line": func_data["start_line"],
                    "end_line": func_data["end_line"],
                    "docstring": func_data["docstring"]
                })

    # Save to file
    index_path = base_path / ".agent-context" / "repo_index.json"
    index_path.parent.mkdir(exist_ok=True)
    with open(index_path, "w") as f:
        json.dump(repo_index, f, indent=2)
        
    return repo_index

if __name__ == "__main__":
    # Example usage
    # Replace '.' with your repository path
    index = index_repository('.')
    print(f"Indexed {len(index['files'])} files and {len(index['symbols'])} symbols.")
