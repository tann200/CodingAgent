from typing import Optional
"""Consolidated system map generator.
- Outputs an ASCII tree to docs/system_map.md with a UTC generation timestamp.
- Excludes: audit/, .agent-context/, patterns from .gitignore, and ALWAYS_EXCLUDE
- Always includes scripts/ folder contents
- Also writes optional JSON tree to scripts/tree.json for other tools

Usage: python scripts/generate_system_map.py
"""
from pathlib import Path
import fnmatch
import json
from datetime import datetime

# Repository paths and outputs
REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_MD = REPO_ROOT / 'docs' / 'system_map.md'
OUT_JSON = REPO_ROOT / 'scripts' / 'tree.json'
GITIGNORE = REPO_ROOT / '.gitignore'

# Top-level names to always exclude from the system map
ALWAYS_EXCLUDE = {
    '.git',
    '.venv',
    '__pycache__',
    '.idea',
    'node_modules',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    '.github',
    '.claude',
    'cached',
    'cache',
    '.cache',
    'logs',
    'dist',
    'build',
    '*.egg-info',
    'tests',
    'output',
}

# When system_only is enabled, only include these top-level names
INCLUDED_TOP_LEVEL = {
    'src',
    'docs',
    'agent-brain',
    'scripts',
    'README.md',
    'LICENSE',
    'pyproject.toml',
    'requirements.txt',
    'Makefile',
}

def load_gitignore_patterns(path: Path):
    patterns = []
    if not path.exists():
        return patterns
    for ln in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        patterns.append(ln)
    return patterns

def matches_patterns(rel_path: str, patterns):
    for pat in patterns:
        p = pat.lstrip('/')
        if fnmatch.fnmatch(rel_path, p):
            return True
        if fnmatch.fnmatch(rel_path.split('/')[-1], p):
            return True
    return False

def should_exclude(path: Path, gitignore_patterns):
    rel = path.relative_to(REPO_ROOT).as_posix()
    parts = rel.split('/')
    for seg in parts:
        for pat in ALWAYS_EXCLUDE:
            try:
                if fnmatch.fnmatch(seg, pat):
                    return True
            except Exception:
                if seg == pat:
                    return True
    if matches_patterns(rel, gitignore_patterns):
        return True
    return False

def build_tree(root: Path, gitignore_patterns, max_depth: Optional[int] = None, show_excluded: bool = False, system_only: bool = True):
    tree_lines = []
    tree_nodes = {}

    def walk(p: Path, prefix: str = '', current_depth: int = 0):
        try:
            entries = sorted(list(p.iterdir()), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return
        for idx, child in enumerate(entries):
            if current_depth == 0 and system_only:
                top_name = child.name
                if top_name not in INCLUDED_TOP_LEVEL and top_name not in ALWAYS_EXCLUDE:
                    continue
                    
            rel = child.relative_to(REPO_ROOT).as_posix()
            if should_exclude(child, gitignore_patterns):
                continue

            is_last = idx == len(entries) - 1
            connector = '└── ' if is_last else '├── '
            # Extract just the filename to make it look like `tree` command
            name = child.name
            tree_lines.append(f"{prefix}{connector}{name}")
            tree_nodes[rel] = {'path': rel, 'is_dir': child.is_dir()}

            if child.is_dir():
                if max_depth is not None and current_depth + 1 >= max_depth:
                    extension = '    ' if is_last else '│   '
                    tree_lines.append(f"{prefix}{extension}└── ...")
                else:
                    extension = '    ' if is_last else '│   '
                    walk(child, prefix + extension, current_depth + 1)

    tree_lines.append(f"Repository: {REPO_ROOT.name}")
    tree_lines.append("")
    walk(root, '', 0)
    return tree_lines, tree_nodes

def write_outputs(md_lines, tree_nodes):
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')
    header = ["# System Map", "", f"Generated: {ts}", "", "```text"]
    content = '\n'.join(header + md_lines) + '\n```\n'
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(content, encoding='utf-8')
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({'generated': ts, 'nodes': tree_nodes}, indent=2), encoding='utf-8')
    print('Wrote', OUT_MD)

def main():
    patterns = load_gitignore_patterns(GITIGNORE)
    patterns += ['.agent-context']
    md_lines, tree_nodes = build_tree(REPO_ROOT, patterns, max_depth=5, show_excluded=False, system_only=True)
    write_outputs(md_lines, tree_nodes)

if __name__ == '__main__':
    main()
