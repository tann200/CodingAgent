"""``codingagent skill`` — list, inspect, export, and validate packaged agent skills.

Supports packaging and exporting skills to:
- Agensi.io marketplace format (``.agensi/skills/<name>/SKILL.md``)
- Claude Code (``.claude/skills/<name>/SKILL.md``)
- Cursor (``.cursor/skills/<name>/SKILL.md``)
- Custom directories / agent harnesses
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.cli._helpers import print_json, resolve_workdir
from src.tools.skill_tools import _find_skill_file, _list_skill_names, load_skill

_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_CYAN = "\033[36m"


def extract_skill_metadata(content: str, default_name: str) -> Tuple[str, str, str]:
    """Extract (name, description, body) from skill markdown text."""
    name = default_name
    description = ""
    body = content

    # Check for YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if fm_match:
        fm_text, body = fm_match.groups()
        for line in fm_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("name:"):
                name = line_str.split(":", 1)[1].strip().strip("\"'")
            elif line_str.startswith("description:"):
                description = line_str.split(":", 1)[1].strip().strip("\"'")

    if not description:
        # Extract first non-heading, non-empty text lines
        for para in body.split("\n\n"):
            lines = [
                line.strip()
                for line in para.splitlines()
                if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("---")
            ]
            if lines:
                description = " ".join(lines)[:160]
                break

    if not description:
        description = f"Agent skill: {name}"

    return name, description, body.strip()


def get_skill_info(name: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a single named skill."""
    path = _find_skill_file(name)
    if not path or not path.exists():
        return None

    try:
        content = path.read_text(encoding="utf-8")
        meta_name, desc, body = extract_skill_metadata(content, name)
        is_bundle = path.name in ("SKILL.md", "skill.md")
        return {
            "name": name,
            "title": meta_name,
            "description": desc,
            "path": str(path),
            "is_bundle": is_bundle,
            "size_bytes": path.stat().st_size,
            "line_count": len(content.splitlines()),
        }
    except Exception:
        return None


get_skill_metadata = get_skill_info


def list_all_skills() -> List[Dict[str, Any]]:
    """List metadata for all available skills."""
    names = _list_skill_names()
    results: List[Dict[str, Any]] = []
    for n in names:
        info = get_skill_info(n)
        if info:
            results.append(info)
    return results


def resolve_export_destination(
    skill_name: str,
    target: str,
    out_dir: Optional[str] = None,
) -> Path:
    """Compute the target file path for an exported skill."""
    base = Path(resolve_workdir(out_dir))
    t_lower = (target or "agensi").lower()

    if t_lower == "claude":
        dest_dir = base / ".claude" / "skills" / skill_name
    elif t_lower == "cursor":
        dest_dir = base / ".cursor" / "skills" / skill_name
    elif t_lower == "agensi":
        dest_dir = base / ".agensi" / "skills" / skill_name
    else:
        # Direct directory export
        dest_dir = base / skill_name

    return dest_dir / "SKILL.md"


