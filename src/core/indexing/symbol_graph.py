from __future__ import annotations
import ast
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Multi-language regex patterns (#36)
# Each entry maps suffix → {"function": pattern, "class": pattern}
# Capture group 1 must be the symbol name.
# ---------------------------------------------------------------------------
_LANG_PATTERNS: Dict[str, Dict[str, re.Pattern]] = {
    # JavaScript / TypeScript
    ".js": {
        "function": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            re.MULTILINE,
        ),
        "class": re.compile(
            r"(?:^|\s)(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            re.MULTILINE,
        ),
    },
    ".ts": {
        "function": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[<(]",
            re.MULTILINE,
        ),
        "class": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            re.MULTILINE,
        ),
    },
    ".tsx": {  # same as .ts
        "function": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[<(]",
            re.MULTILINE,
        ),
        "class": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            re.MULTILINE,
        ),
    },
    ".jsx": {  # same as .js
        "function": re.compile(
            r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            re.MULTILINE,
        ),
        "class": re.compile(
            r"(?:^|\s)(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            re.MULTILINE,
        ),
    },
    # Go
    ".go": {
        "function": re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE),
        "class": re.compile(r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+struct\b", re.MULTILINE),
    },
    # Rust
    ".rs": {
        "function": re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]", re.MULTILINE),
        "class": re.compile(r"^(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
    },
    # Java
    ".java": {
        "function": re.compile(
            r"(?:public|protected|private|static|\s)+[\w<>\[\]]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            re.MULTILINE,
        ),
        "class": re.compile(
            r"(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        ),
    },
}

# All supported suffixes (Python handled separately via AST)
_SUPPORTED_SUFFIXES = {".py"} | set(_LANG_PATTERNS.keys())

_SKIP_DIRS = {".agent-context", "__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build"}


