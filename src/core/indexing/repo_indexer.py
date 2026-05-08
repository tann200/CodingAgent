import ast
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    from src.tools.tools_config import get_context_dir_name as _get_ctx_dir_name
except Exception:
    def _get_ctx_dir_name() -> str:  # type: ignore[misc]
        return ".codingAgent"

try:
    from src.core.io_utils import atomic_write_json as _atomic_write_json
except Exception:
    _atomic_write_json = None  # type: ignore[assignment]

# Multi-language support
INDEX_VERSION = "3.0"  # Multi-language indexing version

# File extension to language mapping
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
}

# Language-specific patterns for function/method detection
LANGUAGE_PATTERNS = {
    "javascript": {
        "function": r"(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\(|(\w+)\s*:\s*(?:async\s*)?\()",
        "class": r"class\s+(\w+)",
    },
    "typescript": {
        "function": r"(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\(|(\w+)\s*\([^)]*\)\s*(?::\s*\w+)?\s*{)",
        "class": r"class\s+(\w+)",
    },
    "go": {
        "function": r"func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(",
        "struct": r"type\s+(\w+)\s+struct",
    },
    "rust": {
        "function": r"fn\s+(\w+)",
        "struct": r"struct\s+(\w+)",
        "impl": r"impl\s+(?:<[^>]+>\s+)?(\w+)",
    },
    "java": {
        "function": r"(?:public|private|protected)?\s+(?:static\s+)?(?:void|int|String|\w+)\s+(\w+)\s*\(",
        "class": r"class\s+(\w+)",
    },
}


def get_imports(node: ast.Module) -> List[str]:
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


def get_language(path: Path) -> Optional[str]:
    """Get the language for a file based on its extension."""
    ext = path.suffix.lower()
    return LANGUAGE_EXTENSIONS.get(ext)


def parse_file(path: Path) -> Dict[str, Any]:
    """
    Parse a file based on its language.
    For Python, uses AST. For other languages, uses regex patterns.
    """
    language = get_language(path)

    if language == "python":
        return parse_python_file(path)

    if language:
        return parse_with_regex(path, language)

    return {}


def parse_with_regex(path: Path, language: str) -> Dict[str, Any]:
    """
    Parse non-Python files using regex patterns.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return {}

    symbols = []
    patterns = LANGUAGE_PATTERNS.get(language, {})

    # Find functions/methods
    func_pattern = patterns.get("function")
    if func_pattern:
        for match in re.finditer(func_pattern, content):
            # Use lastindex to avoid IndexError on patterns with fewer than 3 groups
            name = next(
                (
                    match.group(i)
                    for i in range(1, (match.lastindex or 0) + 1)
                    if match.group(i)
                ),
                None,
            )
            if name:
                symbols.append(
                    {
                        "name": name,
                        "type": "function",
                        "language": language,
                    }
                )

    # Find classes/structs/impls - iterate over all type patterns
    for type_key in ["class", "struct", "impl"]:
        type_pattern = patterns.get(type_key)
        if not type_pattern:
            continue
        for match in re.finditer(type_pattern, content):
            name = match.group(1)
            if name:
                symbols.append(
                    {
                        "name": name,
                        "type": type_key,
                        "language": language,
                    }
                )

    return {
        "file_path": str(path),
        "language": language,
        "symbols": symbols,
    }


def compute_file_hash(path: Path) -> str:
    """Compute MD5 hash of file content for change detection."""
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def parse_python_file(path: Path) -> Dict[str, Any]:
    """
    Parses a Python file to extract file-level info, classes, functions, and imports.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            tree = ast.parse(content)
    except Exception as exc:
        # RA-VOL23-1: log the exception type and path so index gaps are diagnosable
        logger.debug(
            "parse_python_file: skipping %s (%s: %s)", path, type(exc).__name__, exc
        )
        return {}

    classes = []
    functions = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(
                {
                    "name": node.name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "docstring": ast.get_docstring(node),
                }
            )
        elif isinstance(node, ast.FunctionDef):
            functions.append(
                {
                    "name": node.name,
                    "type": "function",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "docstring": ast.get_docstring(node),
                }
            )

    return {
        "file_path": str(path),
        "imports": get_imports(tree),
        "classes": classes,
        "functions": functions,
    }


def _load_index_metadata(base_path: Path) -> Dict[str, Any]:
    """Load existing index metadata for incremental updates."""
    meta_path = base_path / _get_ctx_dir_name() / "repo_index_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_index_metadata(base_path: Path, metadata: Dict[str, Any]) -> None:
    """Save index metadata for incremental updates."""
    meta_path = base_path / _get_ctx_dir_name() / "repo_index_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    if _atomic_write_json is not None:
        if not _atomic_write_json(meta_path, metadata, logger=logger):
            logger.warning("repo_indexer: failed to write metadata to %s", meta_path)
    else:
        try:
            meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("repo_indexer: failed to write metadata to %s", meta_path)