def export_single_skill(
    skill_name: str,
    target: str,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Export a named skill to the target agent harness directory."""
    path = _find_skill_file(skill_name)
    if not path or not path.exists():
        return {
            "name": skill_name,
            "status": "not_found",
            "error": f"Skill '{skill_name}' not found",
        }

    try:
        content = path.read_text(encoding="utf-8")
        meta_name, desc, body = extract_skill_metadata(content, skill_name)

        # Standardise frontmatter
        standardized_content = (
            f"---\n"
            f"name: {meta_name}\n"
            f"description: {desc}\n"
            f"---\n\n"
            f"{body}\n"
        )

        dest_file = resolve_export_destination(skill_name, target, out_dir)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(standardized_content, encoding="utf-8")

        return {
            "name": skill_name,
            "status": "exported",
            "target": target,
            "source_path": str(path),
            "export_path": str(dest_file),
            "bytes_written": len(standardized_content.encode("utf-8")),
        }
    except Exception as exc:
        return {
            "name": skill_name,
            "status": "error",
            "error": str(exc),
        }


def validate_single_skill(skill_name: str) -> Dict[str, Any]:
    """Validate a skill's format, metadata, and body."""
    path = _find_skill_file(skill_name)
    if not path or not path.exists():
        return {
            "name": skill_name,
            "valid": False,
            "issues": [f"File not found: '{skill_name}'"],
        }

    issues: List[str] = []
    try:
        content = path.read_text(encoding="utf-8")
        if len(content.strip()) < 50:
            issues.append("Skill instruction body is unusually short (<50 chars)")

        meta_name, desc, body = extract_skill_metadata(content, skill_name)
        if not meta_name:
            issues.append("Missing skill name metadata")
        if not desc or desc.startswith("Agent skill:"):
            issues.append("Missing descriptive metadata (YAML description: or summary paragraph)")
        if not body or len(body.splitlines()) < 3:
            issues.append("Instruction body has fewer than 3 lines")

        return {
            "name": skill_name,
            "title": meta_name,
            "valid": len(issues) == 0,
            "path": str(path),
            "issues": issues,
        }
    except Exception as exc:
        return {
            "name": skill_name,
            "valid": False,
            "issues": [f"Failed to read skill: {exc}"],
        }


def run_skill(args: Any) -> int:
    """Dispatch ``codingagent skill`` subcommands."""
    action = getattr(args, "skill_action", None) or "list"
    as_json = getattr(args, "json", False)

    # ------------------------------------------------------------------ LIST
    if action == "list":
        skills = list_all_skills()
        if as_json:
            print_json({"status": "ok", "count": len(skills), "skills": skills})
            return 0

        print(f"\n{_C_BOLD}{_C_CYAN}Packaged Agent Skills ({len(skills)} available){_C_RESET}\n")
        for s in skills:
            kind = f"{_C_GREEN}[bundle]{_C_RESET}" if s["is_bundle"] else f"{_C_DIM}[flat]{_C_RESET}"
            print(f"  • {_C_BOLD}{s['name']}{_C_RESET} {kind}")
            print(f"    {_C_DIM}{s['description'][:100]}{_C_RESET}")
        print(f"\n{_C_DIM}Use `codingagent skill show <name>` to view or `codingagent skill export <name>` to package.{_C_RESET}\n")
        return 0

    # ------------------------------------------------------------------ SHOW
    if action == "show":
        skill_name = getattr(args, "name", None)
        if not skill_name:
            print("Error: skill name required. Run `codingagent skill list` to view available skills.", file=sys.stderr)
            return 1

        res = load_skill(skill_name)
        if res.get("status") != "ok":
            if as_json:
                print_json(res)
            else:
                print(f"Error: {res.get('error')}", file=sys.stderr)
            return 1

        if as_json:
            print_json(res)
            return 0

        print(f"\n{_C_BOLD}{_C_CYAN}=== Skill: {res['name']} ==={_C_RESET}\n")
        print(res.get("content", ""))
        print(f"\n{_C_DIM}Source: {res.get('path')}{_C_RESET}\n")
        return 0

    # ------------------------------------------------------------------ EXPORT
    if action == "export":
        skill_name = getattr(args, "name", None)
        export_all = getattr(args, "all", False)
        target = getattr(args, "target", "agensi")
        out_dir = getattr(args, "out", None)

        if not skill_name and not export_all:
            print("Error: specify a skill name or use --all. Example: `codingagent skill export polyglot-verify`", file=sys.stderr)
            return 1

        targets_to_export: List[str] = _list_skill_names() if export_all else ([str(skill_name)] if skill_name else [])
        results: List[Dict[str, Any]] = []

        for name_to_exp in targets_to_export:
            res = export_single_skill(name_to_exp, target=target, out_dir=out_dir)
            results.append(res)

        all_ok = all(r.get("status") == "exported" for r in results)

        if as_json:
            print_json({
                "status": "ok" if all_ok else "partial",
                "target": target,
                "exported": results,
            })
            return 0 if all_ok else 1

        print(f"\n{_C_BOLD}{_C_CYAN}Exported Skills to target: '{target}'{_C_RESET}\n")
        for r in results:
            if r.get("status") == "exported":
                print(f"  {_C_GREEN}✓{_C_RESET} {_C_BOLD}{r['name']}{_C_RESET} → {r['export_path']} ({r['bytes_written']} bytes)")
            else:
                print(f"  {_C_RED}✗{_C_RESET} {_C_BOLD}{r['name']}{_C_RESET} → {r.get('error')}")
        print()
        return 0 if all_ok else 1

    # ------------------------------------------------------------------ VALIDATE
    if action == "validate":
        skill_name = getattr(args, "name", None)
        validate_all = getattr(args, "all", False)

        if not skill_name and not validate_all:
            skill_name = None
            validate_all = True

        targets_to_val: List[str] = _list_skill_names() if validate_all else ([str(skill_name)] if skill_name else [])
        results = [validate_single_skill(n) for n in targets_to_val if n]

        all_valid = all(r.get("valid") for r in results)

        if as_json:
            print_json({
                "status": "ok" if all_valid else "issues_found",
                "validated": results,
            })
            return 0 if all_valid else 1

        print(f"\n{_C_BOLD}{_C_CYAN}Skill Validation Report ({len(results)} skills){_C_RESET}\n")
        for r in results:
            if r.get("valid"):
                print(f"  {_C_GREEN}✓{_C_RESET} {_C_BOLD}{r['name']}{_C_RESET}: Valid metadata & format")
            else:
                print(f"  {_C_YELLOW}!{_C_RESET} {_C_BOLD}{r['name']}{_C_RESET}:")
                for issue in r.get("issues", []):
                    print(f"      - {issue}")
        print()
        return 0 if all_valid else 1

    return 0