class SymbolGraph:
    """Graph-based code symbol index with incremental updates."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.graph_path = self.workdir / ".agent-context" / "symbol_graph.json"
        self._load_graph()

    def _load_graph(self):
        """Load existing symbol graph or create new."""
        if self.graph_path.exists():
            try:
                with open(self.graph_path, "r") as f:
                    data = json.load(f)
                    self.nodes = data.get("nodes", {})
                    self.edges = data.get("edges", [])
                    self.file_hashes = data.get("file_hashes", {})
            except Exception:
                self._init_empty()
        else:
            self._init_empty()

    def _init_empty(self):
        """Initialize empty graph."""
        self.nodes = {}
        self.edges = []
        self.file_hashes = {}

    def _save_graph(self):
        """Persist graph to disk."""
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.graph_path, "w") as f:
            json.dump(
                {
                    "nodes": self.nodes,
                    "edges": self.edges,
                    "file_hashes": self.file_hashes,
                    "updated_at": datetime.now().isoformat(),
                },
                f,
                indent=2,
            )

    def _get_file_hash(self, path: Path) -> str:
        """Get hash of file content."""
        if not path.exists():
            return ""
        return hashlib.md5(path.read_bytes()).hexdigest()

    @staticmethod
    def _strip_comments(source: str, suffix: str) -> str:
        """Strip comments from source before regex matching to avoid false positives.

        Removes single-line (``// ...``) and block (``/* ... */``) comments for
        C-family languages.  String literals that happen to contain comment-like
        patterns are not perfectly protected, but the vast majority of
        false-positive symbols (e.g. commented-out function definitions) are
        eliminated.
        """
        if suffix in {".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rs"}:
            # Strip block comments /* ... */ — replace with same number of
            # newlines so that line numbers for subsequent symbols are preserved.
            source = re.sub(
                r"/\*.*?\*/",
                lambda m: "\n" * m.group(0).count("\n"),
                source,
                flags=re.DOTALL,
            )
            # Strip single-line comments // ... (replace with blank; newline kept
            # by the pattern boundary [^\n]*)
            source = re.sub(r"//[^\n]*", "", source)
        elif suffix in {".py"}:
            # Python uses AST — no stripping needed
            pass
        return source

    def _parse_file_regex(self, path: Path) -> Dict[str, Any]:
        """Extract symbols from non-Python files using language-specific regex patterns.

        Comments are stripped before matching (RA-2 fix) so that commented-out
        function/class definitions and signatures inside doc-strings do not produce
        false-positive symbols.
        """
        patterns = _LANG_PATTERNS.get(path.suffix)
        if not patterns:
            return {"classes": [], "functions": [], "imports": [], "docstring": ""}
        try:
            raw_source = path.read_text(encoding="utf-8", errors="ignore")
            # Strip comments before regex matching to avoid false positives on
            # commented-out definitions (RA-2).  _strip_comments preserves
            # newlines so line numbers in the stripped text match the original.
            source = self._strip_comments(raw_source, path.suffix)
            functions = [
                {"name": m.group(1), "line": source[: m.start()].count("\n") + 1, "args": [], "docstring": ""}
                for m in patterns["function"].finditer(source)
            ]
            classes = [
                {"name": m.group(1), "line": source[: m.start()].count("\n") + 1, "bases": [], "methods": [], "docstring": ""}
                for m in patterns["class"].finditer(source)
            ]
            return {"classes": classes, "functions": functions, "imports": [], "docstring": ""}
        except Exception as e:
            logger.warning(f"Failed to parse {path}: {e}")
            return {"classes": [], "functions": [], "imports": [], "docstring": ""}

    def _parse_file(self, path: Path) -> Dict[str, Any]:
        """Extract symbols from Python file using AST."""
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)

            symbols = {
                "classes": [],
                "functions": [],
                "imports": [],
                "docstring": ast.get_docstring(tree) or "",
            }

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    symbols["classes"].append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "bases": [
                                b.attr
                                if isinstance(b, ast.Attribute)
                                else (b.id if isinstance(b, ast.Name) else str(b))
                                for b in node.bases
                            ],
                            "methods": [
                                n.name
                                for n in node.body
                                if isinstance(n, ast.FunctionDef)
                            ],
                            "docstring": ast.get_docstring(node) or "",
                        }
                    )
                elif isinstance(node, ast.FunctionDef):
                    symbols["functions"].append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "args": [a.arg for a in node.args.args],
                            "docstring": ast.get_docstring(node) or "",
                        }
                    )
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            symbols["imports"].append(alias.name)
                    elif node.module:
                        for alias in node.names:
                            symbols["imports"].append(
                                f"{node.module}.{alias.name}"
                                if alias.name
                                else node.module
                            )

            return symbols
        except Exception as e:
            logger.warning(f"Failed to parse {path}: {e}")
            return {"classes": [], "functions": [], "imports": [], "docstring": ""}

    def update_file(self, path: str):
        """Update symbols for a single file."""
        p = Path(path)
        if not p.exists() or p.suffix not in _SUPPORTED_SUFFIXES:
            return

        current_hash = self._get_file_hash(p)

        if str(p) in self.file_hashes and self.file_hashes[str(p)] == current_hash:
            logger.debug(f"File unchanged: {path}")
            return

        logger.info(f"Updating symbol graph for: {path}")
        symbols = self._parse_file(p) if p.suffix == ".py" else self._parse_file_regex(p)

        rel_path = str(p.relative_to(self.workdir))
        self.nodes[rel_path] = {
            "symbols": symbols,
            "updated_at": datetime.now().isoformat(),
        }
        self.file_hashes[str(p)] = current_hash

        self._update_edges(rel_path, symbols)
        self._save_graph()

    def _update_edges(self, file_path: str, symbols: Dict):
        """Update import edges."""
        self.edges = [e for e in self.edges if e.get("from") != file_path]

        for imp in symbols.get("imports", []):
            self.edges.append({"from": file_path, "to": imp, "type": "imports"})

    def remove_file(self, path: str):
        """Remove file from graph."""
        p = str(Path(path).relative_to(self.workdir))
        if p in self.nodes:
            del self.nodes[p]
        if p in self.file_hashes:
            del self.file_hashes[p]
        self.edges = [e for e in self.edges if e.get("from") != p]
        self._save_graph()

    def find_calls(self, function_name: str) -> List[Dict]:
        """Find call sites where function_name is called.

        Searches file contents (text scan) for occurrences of `function_name(` to
        locate actual call sites, excluding function definitions.
        """
        import re

        results = []
        call_pattern = re.compile(r"\b" + re.escape(function_name) + r"\s*\(")
        # Pattern to match function definitions (def function_name(...)
        def_pattern = re.compile(
            r"^\s*(?:async\s+)?def\s+" + re.escape(function_name) + r"\s*\("
        )
        for file_path in self.nodes.keys():
            try:
                p = Path(file_path)
                if not p.is_absolute():
                    p = self.workdir / p
                if not p.exists():
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), start=1):
                    # Skip the function definition itself
                    if def_pattern.match(line):
                        continue
                    if call_pattern.search(line):
                        results.append(
                            {
                                "file": file_path,
                                "line": i,
                                "snippet": line.strip(),
                            }
                        )
            except Exception:
                continue
        return results

    def find_definitions(self, function_name: str) -> List[Dict]:
        """Find definition sites for a given function or class name."""
        results = []
        for file_path, data in self.nodes.items():
            for func in data.get("symbols", {}).get("functions", []):
                if func["name"] == function_name:
                    results.append({"file": file_path, "line": func["line"]})
            for cls in data.get("symbols", {}).get("classes", []):
                if cls["name"] == function_name:
                    results.append({"file": file_path, "line": cls["line"]})
        return results

    def find_tests_for_module(self, module_name: str) -> List[Dict]:
        """Find test files for a given module."""
        results = []
        test_patterns = ["test_", "_test.py", "tests/"]

        for file_path in self.nodes.keys():
            if any(p in file_path for p in test_patterns):
                if (
                    module_name in file_path
                    or module_name.replace(".py", "") in file_path
                ):
                    results.append({"file": file_path})

        return results

    def get_symbol_at_line(self, file_path: str, line: int) -> Optional[Dict]:
        """Get symbol at a specific line."""
        if file_path not in self.nodes:
            return None

        symbols = self.nodes[file_path].get("symbols", {})

        for cls in symbols.get("classes", []):
            if cls.get("line") == line:
                return {"type": "class", "name": cls["name"]}

        for func in symbols.get("functions", []):
            if func.get("line") == line:
                return {"type": "function", "name": func["name"]}

        return None

    def get_all_symbols(self) -> Dict[str, List[str]]:
        """Get all symbols in the project."""
        all_symbols = {}

        for file_path, data in self.nodes.items():
            for cls in data.get("symbols", {}).get("classes", []):
                all_symbols[cls["name"]] = {"type": "class", "file": file_path}
            for func in data.get("symbols", {}).get("functions", []):
                all_symbols[func["name"]] = {"type": "function", "file": file_path}

        return all_symbols

    def rebuild_index(self, root_path: str = None):
        """Full rebuild of the symbol index."""
        root = Path(root_path) if root_path else self.workdir

        logger.info(f"Rebuilding symbol graph from {root}")
        self._init_empty()

        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _SUPPORTED_SUFFIXES:
                continue
            if any(skip in path.parts for skip in _SKIP_DIRS):
                continue
            self.update_file(str(path))

        logger.info(f"Symbol graph rebuilt: {len(self.nodes)} files indexed")


class IncrementalIndexer:
    """Watches filesystem and incrementally updates symbol graph."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.symbol_graph = SymbolGraph(workdir=str(self.workdir))
        self._last_modified: Dict[str, float] = {}

    def check_and_update(self, file_path: str = None):
        """Check for changes and update index."""
        if file_path:
            if Path(file_path).suffix in _SUPPORTED_SUFFIXES:
                self.symbol_graph.update_file(file_path)
        else:
            for path in self.workdir.rglob("*"):
                if not path.is_file() or path.suffix not in _SUPPORTED_SUFFIXES:
                    continue
                if any(skip in path.parts for skip in _SKIP_DIRS):
                    continue

                try:
                    mtime = path.stat().st_mtime
                    key = str(path)

                    if (
                        key not in self._last_modified
                        or self._last_modified[key] != mtime
                    ):
                        self._last_modified[key] = mtime
                        self.symbol_graph.update_file(str(path))
                except Exception:
                    pass

    def get_symbol_info(self, name: str) -> Optional[Dict]:
        """Get information about a symbol."""
        all_symbols = self.symbol_graph.get_all_symbols()
        return all_symbols.get(name)

    def find_references(self, symbol_name: str) -> List[Dict]:
        """Find all references to a symbol."""
        return self.symbol_graph.find_calls(symbol_name)
