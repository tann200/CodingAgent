from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Dict, Any
import re


def grep(pattern: str, path: str = ".", workdir: Any = None) -> Dict[str, Any]:
    """
    Performs a grep search in the specified path. Tries the system grep binary first; falls back
    to a pure-Python implementation if grep isn't available.
    """
    # Handle both Path and string workdir
    if workdir is None:
        workdir = Path.cwd()
    elif isinstance(workdir, str):
        workdir = Path(workdir)

    # For security, ensure the search is constrained to the workdir
    search_path = (workdir / path).resolve()
    if not str(search_path).startswith(str(workdir.resolve())):
        return {"error": "Search path is outside the working directory."}

    try:
        process = subprocess.run(
            ["grep", "-r", "-n", pattern, str(search_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode == 0:
            return {"output": process.stdout}
        elif process.returncode == 1:
            return {"output": "Pattern not found."}
        else:
            # If grep failed for reasons other than 'not found', fall back to Python
            # but include stderr for debugging
            # fall through to python fallback
            pass
    except FileNotFoundError:
        # fallback to python implementation below
        pass
    except Exception:
        # any other subprocess problem -> fallback
        pass

    # Python fallback implementation
    try:
        out_lines = []
        regex = re.compile(pattern)
        for p in search_path.rglob("*"):
            if p.is_file():
                try:
                    with p.open("r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh, start=1):
                            if regex.search(line):
                                rel = (
                                    str(p.relative_to(search_path))
                                    if p.exists()
                                    else str(p)
                                )
                                out_lines.append(f"{rel}:{i}:{line.rstrip()}")
                except Exception:
                    # ignore unreadable files
                    continue
        if out_lines:
            return {"output": "\n".join(out_lines)}
        else:
            return {"output": "Pattern not found."}
    except Exception as e:
        return {"error": str(e)}


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
            return {"error": process.stderr}
    except FileNotFoundError:
        return {
            "error": "git command not found. Please ensure it is installed and in your PATH."
        }
    except Exception as e:
        return {"error": str(e)}


def summarize_structure(
    path: str = ".", workdir: Path = Path.cwd(), max_entries: int = 50
) -> Dict[str, Any]:
    """
    Summarize the workspace structure at `path` relative to `workdir`.
    Returns counts and a short listing of top entries (name, is_dir, size_bytes).
    """
    try:
        root = (workdir / path).resolve()
        # ensure inside workdir
        if not str(root).startswith(str(workdir.resolve())):
            return {"error": "Path outside working directory is not allowed"}
        file_count = 0
        dir_count = 0
        total_size = 0
        entries = []
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
        return {"error": str(e)}


def summarize_structure(workdir: Any = None) -> Dict[str, Any]:
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
        return {"error": str(e)}

    return summary