def index_repository(workdir: str, incremental: bool = True) -> Dict[str, Any]:
    """
    Walks through the repository, parses files, and builds an index.
    Supports Python, JavaScript, TypeScript, Go, Rust, Java, and more.

    Args:
        workdir: Path to the repository to index
        incremental: If True, only re-index changed files (default: True)

    Returns:
        Repository index with files and symbols
    """

    base_path = Path(workdir)

    # Load existing index for incremental updates
    existing_index: Dict[str, Any] = {"files": [], "symbols": []}
    index_path = base_path / _get_ctx_dir_name() / "repo_index.json"
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                existing_index = json.load(f)
        except Exception:
            pass

    # Load metadata for change detection
    metadata = _load_index_metadata(base_path) if incremental else {}
    existing_hashes = metadata.get("file_hashes", {})

    # Track changes
    ignore_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".venv",
        ".codingAgent",
        ".agent-context",
    }

    # Build set of current files for all supported languages
    current_files: Dict[str, Path] = {}
    for ext in LANGUAGE_EXTENSIONS.keys():
        for path in base_path.rglob(f"*{ext}"):
            if any(part in ignore_dirs for part in path.parts):
                continue
            rel_path = str(path.relative_to(base_path))
            current_files[rel_path] = path

    # Determine files to index (new or changed)
    files_to_index: Dict[str, Path] = {}
    deleted_files = set(existing_hashes.keys()) - set(current_files.keys())

    for rel_path, path in current_files.items():
        file_hash = compute_file_hash(path)
        if (
            rel_path not in existing_hashes
            or existing_hashes.get(rel_path) != file_hash
        ):
            files_to_index[rel_path] = path

    # If incremental and nothing changed, return existing index
    if incremental and not files_to_index and not deleted_files:
        # Update metadata timestamp
        metadata["last_indexed"] = datetime.now().isoformat()
        _save_index_metadata(base_path, metadata)
        return existing_index

    # Start building index
    repo_index: Dict[str, Any] = {"files": [], "symbols": []}
    new_hashes: Dict[str, str] = {}

    # Process files to index
    for rel_path, path in files_to_index.items():
        language = get_language(path)
        file_data = parse_file(path)

        if file_data:
            file_entry = {"path": rel_path, "language": language}

            # Add imports for Python files
            if "imports" in file_data:
                file_entry["imports"] = file_data["imports"]

            repo_index["files"].append(file_entry)

            # Add symbols
            if "classes" in file_data:
                for class_data in file_data["classes"]:
                    repo_index["symbols"].append(
                        {
                            "symbol_name": class_data["name"],
                            "symbol_type": "class",
                            "file_path": rel_path,
                            "language": language,
                        }
                    )

            if "functions" in file_data:
                for func_data in file_data["functions"]:
                    repo_index["symbols"].append(
                        {
                            "symbol_name": func_data["name"],
                            "symbol_type": "function",
                            "file_path": rel_path,
                            "language": language,
                        }
                    )

            # For regex-parsed files
            if "symbols" in file_data:
                for sym in file_data["symbols"]:
                    repo_index["symbols"].append(
                        {
                            "symbol_name": sym["name"],
                            "symbol_type": sym["type"],
                            "file_path": rel_path,
                            "language": language,
                        }
                    )

            new_hashes[rel_path] = compute_file_hash(path)

    # Merge with existing index for unchanged files
    existing_files_set = set(current_files.keys()) - set(files_to_index.keys())
    existing_file_data = {f["path"]: f for f in existing_index.get("files", [])}
    existing_symbols_by_file = {}
    for sym in existing_index.get("symbols", []):
        fp = sym.get("file_path")
        if fp:
            if fp not in existing_symbols_by_file:
                existing_symbols_by_file[fp] = []
            existing_symbols_by_file[fp].append(sym)

    for rel_path in existing_files_set:
        if rel_path in existing_file_data:
            repo_index["files"].append(existing_file_data[rel_path])
        if rel_path in existing_symbols_by_file:
            repo_index["symbols"].extend(existing_symbols_by_file[rel_path])

    # Add hashes for unchanged files
    for rel_path in existing_files_set:
        if rel_path in existing_hashes:
            new_hashes[rel_path] = existing_hashes[rel_path]

    # Save updated index
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(repo_index, f, indent=2)

    # Save metadata for next incremental update
    metadata = {
        "index_version": INDEX_VERSION,
        "last_indexed": datetime.now().isoformat(),
        "file_count": len(repo_index["files"]),
        "symbol_count": len(repo_index["symbols"]),
        "file_hashes": new_hashes,
        "incremental": incremental,
        "files_indexed": len(files_to_index),
        "files_deleted": len(deleted_files),
    }
    _save_index_metadata(base_path, metadata)

    return repo_index


