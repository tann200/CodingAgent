from __future__ import annotations

from pathlib import Path
from typing import Dict, Any


# Default working directory used by tools (project root /output)
DEFAULT_WORKDIR = Path.cwd() / 'output'
DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p
    p = p.resolve()
    # ensure we are inside workdir
    if not str(p).startswith(str(workdir.resolve())):
        raise PermissionError('Path outside working directory is not allowed')
    return p


def write_file(path: str, content: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')
    return {"path": str(p), "status": "ok"}


def read_file(path: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}
    return {"path": str(p), "status": "ok", "content": p.read_text(encoding='utf-8')}


def list_dir(path: str = '.', workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    items = []
    for child in p.iterdir():
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


def delete_file(path: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    try:
        p = _safe_resolve(path, workdir)
        if not p.exists():
            return {"path": str(p), "status": "not_found"}
        if p.is_dir():
            import shutil
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"path": str(p), "status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def sandbox_info(workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    return {"workdir": str(workdir.resolve())}

def read_file_chunk(path: str, offset: int = 0, limit: int = -1, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}
    
    with p.open('r', encoding='utf-8') as f:
        f.seek(offset)
        content = f.read(limit)
        return {"path": str(p), "status": "ok", "content": content, "offset": offset, "limit": limit}

def edit_file(path: str, patch: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    import subprocess
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile('w', suffix='.patch', delete=False) as f:
        f.write(patch)
        patch_file = f.name
        
    try:
        if not patch.strip().startswith('---') and not patch.strip().startswith('@@'):
            return {"path": str(p), "status": "error", "error": "Invalid patch format. Must be unified diff."}

        # Apply unified diff. 
        # Using -f to force (ignore previous patches) and -u (unified)
        result = subprocess.run(
            ['patch', '-u', '-f', str(p), '-i', patch_file],
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode != 0:
            return {"path": str(p), "status": "error", "error": f"Patch failed code {result.returncode}:\n{result.stdout}\n{result.stderr}"}
        
        return {"path": str(p), "status": "ok"}
    finally:
        try:
            os.remove(patch_file)
        except OSError:
            pass

