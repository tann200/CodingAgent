from typing import Dict, Any, List
from pathlib import Path
import ast
import json
import re

from src.tools._tool import tool


_EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
}


@tool(tags=["review"])
def analyze_repository(workdir: str) -> Dict[str, Any]:
    """
    Analyzes the repository across multiple languages (Python, JS/TS, Go, Rust)
    and creates a repo_memory.json with summaries, dependencies, and per-language stats.
    """
    try:
        workdir_path = Path(workdir)
        languages: Dict[str, Any] = {}

        # Python — AST-based
        py_files = [
            f
            for f in workdir_path.glob("**/*.py")
            if not any(part in _EXCLUDE_DIRS for part in f.parts)
        ]
        if py_files:
            py_data = _analyze_python_files(py_files)
            languages["python"] = py_data

        # JavaScript / TypeScript — regex-based
        js_patterns = ("*.js", "*.jsx", "*.mjs", "*.cjs", "*.ts", "*.tsx")
        js_files: List[Path] = []
        for pat in js_patterns:
            js_files.extend(
                f
                for f in workdir_path.glob(f"**/{pat}")
                if not any(part in _EXCLUDE_DIRS for part in f.parts)
            )
        if js_files:
            js_data = _analyze_js_ts_files(js_files)
            languages["javascript"] = js_data

        # Go — regex-based
        go_files = [
            f
            for f in workdir_path.glob("**/*.go")
            if not any(part in _EXCLUDE_DIRS for part in f.parts)
        ]
        if go_files:
            go_data = _analyze_go_files(go_files)
            languages["go"] = go_data

        # Rust — regex-based
        rs_files = [
            f
            for f in workdir_path.glob("**/*.rs")
            if not any(part in _EXCLUDE_DIRS for part in f.parts)
        ]
        if rs_files:
            rs_data = _analyze_rust_files(rs_files)
            languages["rust"] = rs_data

        # Build module_summaries + dependency_relationships from all languages
        repo_memory = {
            "module_summaries": {},
            "dependency_relationships": {},
            "languages": {},
        }
        for lang, data in languages.items():
            repo_memory["languages"][lang] = {
                "files": data.get("file_count", 0),
                "functions": data.get("function_count", 0),
                "classes": data.get("class_count", 0),
            }
            for fpath, fsummary in data.get("summaries", {}).items():
                rel = str(Path(fpath).relative_to(workdir_path))
                repo_memory["module_summaries"][rel] = fsummary
            for fpath, fimports in data.get("imports", {}).items():
                rel = str(Path(fpath).relative_to(workdir_path))
                repo_memory["dependency_relationships"][rel] = fimports

        repo_memory_path = workdir_path / ".agent-context" / "repo_memory.json"
        repo_memory_path.parent.mkdir(parents=True, exist_ok=True)
        repo_memory_path.write_text(json.dumps(repo_memory, indent=2))

        total = sum(d.get("file_count", 0) for d in languages.values())
        return {
            "status": "ok",
            "message": f"Repository analysis complete. Found {total} files across {list(languages.keys())}.",
            "languages": repo_memory["languages"],
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---- Python (AST-based) ----


def _analyze_python_files(files: List[Path]) -> Dict[str, Any]:
    summaries: Dict[str, Any] = {}
    imports: Dict[str, List[str]] = {}
    func_count = 0
    cls_count = 0
    for f in files:
        summary, imps = _analyze_python_file(f)
        summaries[str(f)] = summary
        imports[str(f)] = imps
        func_count += len(summary.get("functions", []))
        cls_count += len(summary.get("classes", []))
    return {
        "file_count": len(files),
        "function_count": func_count,
        "class_count": cls_count,
        "summaries": summaries,
        "imports": imports,
    }


def _analyze_python_file(file_path: Path):
    summary = {"classes": [], "functions": []}
    imports = []
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    summary["classes"].append(node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    summary["functions"].append(node.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module)
        except Exception:
            pass
    return summary, imports


# Backward-compatible alias
_analyze_file = _analyze_python_file


# ---- JS/TS (regex-based) ----

_EXPORT_FUNC_RE = re.compile(r"export\s+(?:async\s+)?function\s+(\w+)")
_EXPORT_CLASS_RE = re.compile(r"export\s+class\s+(\w+)")
_EXPORT_CONST_RE = re.compile(r"export\s+(?:const|let|var)\s+(\w+)")
_IMPORT_RE = re.compile(r"import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]")
_FUNC_RE = re.compile(r"(?:async\s+)?function\s+(\w+)")
_ARROW_EXPORT_RE = re.compile(r"export\s+(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(")


def _analyze_js_ts_files(files: List[Path]) -> Dict[str, Any]:
    summaries: Dict[str, Any] = {}
    imports: Dict[str, List[str]] = {}
    func_count = 0
    cls_count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        funcs = list(
            set(
                _EXPORT_FUNC_RE.findall(content)
                + _FUNC_RE.findall(content)
                + _ARROW_EXPORT_RE.findall(content)
            )
        )
        classes = _EXPORT_CLASS_RE.findall(content)
        exports = _EXPORT_CONST_RE.findall(content)
        imps = _IMPORT_RE.findall(content)
        summaries[str(f)] = {"functions": funcs, "classes": classes, "exports": exports}
        imports[str(f)] = imps
        func_count += len(funcs)
        cls_count += len(classes)
    return {
        "file_count": len(files),
        "function_count": func_count,
        "class_count": cls_count,
        "summaries": summaries,
        "imports": imports,
    }


# ---- Go (regex-based) ----

_GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(", re.MULTILINE)
_GO_STRUCT_RE = re.compile(r"^type\s+(\w+)\s+struct", re.MULTILINE)
_GO_IMPORT_RE = re.compile(r'"([^"]+)"')


def _analyze_go_files(files: List[Path]) -> Dict[str, Any]:
    summaries: Dict[str, Any] = {}
    imports: Dict[str, List[str]] = {}
    func_count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        funcs = _GO_FUNC_RE.findall(content)
        structs = _GO_STRUCT_RE.findall(content)
        imps = _GO_IMPORT_RE.findall(content)
        summaries[str(f)] = {"functions": funcs, "structs": structs}
        imports[str(f)] = imps
        func_count += len(funcs)
    return {
        "file_count": len(files),
        "function_count": func_count,
        "class_count": 0,
        "summaries": summaries,
        "imports": imports,
    }


# ---- Rust (regex-based) ----

_RS_PUB_FN_RE = re.compile(r"pub\s+(?:async\s+)?fn\s+(\w+)")
_RS_PRIV_FN_RE = re.compile(r"^\s*fn\s+(\w+)", re.MULTILINE)
_RS_STRUCT_RE = re.compile(r"pub\s+struct\s+(\w+)")
_RS_ENUM_RE = re.compile(r"pub\s+enum\s+(\w+)")
_RS_USE_RE = re.compile(r"^use\s+([\w:]+)", re.MULTILINE)


def _analyze_rust_files(files: List[Path]) -> Dict[str, Any]:
    summaries: Dict[str, Any] = {}
    imports: Dict[str, List[str]] = {}
    func_count = 0
    cls_count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        pub_fns = _RS_PUB_FN_RE.findall(content)
        priv_fns = [f for f in _RS_PRIV_FN_RE.findall(content) if f not in set(pub_fns)]
        structs = _RS_STRUCT_RE.findall(content)
        enums = _RS_ENUM_RE.findall(content)
        uses = _RS_USE_RE.findall(content)
        summaries[str(f)] = {
            "pub_functions": pub_fns,
            "priv_functions": priv_fns,
            "structs": structs,
            "enums": enums,
        }
        imports[str(f)] = uses
        func_count += len(pub_fns) + len(priv_fns)
        cls_count += len(structs) + len(enums)
    return {
        "file_count": len(files),
        "function_count": func_count,
        "class_count": cls_count,
        "summaries": summaries,
        "imports": imports,
    }
