"""repo_read_tools — read-only repository analysis tools.

Consolidates tools from the following former modules (now merged here):

- repo_overview_tool  → repo_overview()
- repo_summary        → helper functions (not @tool-decorated)
- repo_tools          → find_files(), search_code(), find_symbol(), find_references()
- repo_analysis_tools → analyze_repository()

Grouping convention
-------------------
- repo_read_tools.py  : read-only tools (no filesystem writes, side_effects=[])
- repo_write_tools.py : tools that write to the workspace (side_effects=["write"])
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool, PermissionKind
from src.tools.tools_config import agent_context_path

# ---- Constants ----

# Directories always excluded from the walk / analysis
_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "target",
    ".tox", ".eggs", "*.egg-info", ".cache", ".idea", ".DS_Store",
})

# Files that indicate project type
_MANIFEST_FILES: tuple[str, ...] = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Makefile", "CMakeLists.txt", "requirements.txt",
    "Pipfile", "poetry.lock",
)

# TASK-17: Repo-scale glob limit — higher than the file_tools.glob limit (500)
# because repo-wide searches legitimately return tens of thousands of paths.
MAX_GLOB_RESULTS = 10_000

# ---- repo_overview helpers ----


def _walk_tree(
    root: Path,
    max_depth: int,
    max_files: int,
) -> List[Dict[str, Any]]:
    """Walk the directory tree and return a flat list of entry dicts."""
    entries: List[Dict[str, Any]] = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth or len(entries) >= max_files:
            return
        try:
            children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return

        for child in children:
            if len(entries) >= max_files:
                break
            name = child.name
            # Skip excluded directories
            if child.is_dir() and name in _EXCLUDE_DIRS:
                continue
            rel = str(child.relative_to(root))
            entry: Dict[str, Any] = {
                "path": rel,
                "type": "dir" if child.is_dir() else "file",
                "depth": depth,
            }
            if child.is_file():
                try:
                    entry["size"] = child.stat().st_size
                except OSError:
                    entry["size"] = -1
            entries.append(entry)
            if child.is_dir():
                _recurse(child, depth + 1)

    _recurse(root, 1)
    return entries


def _detect_project_type(root: Path) -> List[str]:
    """Return a list of detected project-type labels."""
    labels: List[str] = []
    for manifest in _MANIFEST_FILES:
        if (root / manifest).exists():
            labels.append(manifest)
    return labels


@tool(side_effects=[], tags=["repo", "planning"], permission_kind=PermissionKind.READ_FILE)
def repo_overview(
    workdir: Optional[str] = None,
    max_depth: int = 3,
    max_files: int = 200,
) -> Dict[str, Any]:
    """Return a lightweight directory tree and project metadata for *workdir*.

    Args:
        workdir: Root directory to inspect.  Defaults to the current working
            directory when omitted.
        max_depth: Maximum directory depth to traverse (default 3).
        max_files: Maximum total entries to include (default 200).

    Returns:
        A dict with keys:
          - ``root``: absolute path of the inspected directory
          - ``entries``: list of ``{path, type, depth[, size]}`` dicts
          - ``truncated``: True when the file cap was hit
          - ``manifests``: list of detected manifest/config filenames
          - ``total_entries``: count of entries before truncation cap
    """
    root = Path(workdir).resolve() if workdir else Path.cwd()
    if not root.is_dir():
        return {
            "ok": False,
            "error": f"Directory not found: {root}",
            "root": str(root),
            "entries": [],
            "truncated": False,
            "manifests": [],
        }

    entries = _walk_tree(root, max_depth=max_depth, max_files=max_files)
    manifests = _detect_project_type(root)

    return {
        "ok": True,
        "root": str(root),
        "entries": entries,
        "truncated": len(entries) >= max_files,
        "manifests": manifests,
        "total_entries": len(entries),
    }


# ---- repo_summary helpers ----


def detect_framework(workdir: str) -> Optional[str]:
    """Detect web framework from imports and files."""
    workdir_path = Path(workdir)

    # Check for common frameworks
    framework_indicators = {
        "FastAPI": ["from fastapi import", "FastAPI()", "uvicorn"],
        "Flask": ["from flask import", "Flask("],
        "Express": ["express()", "require('express')"],
        "React": ["import React", "create-react-app"],
        "Vue": ["import Vue", "createApp("],
        "Angular": ["@Component", "ng serve"],
        "Spring": ["@SpringBootApplication"],
        "Rails": ["rails new", "application.rb"],
        "Laravel": ["Illuminate\\", "artisan serve"],
    }

    # Scan Python files for imports, excluding virtual envs and caches
    py_files = [
        f for f in workdir_path.rglob("*.py")
        if not any(part in _EXCLUDE_DIRS for part in f.parts)
    ]
    for py_file in py_files[:20]:  # Limit scanning
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for framework, indicators in framework_indicators.items():
                if any(ind in content for ind in indicators):
                    return framework
        except Exception:
            continue

    return None


def detect_languages(workdir: str) -> List[str]:
    """Detect programming languages in repo."""
    workdir_path = Path(workdir)
    languages = set()

    extensions = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".jsx": "JavaScript",
        ".tsx": "TypeScript",
        ".java": "Java",
        ".go": "Go",
        ".rs": "Rust",
        ".cpp": "C++",
        ".c": "C",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
        ".swift": "Swift",
        ".kt": "Kotlin",
    }

    # Single-pass directory scan instead of one rglob per extension (performance fix)
    for f in workdir_path.rglob("*"):
        if f.is_file() and not any(part in _EXCLUDE_DIRS for part in f.parts):
            lang = extensions.get(f.suffix.lower())
            if lang:
                languages.add(lang)

    return sorted(list(languages))


def detect_test_framework(workdir: str) -> Optional[str]:
    """Detect test framework."""
    workdir_path = Path(workdir)

    # Check for test files and configs
    test_indicators = {
        "pytest": ["pytest.ini", "conftest.py", "test_*.py", "tests/"],
        "unittest": ["unittest", "test_*.py"],
        "jest": ["jest.config.js", "jest.config.ts"],
        "vitest": ["vitest.config.js", "vitest.config.ts"],
        "mocha": ["mocha.opts", ".mocharc"],
        "rspec": ["spec/", "_spec.rb"],
        "go test": ["*_test.go"],
    }

    for framework, patterns in test_indicators.items():
        for pattern in patterns:
            if "*" in pattern:
                if list(workdir_path.rglob(pattern)):
                    return framework
            else:
                if (workdir_path / pattern).exists():
                    return framework

    return None


def detect_entrypoints(workdir: str) -> List[str]:
    """Detect entry point files."""
    workdir_path = Path(workdir)
    entrypoints = []

    common_entrypoints = [
        "main.py",
        "app.py",
        "server.py",
        "api.py",
        "index.js",
        "index.ts",
        "main.js",
        "main.ts",
        "main.go",
        "main.rs",
        "main.java",
        "run.py",
        "serve.py",
        "__main__.py",
    ]

    for entry in common_entrypoints:
        if (workdir_path / entry).exists():
            entrypoints.append(entry)

    # Look for __main__.py in packages
    for main_file in workdir_path.rglob("__main__.py"):
        entrypoints.append(str(main_file.relative_to(workdir_path)))

    return entrypoints


def list_modules(workdir: str) -> List[str]:
    """List top-level modules/packages."""
    workdir_path = Path(workdir)
    modules = []

    # Python packages
    for item in workdir_path.iterdir():
        if item.is_dir():
            if (item / "__init__.py").exists():
                modules.append(item.name)
            elif item.suffix == "" and not item.name.startswith("."):
                modules.append(item.name)

    # JS/TS packages
    if (workdir_path / "src").exists():
        for item in (workdir_path / "src").iterdir():
            if item.is_dir():
                modules.append(f"src/{item.name}")

    return sorted(modules)[:10]  # Limit to 10


def find_dependency_files(workdir: str) -> List[str]:
    """Find dependency/configuration files."""
    workdir_path = Path(workdir)
    deps = []

    dep_files = [
        "requirements.txt",
        "Pipfile",
        "Pipfile.lock",
        "pyproject.toml",
        "package.json",
        "yarn.lock",
        "package-lock.json",
        "bun.lockb",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ]

    for dep_file in dep_files:
        if (workdir_path / dep_file).exists():
            deps.append(dep_file)

    return deps


def _get_config_files_mtime(workdir: str) -> Dict[str, float]:
    """Get mtimes of config files that determine repo structure."""
    config_patterns = [
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements*.txt",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "pom.xml",
        "Dockerfile",
        "docker-compose.yml",
        ".python-version",
    ]
    workdir_path = Path(workdir)
    mtimes = {}
    for pattern in config_patterns:
        for p in workdir_path.glob(pattern):
            try:
                mtimes[str(p.relative_to(workdir_path))] = p.stat().st_mtime
            except Exception:
                pass
    return mtimes


def _get_cached_repo_summary(workdir: str) -> Optional[Dict[str, Any]]:
    """Load cached repo summary if config files haven't changed."""
    try:
        cache_dir = (
            Path(agent_context_path(Path(workdir)))
            if agent_context_path is not None
            else Path(workdir) / ".codingAgent"
        )
        cache_path = cache_dir / "repo_summary_cache.json"
        if not cache_path.exists():
            return None
        import json

        with open(cache_path, "r") as f:
            cache = json.load(f)
        current_mtimes = _get_config_files_mtime(workdir)
        cached_mtimes = cache.get("config_mtimes", {})
        if current_mtimes == cached_mtimes:
            return cache.get("summary")
    except Exception:
        pass
    return None


