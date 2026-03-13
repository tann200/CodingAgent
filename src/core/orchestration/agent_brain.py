"""Utility to load system prompts from the project-root `agent-brain/` folder.

This module provides `load_system_prompt(name, path)` which will try multiple
locations and conventions so that agent identities stored under `agent-brain/` are
loaded reliably as system prompts.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import re

try:
    import yaml
    _HAS_YAML = True
except Exception:
    yaml = None
    _HAS_YAML = False


def _repo_root() -> Path:
    # Assume this file sits under src/core/orchestration; repo root is two parents up
    return Path(__file__).parents[3]


def _agent_brain_dir() -> Path:
    return _repo_root() / 'agent-brain'


def _candidate_paths_for_name(name: str) -> list:
    """Return candidate file paths for a given prompt name.

    Conventions supported (in order):
    - agent-brain/roles/{name}.md
    - agent-brain/agents/{name}.md
    - agent-brain/system_prompt_{name}.md
    - agent-brain/{name}.md
    """
    candidates = []
    ad = _agent_brain_dir()
    if not name:
        return candidates
    # allow both exact name and normalized variants
    name_clean = str(name).strip()
    candidates.append(ad / 'roles' / f"{name_clean}.md")
    candidates.append(ad / 'agents' / f"{name_clean}.md")
    candidates.append(ad / f"system_prompt_{name_clean}.md")
    candidates.append(ad / f"{name_clean}.md")
    return candidates


def _parse_front_matter(text: str) -> Optional[dict]:
    """Parse YAML front-matter and return a dict. Falls back to simple parser if PyYAML isn't available."""
    if not text or not text.startswith('---'):
        return None
    # find end of front matter
    m = re.match(r'^---\s*\n(.*?)(\n---\s*\n)', text, flags=re.S)
    if not m:
        return None
    body = m.group(1)
    if _HAS_YAML and yaml is not None:
        try:
            data = yaml.safe_load(body)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    # fallback minimal parser
    out = {}
    for line in body.splitlines():
        if not line.strip() or ':' not in line:
            continue
        k, v = line.split(':', 1)
        out[k.strip()] = v.strip().strip('"')
    return out



def _load_core_component(filename: str) -> str:
    """Load a core markdown component from the agent-brain directory if it exists."""
    p = _agent_brain_dir() / 'identity' / filename
    if p.exists() and p.is_file():
        txt = p.read_text(encoding='utf-8')
        fm = _parse_front_matter(txt)
        if fm is not None:
            rest = re.sub(r'^---\s*\n.*?\n---\s*\n', '', txt, flags=re.S)
            return rest.strip()
        return txt.strip()
    return ""

def _compile_system_prompt(role_content: str) -> str:
    """Compile the final system prompt by injecting SOUL and LAWS with XML tags."""
    if not role_content:
        return ""
        
    soul_content = _load_core_component("SOUL.md")
    laws_content = _load_core_component("LAWS.md")
    
    parts = []
    
    # 1. Base Role
    parts.append("<system_role>")
    parts.append(role_content)
    parts.append("</system_role>")
    
    # 2. Operating Principles (SOUL)
    if soul_content:
        parts.append("\n<operating_principles>")
        parts.append(soul_content)
        parts.append("</operating_principles>")
        
    # 3. Core Laws (LAWS)
    if laws_content:
        parts.append("\n<core_laws>")
        parts.append(laws_content)
        parts.append("</core_laws>")
        
    return "\n".join(parts)

def load_system_prompt(name: Optional[str] = None, path: Optional[Path] = None) -> Optional[str]:
    """Load a system prompt by name or explicit path.

    Behavior:
    - If `path` is provided and exists, load and return its text. If it contains front-matter, prefer the body after front-matter.
    - If `name` is provided, try a small set of candidate files and prefer a file containing front-matter where "default: true" is set.
    - If neither is provided, try sensible defaults and prefer front-matter-enabled files.
    - Returns None when no prompt is found.
    """
    try:
        # explicit path takes precedence
        if path:
            p = Path(path)
            if p.exists() and p.is_file():
                txt = p.read_text(encoding='utf-8')
                fm = _parse_front_matter(txt)
                if fm is not None:
                    # return body after front matter
                    rest = re.sub(r'^---\s*\n.*?\n---\s*\n', '', txt, flags=re.S)
                    return _compile_system_prompt(rest.strip())
                return _compile_system_prompt(txt)

        # if name provided, search candidates and prefer front-matter default
        candidates = _candidate_paths_for_name(name) if name else []
        fm_candidates = []
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    txt = cand.read_text(encoding='utf-8')
                    fm = _parse_front_matter(txt)
                    if fm is not None:
                        # if front-matter marks default true and name matches, take it
                        if fm.get('default', '').lower() in ('true', '1', 'yes'):
                            rest = re.sub(r'^---\s*\n.*?\n---\s*\n', '', txt, flags=re.S)
                            return _compile_system_prompt(rest.strip())
                        fm_candidates.append((cand, txt, fm))
                    else:
                        # return first non-fm file if no fm defaults matched
                        return _compile_system_prompt(txt)
            except Exception:
                continue
        # if we collected front-matter candidates, return the first one's body
        if fm_candidates:
            _, txt, fm = fm_candidates[0]
            rest = re.sub(r'^---\s*\n.*?\n---\s*\n', '', txt, flags=re.S)
            return _compile_system_prompt(rest.strip())

        # fallback defaults when name is None: prefer front-matter-enabled files
        brain = _agent_brain_dir()
        primary = brain / 'roles' / 'operational.md'
        if primary.exists() and primary.is_file():
            txt = primary.read_text(encoding='utf-8')
            fm = _parse_front_matter(txt)
            if fm:
                rest = re.sub(r'^---\s*\n.*?\n---\s*\n', '', txt, flags=re.S)
                return _compile_system_prompt(rest.strip())
            return _compile_system_prompt(txt)
        
        # If no role files found, just return a bare minimum prompt
        return _compile_system_prompt("You are a helpful coding assistant.")
    except Exception:
        return None
    return None
