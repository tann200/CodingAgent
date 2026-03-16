from __future__ import annotations
import ast
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from datetime import datetime

logger = logging.getLogger(__name__)


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
        if not p.exists() or p.suffix != ".py":
            return

        current_hash = self._get_file_hash(p)

        if p in self.file_hashes and self.file_hashes[p] == current_hash:
            logger.debug(f"File unchanged: {path}")
            return

        logger.info(f"Updating symbol graph for: {path}")
        symbols = self._parse_file(p)

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
        """Find functions that call a given function."""
        results = []
        for file_path, data in self.nodes.items():
            for func in data.get("symbols", {}).get("functions", []):
                if func["name"] == function_name:
                    results.append({"file": file_path, "line": func["line"]})
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

        for path in root.rglob("*.py"):
            if ".agent-context" in str(path) or "__pycache__" in str(path):
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
            if Path(file_path).suffix == ".py":
                self.symbol_graph.update_file(file_path)
        else:
            for path in self.workdir.rglob("*.py"):
                if ".agent-context" in str(path) or "__pycache__" in str(path):
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