def _save_repo_summary_cache(workdir: str, summary: Dict[str, Any]) -> None:
    """Save repo summary to cache."""
    try:
        cache_dir = (
            Path(agent_context_path(Path(workdir)))
            if agent_context_path is not None
            else Path(workdir) / ".codingAgent"
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "repo_summary_cache.json"
        import json
        import tempfile

        cache = {
            "config_mtimes": _get_config_files_mtime(workdir),
            "summary": summary,
        }
        # Write atomically: temp file + os.replace (shutil.move fallback for cross-device).
        import shutil
        _fd, _tmp = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
        try:
            try:
                with os.fdopen(_fd, "w") as _f:
                    _fd = None  # type: ignore[assignment]  # ownership transferred to fdopen
                    json.dump(cache, _f)
            finally:
                if _fd is not None:
                    os.close(_fd)
                    _fd = None  # type: ignore[assignment]
            try:
                os.replace(_tmp, str(cache_path))
            except OSError:
                shutil.move(_tmp, str(cache_path))
        except Exception:
            try:
                os.unlink(_tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def generate_repo_summary(workdir: str) -> Dict[str, Any]:
    """Generate comprehensive repository summary.

    PB-6 fix: Cache summary based on config file mtimes. Only regenerates when
    pyproject.toml, package.json, or other config files change. Skips expensive
    filesystem walks for unchanged repos.

    Returns:
        Dict containing:
        - framework: Main framework (e.g., FastAPI, Django)
        - languages: List of programming languages
        - test_framework: Test framework used
        - entrypoints: List of entry point files
        - modules: Top-level modules
        - dependency_files: Dependency configuration files
    """
    cached = _get_cached_repo_summary(workdir)
    if cached:
        return cached

    framework = detect_framework(workdir)
    languages = detect_languages(workdir)
    test_framework = detect_test_framework(workdir)
    entrypoints = detect_entrypoints(workdir)
    modules = list_modules(workdir)
    dependency_files = find_dependency_files(workdir)

    summary = {
        "framework": framework,
        "languages": languages,
        "test_framework": test_framework,
        "entrypoints": entrypoints,
        "modules": modules,
        "dependency_files": dependency_files,
        "summary": _format_summary(framework, languages, test_framework, modules),
    }

    _save_repo_summary_cache(workdir, summary)
    return summary


def _format_summary(
    framework: Optional[str],
    languages: List[str],
    test_framework: Optional[str],
    modules: List[str],
) -> str:
    """Format summary as readable string."""
    parts = []

    if framework:
        parts.append(f"Framework: {framework}")
    if languages:
        parts.append(f"Languages: {', '.join(languages)}")
    if test_framework:
        parts.append(f"Tests: {test_framework}")
    if modules:
        parts.append(f"Modules: {', '.join(modules[:5])}")

    return " | ".join(parts) if parts else "Unknown project structure"


# Tool wrapper
def summarize_repo(workdir: str = ".") -> Dict[str, Any]:
    """Tool wrapper for repo summary."""
    try:
        summary = generate_repo_summary(workdir)
        return {"status": "ok", **summary}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---- repo_tools: read-only tools ----


@tool(tags=["coding"])
def find_files(pattern: str, workdir: str) -> Dict[str, Any]:
    """Find files in the repository matching a glob pattern.

    Supports ** for recursive matching.  Returns up to MAX_GLOB_RESULTS (10 000)
    matches.  When truncated, the result includes ``truncated: True`` and
    ``total_found`` so callers know how many results were omitted.

    Returns::

        {"status": "ok", "files": [...], "truncated": bool, "total_found": int}
    """
    try:
        base = Path(workdir).resolve()
        # Reject patterns that would escape the working directory
        if ".." in pattern:
            return {
                "status": "error",
                "error": (
                    "Pattern must not contain '..'. "
                    "Path traversal outside the working directory is not allowed."
                ),
            }
        if "**" in pattern:
            raw = base.glob(pattern)
        else:
            raw = base.rglob(pattern)

        files: list[str] = []
        for p in raw:
            if not p.is_file():
                continue
            try:
                rel = str(p.resolve().relative_to(base))
                files.append(rel)
            except ValueError:
                continue  # resolved outside base — skip

        total_found = len(files)
        truncated = total_found > MAX_GLOB_RESULTS
        files = sorted(files)[:MAX_GLOB_RESULTS]
        return {
            "status": "ok",
            "files": files,
            "truncated": truncated,
            "total_found": total_found,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"])
def search_code(query: str, workdir: str) -> Dict[str, Any]:
    """
    Performs a semantic search over the codebase.
    """
    try:
        from src.core.indexing.vector_store import VectorStore
    except ImportError:
        return {"status": "error", "error": "src.core.indexing not available"}
    if VectorStore is None:
        return {"status": "error", "error": "src.core.indexing not available"}
    try:
        vs = VectorStore(workdir)
        results = vs.search(query)
        return {"status": "ok", "results": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"])
def find_symbol(name: str, workdir: str) -> Dict[str, Any]:
    """
    Finds a symbol (class or function) by its exact name.
    """
    import json

    index_path = agent_context_path(Path(workdir)) / "repo_index.json"
    if not index_path.exists():
        return {
            "status": "error",
            "error": "Repo index not found. Run initialize_repo_intelligence first.",
        }

    with open(index_path, "r", encoding="utf-8") as f:
        repo_index = json.load(f)

    results = [s for s in repo_index["symbols"] if s["symbol_name"] == name]
    return {"status": "ok", "results": results}


@tool(tags=["coding"])
def find_references(name: str, workdir: str) -> Dict[str, Any]:
    """
    Find references to a symbol by scanning indexed files for occurrences of the symbol name.
    Uses word-boundary matching to avoid false positives (e.g. 'run' won't match 'running').
    Returns per-match line numbers and snippets.
    """
    try:
        base = Path(workdir)
        index_path = agent_context_path(base) / "repo_index.json"
        if not index_path.exists():
            return {
                "status": "error",
                "error": "Repo index not found. Run initialize_repo_intelligence first.",
            }
        with open(index_path, "r", encoding="utf-8") as f:
            repo_index = json.load(f)
        files = [f.get("path") for f in repo_index.get("files", [])]
        refs = []
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        for rel in files:
            try:
                p = _safe_resolve(str(rel), base)
            except PermissionError:
                continue
            try:
                text = p.read_text(encoding="utf-8")
                lines = text.splitlines()
                for line_num, line in enumerate(lines, 1):
                    for m in pattern.finditer(line):
                        col = m.start() + 1
                        refs.append(
                            {
                                "file": str(rel),
                                "line": line_num,
                                "col": col,
                                "snippet": line.strip(),
                            }
                        )
            except Exception:
                continue
        return {"status": "ok", "results": refs}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---- repo_analysis_tools ----


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
        repo_memory: Dict[str, Any] = {
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

        ctx = (
            agent_context_path(workdir_path)
            if agent_context_path is not None
            else workdir_path / ".codingAgent"
        )
        repo_memory_path = Path(ctx) / "repo_memory.json"
        repo_memory_path.parent.mkdir(parents=True, exist_ok=True)
        # Prefer central atomic JSON writer; fall back to write_text
        try:
            from src.core.io_utils import atomic_write_json

            ok = atomic_write_json(repo_memory_path, repo_memory)
            if ok:
                return {
                    "status": "ok",
                    "message": f"Repository analysis complete. Found {sum(d.get('file_count', 0) for d in languages.values())} files across {list(languages.keys())}.",
                    "languages": repo_memory["languages"],
                }
        except Exception:
            # Fall back to the original behaviour
            pass

        # Fallback: mkstemp -> os.replace with fsync; final fallback to Path.write_text.
        try:
            import tempfile

            _fd = None
            _tmp = None
            try:
                _fd, _tmp = tempfile.mkstemp(
                    dir=str(repo_memory_path.parent), suffix=".tmp"
                )
                try:
                    with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                        _fd = None
                        json.dump(repo_memory, _f, indent=2)
                        try:
                            _f.flush()
                            os.fsync(_f.fileno())
                        except Exception:
                            pass
                    try:
                        os.replace(_tmp, str(repo_memory_path))
                    except Exception:
                        try:
                            shutil.move(_tmp, str(repo_memory_path))
                        except Exception:
                            # Last-resort: write directly (should be rare)
                            repo_memory_path.write_text(
                                json.dumps(repo_memory, indent=2)
                            )
                except Exception:
                    if _fd is not None:
                        try:
                            os.close(_fd)
                        except Exception:
                            pass
                    raise
            except Exception as _ex:
                try:
                    if _tmp and os.path.exists(_tmp):
                        os.unlink(_tmp)
                except Exception:
                    pass
                # Surface an error to the caller rather than silently succeed
                raise
        except Exception:
            # Final fallback — attempt a direct write and report errors to caller
            try:
                repo_memory_path.write_text(json.dumps(repo_memory, indent=2))
            except Exception as _final:
                raise

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
    summary: Dict[str, Any] = {"classes": [], "functions": []}
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
