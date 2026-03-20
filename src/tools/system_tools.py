from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Dict, Any
import re

from src.tools._path_utils import safe_resolve as _safe_resolve


def _parse_grep_output(raw: str) -> list:
    """Parse grep -r -n output into structured dicts. Context separator lines are skipped."""
    results = []
    for line in raw.splitlines():
        # grep -n format: path:line_number:content
        # Skip binary-file notices and context separator lines (--)
        if line.startswith("--") or line.startswith("Binary file"):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3:
            try:
                results.append({
                    "file_path": parts[0],
                    "line_number": int(parts[1]),
                    "content": parts[2],
                })
            except (ValueError, IndexError):
                pass
    return results


def grep(
    pattern: str,
    path: str = ".",
    workdir: Any = None,
    include: str = "",
    context: int = 0,
) -> Dict[str, Any]:
    """
    Regex search in the specified path.

    Args:
        pattern:  Regular expression to search for.
        path:     Directory or file to search within (relative to workdir).
        workdir:  Root directory (default: cwd).
        include:  Optional file-glob filter passed to --include (e.g. "*.py").
        context:  Number of lines of context to show before and after each match.

    Returns a dict with:
        output   - raw grep output string
        matches  - structured list of {file_path, line_number, content} dicts
                   (only the matching lines, not context lines)
    """
    if workdir is None:
        workdir = Path.cwd()
    elif isinstance(workdir, str):
        workdir = Path(workdir)

    try:
        search_path = _safe_resolve(path, workdir)
    except PermissionError as e:
        return {"status": "error", "error": str(e)}

    try:
        cmd = ["grep", "-r", "-n", pattern]
        if context > 0:
            cmd += ["-C", str(context)]
        if include:
            cmd += [f"--include={include}"]
        cmd.append(str(search_path))

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if process.returncode == 0:
            raw = process.stdout
            return {"status": "ok", "output": raw, "matches": _parse_grep_output(raw)}
        elif process.returncode == 1:
            return {"status": "ok", "output": "Pattern not found.", "matches": []}
        else:
            pass  # fall through to Python fallback
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "grep timed out after 30 seconds."}
    except FileNotFoundError:
        pass  # grep binary not available — use Python fallback
    except Exception:
        pass

    # Python fallback
    try:
        suffix_filter = None
        if include and include.startswith("*."):
            suffix_filter = include[1:]  # e.g. ".py"

        regex = re.compile(pattern)
        out_lines: list[str] = []
        matches: list[dict] = []

        for p in search_path.rglob("*"):
            if not p.is_file():
                continue
            if suffix_filter and p.suffix != suffix_filter:
                continue
            try:
                with p.open("r", encoding="utf-8", errors="ignore") as fh:
                    file_lines = fh.readlines()
                rel = str(p.relative_to(search_path)) if p.exists() else str(p)
                for i, line in enumerate(file_lines):
                    if regex.search(line):
                        matches.append({
                            "file_path": rel,
                            "line_number": i + 1,
                            "content": line.rstrip(),
                        })
                        start = max(0, i - context)
                        end = min(len(file_lines), i + context + 1)
                        for j in range(start, end):
                            out_lines.append(f"{rel}:{j + 1}:{file_lines[j].rstrip()}")
            except Exception:
                continue

        if matches:
            return {"status": "ok", "output": "\n".join(out_lines), "matches": matches}
        return {"status": "ok", "output": "Pattern not found.", "matches": []}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_git_diff() -> Dict[str, Any]:
    """
    Gets the git diff of the current repository.
    """
    try:
        process = subprocess.run(
            ["git", "diff"], capture_output=True, text=True, check=False
        )
        if process.returncode == 0:
            return {"diff": process.stdout}
        else:
            return {"status": "error", "error": process.stderr}
    except FileNotFoundError:
        return {
            "error": "git command not found. Please ensure it is installed and in your PATH."
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def summarize_structure(
    path: str = ".", workdir: Path = Path.cwd(), max_entries: int = 50
) -> Dict[str, Any]:
    """
    Summarize the workspace structure at `path` relative to `workdir`.
    Returns counts and a short listing of top entries (name, is_dir, size_bytes).
    """
    try:
        try:
            root = _safe_resolve(path, workdir)
        except PermissionError as e:
            return {"status": "error", "error": str(e)}
        file_count = 0
        dir_count = 0
        total_size = 0
        for p in root.rglob("*"):
            try:
                if p.is_dir():
                    dir_count += 1
                else:
                    file_count += 1
                    try:
                        total_size += p.stat().st_size
                    except Exception:
                        pass
            except Exception:
                continue
        # top-level listing
        top = []
        for child in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name))[
            :max_entries
        ]:
            try:
                size = child.stat().st_size if child.is_file() else 0
            except Exception:
                size = 0
            top.append({"name": child.name, "is_dir": child.is_dir(), "size": size})
        return {
            "path": str(root),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_size": total_size,
            "top": top,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def summarize_structure_detailed(workdir: Any = None) -> Dict[str, Any]:
    """Provide a high-level summary of the workspace structure."""
    if workdir is None:
        workdir = Path.cwd()
    elif isinstance(workdir, str):
        workdir = Path(workdir)

    root = workdir.resolve()

    summary = {
        "root": str(root),
        "total_files": 0,
        "total_dirs": 0,
        "by_extension": {},
        "largest_files": [],
        "subdirs": [],
    }

    file_sizes = []

    try:
        for p in root.rglob("*"):
            if (
                ".agent-context" in str(p)
                or "__pycache__" in str(p)
                or ".venv" in str(p)
            ):
                continue

            try:
                if p.is_dir():
                    summary["total_dirs"] += 1
                elif p.is_file():
                    summary["total_files"] += 1
                    size = p.stat().st_size
                    ext = p.suffix or "no_extension"
                    summary["by_extension"][ext] = (
                        summary["by_extension"].get(ext, 0) + 1
                    )
                    file_sizes.append((str(p.relative_to(root)), size))
            except Exception:
                continue

        file_sizes.sort(key=lambda x: x[1], reverse=True)
        summary["largest_files"] = [
            {"path": f[0], "size": f[1]} for f in file_sizes[:5]
        ]

        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                summary["subdirs"].append(d.name)

    except Exception as e:
        return {"status": "error", "error": str(e)}

    return summary
