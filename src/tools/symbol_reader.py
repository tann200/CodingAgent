"""
Symbol-based file reading tools.

Enables function-level and class-level code reading for better context management.
"""

import ast
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


@dataclass
class SymbolLocation:
    """Location of a symbol in a file."""

    name: str
    type: str  # function, class, method, constant
    start_line: int
    end_line: int
    file_path: str


class SymbolReader:
    """Read code at symbol/function/class level."""

    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir)

    def parse_symbols(self, file_path: str) -> List[SymbolLocation]:
        """Parse file and extract all symbol definitions."""
        try:
            resolved = self._resolve_path(file_path)
            content = resolved.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(resolved))

            symbols = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        SymbolLocation(
                            name=node.name,
                            type="function"
                            if not node.name.startswith("_")
                            else "private_function",
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            file_path=str(resolved),
                        )
                    )
                elif isinstance(node, ast.ClassDef):
                    symbols.append(
                        SymbolLocation(
                            name=node.name,
                            type="class",
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            file_path=str(resolved),
                        )
                    )
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            symbols.append(
                                SymbolLocation(
                                    name=target.id,
                                    type="constant",
                                    start_line=node.lineno,
                                    end_line=node.end_lineno or node.lineno,
                                    file_path=str(resolved),
                                )
                            )

            return symbols
        except Exception:
            return []

    def read_symbol(
        self, file_path: str, symbol_name: str, context_lines: int = 5
    ) -> Dict[str, Any]:
        """Read a specific symbol's code."""
        resolved = self._resolve_path(file_path)
        content = resolved.read_text(encoding="utf-8")
        lines = content.split("\n")

        symbols = self.parse_symbols(file_path)

        # Find matching symbol
        for sym in symbols:
            if sym.name == symbol_name:
                start = max(0, sym.start_line - 1 - context_lines)
                end = min(len(lines), (sym.end_line or sym.start_line) + context_lines)
                code = "\n".join(lines[start:end])

                return {
                    "status": "ok",
                    "symbol": symbol_name,
                    "type": sym.type,
                    "start_line": sym.start_line,
                    "end_line": sym.end_line,
                    "code": code,
                    "file_path": str(resolved),
                }

        return {
            "status": "error",
            "error": f"Symbol '{symbol_name}' not found in {file_path}",
        }

    def read_function(self, file_path: str, function_name: str) -> Dict[str, Any]:
        """Read a specific function's code."""
        return self.read_symbol(file_path, function_name)

    def read_class(self, file_path: str, class_name: str) -> Dict[str, Any]:
        """Read a specific class's code."""
        return self.read_symbol(file_path, class_name)

    def read_file_lines(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read file with optional line range."""
        try:
            resolved = self._resolve_path(file_path)
            content = resolved.read_text(encoding="utf-8")
            lines = content.split("\n")

            if start_line is not None and end_line is not None:
                start = max(0, start_line - 1)
                end = min(len(lines), end_line)
                code = "\n".join(lines[start:end])
            elif start_line is not None:
                start = max(0, start_line - 1)
                code = "\n".join(lines[start:])
            elif end_line is not None:
                end = min(len(lines), end_line)
                code = "\n".join(lines[:end])
            else:
                code = content

            return {
                "status": "ok",
                "content": code,
                "start_line": start_line or 1,
                "end_line": end_line or len(lines),
                "total_lines": len(lines),
                "file_path": str(resolved),
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
            }

    def list_symbols(self, file_path: str) -> Dict[str, Any]:
        """List all symbols in a file."""
        symbols = self.parse_symbols(file_path)

        return {
            "status": "ok",
            "file_path": file_path,
            "symbols": [
                {
                    "name": s.name,
                    "type": s.type,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                }
                for s in symbols
            ],
            "count": len(symbols),
        }

    def find_symbol(
        self, symbol_name: str, search_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Find a symbol across multiple files."""
        if search_paths is None:
            search_paths = list(self.workdir.rglob("*.py"))

        for file_path in search_paths:
            try:
                symbols = self.parse_symbols(str(file_path))
                for sym in symbols:
                    if sym.name == symbol_name:
                        return {
                            "status": "ok",
                            "found": True,
                            "symbol": symbol_name,
                            "file_path": str(file_path),
                            "start_line": sym.start_line,
                            "end_line": sym.end_line,
                            "type": sym.type,
                        }
            except Exception:
                continue

        return {
            "status": "ok",
            "found": False,
            "symbol": symbol_name,
            "error": f"Symbol '{symbol_name}' not found",
        }

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve file path relative to workdir."""
        p = Path(file_path)
        if p.is_absolute():
            return p
        return (self.workdir / p).resolve()


# Tool wrappers for integration
def read_file_chunked(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    symbol: Optional[str] = None,
    workdir: Path = None,
) -> Dict[str, Any]:
    """Read file with optional line range or symbol selection."""
    if workdir is None:
        workdir = Path.cwd()

    reader = SymbolReader(str(workdir))

    if symbol:
        return reader.read_symbol(path, symbol)
    elif start_line or end_line:
        return reader.read_file_lines(path, start_line, end_line)
    else:
        # Full file read
        resolved = (workdir / path).resolve()
        try:
            content = resolved.read_text(encoding="utf-8")
            return {"status": "ok", "content": content}
        except Exception as e:
            return {"status": "error", "error": str(e)}


def list_file_symbols(path: str, workdir: Path = None) -> Dict[str, Any]:
    """List all symbols in a file."""
    if workdir is None:
        workdir = Path.cwd()

    reader = SymbolReader(str(workdir))
    return reader.list_symbols(path)


def find_symbol_global(symbol_name: str, workdir: Path = None) -> Dict[str, Any]:
    """Find symbol across entire repository."""
    if workdir is None:
        workdir = Path.cwd()

    reader = SymbolReader(str(workdir))
    return reader.find_symbol(symbol_name)