def get_index_stats(workdir: str) -> Dict[str, Any]:
    """Get statistics about the current index."""
    base_path = Path(workdir)
    meta_path = base_path / _get_ctx_dir_name() / "repo_index_meta.json"
    index_path = base_path / _get_ctx_dir_name() / "repo_index.json"

    stats = {
        "index_exists": index_path.exists(),
        "indexed_files": 0,
        "indexed_symbols": 0,
        "last_indexed": None,
        "index_version": None,
        "is_incremental": False,
    }

    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                stats["last_indexed"] = meta.get("last_indexed")
                stats["index_version"] = meta.get("index_version")
                stats["is_incremental"] = meta.get("incremental", False)
                stats["files_indexed_last"] = meta.get("files_indexed", 0)
                stats["files_deleted_last"] = meta.get("files_deleted", 0)
        except Exception:
            pass

    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
                stats["indexed_files"] = len(idx.get("files", []))
                stats["indexed_symbols"] = len(idx.get("symbols", []))
        except Exception:
            pass

    return stats


def force_full_reindex(workdir: str) -> Dict[str, Any]:
    """Force a full reindex ignoring incremental updates."""
    return index_repository(workdir, incremental=False)


def get_symbols_for_task(
    workdir: str, task: str, max_results: int = 5
) -> List[Dict[str, Any]]:
    """RA-1: Return symbols relevant to *task* from the repo index.

    Extracts candidate identifier tokens from *task* (camelCase split, snake_case
    split, plain words ≥ 4 chars) then matches them against indexed symbols.
    Falls back to an empty list on any error so callers never need to handle
    exceptions.

    Parameters
    ----------
    workdir:
        Repository root to load index from.
    task:
        Natural-language task description (e.g. "refactor authentication module").
    max_results:
        Maximum number of symbol hits to return.

    Returns
    -------
    list of dicts, each with keys: ``name``, ``type``, ``file_path``,
    and optionally ``start_line`` / ``docstring``.
    """
    try:
        index_path = Path(workdir) / _get_ctx_dir_name() / "repo_index.json"
        if not index_path.exists():
            # RA-VOL22-1: Log explicitly so developers can distinguish "no matches"
            # from "index not yet built" when fast-path tasks return empty symbols.
            logger.debug(
                "get_symbols_for_task: repo_index.json not found at %s — "
                "run analysis_node or index_repository() to build it",
                index_path,
            )
            return []
        with open(index_path, "r", encoding="utf-8") as _f:
            repo_index = json.load(_f)

        # Extract candidate tokens from task description.
        # Split on whitespace, underscores, camelCase boundaries, and non-word chars.
        raw_tokens: List[str] = re.split(r"[\s_\-/\\]+", task.lower())
        tokens: List[str] = []
        for tok in raw_tokens:
            # Split camelCase: "authModule" → ["auth", "module"]
            parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", tok).lower().split()
            tokens.extend(p for p in parts if len(p) >= 4)
        token_set = set(tokens)

        if not token_set:
            return []

        hits: List[Dict[str, Any]] = []
        # BUG FIX (CODE_QUALITY_AUDIT #8): symbols are stored in the top-level
        # repo_index["symbols"] flat list (keys: symbol_name, symbol_type,
        # file_path), NOT nested under individual file entries.  The previous
        # code iterated repo_index["files"] and called file_entry.get("symbols")
        # which always returned [] — effectively making this function a no-op.
        for sym in repo_index.get("symbols", []):
            sym_name = (sym.get("symbol_name") or sym.get("name") or "").lower()
            file_path = sym.get("file_path", "")
            # A symbol matches if any task token appears in the symbol name.
            if any(t in sym_name for t in token_set):
                hits.append(
                    {
                        "name": sym.get("symbol_name") or sym.get("name"),
                        "type": sym.get("symbol_type") or sym.get("type"),
                        "file_path": file_path,
                        "start_line": sym.get("start_line"),
                        "docstring": sym.get("docstring"),
                    }
                )
                if len(hits) >= max_results:
                    return hits
        return hits
    except Exception:
        return []


if __name__ == "__main__":
    # Example usage
    # Replace '.' with your repository path
    index = index_repository(".")
    print(f"Indexed {len(index['files'])} files and {len(index['symbols'])} symbols.")

    stats = get_index_stats(".")
    print(f"Last indexed: {stats.get('last_indexed')}")
