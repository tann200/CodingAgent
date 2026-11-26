"""Helpers for loading identity, role, and progressive-disclosure SKILL.md catalogs (TASK-32)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.security.redaction import redact_secrets
from src.core.security.untrusted_envelopes import wrap_untrusted
from src.tools._path_utils import safe_resolve

logger = logging.getLogger(__name__)

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
_BUNDLED_SKILL_DIRS: Tuple[Path, ...] = (
    _CONFIG_ROOT / "skills",
    _CONFIG_ROOT / "agent-brain" / "skills",
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---(?:\n|$)", re.DOTALL)
_FM_KV_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")


@dataclass(frozen=True)
class SkillMeta:
    """Compact metadata for a discovered skill (progressive-disclosure catalog entry)."""

    name: str
    description: str
    path: Path
    provenance: str  # "project" | "user" | "bundled"
    source_scope: str  # ".codingagent" | ".agents" | ".claude" | "~/.codingagent" | "bundled"


def load_prompt_directory(directory: Path, read_text_cached: Callable[[Path], str | None]) -> dict[str, str]:
    """Load all `*.md` files in a directory into a stem->content map."""
    loaded: dict[str, str] = {}
    if not directory.exists():
        return loaded
    for file_path in directory.glob("*.md"):
        loaded[file_path.stem] = read_text_cached(file_path) or ""
    return loaded


def merge_workspace_skill_overrides(
    base_skills: dict[str, str],
    workspace_skill_dirs: list[Path],
    read_text_cached: Callable[[Path], str | None],
) -> dict[str, str]:
    """Overlay workspace skills on top of built-in skills."""
    merged = dict(base_skills)
    for skill_dir in workspace_skill_dirs:
        if not skill_dir.exists():
            continue
        for skill_file in skill_dir.glob("*.md"):
            content = read_text_cached(skill_file)
            if content:
                merged[skill_file.stem] = content
        for child in skill_dir.iterdir():
            if child.is_dir():
                for fname in ("SKILL.md", "skill.md"):
                    cand = child / fname
                    if cand.is_file():
                        content = read_text_cached(cand)
                        if content:
                            merged[child.name] = content
                        break
    return merged


def parse_skill_frontmatter(raw: str, *, fallback_name: str) -> Tuple[str, str, str]:
    """Parse optional YAML frontmatter (CRLF-safe) returning ``(name, description, body)``."""
    normalized = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    name = fallback_name.strip()
    description = ""
    body = normalized

    m = _FRONTMATTER_RE.match(normalized)
    if m:
        fm_block = m.group(1)
        body = normalized[m.end() :]
        for line in fm_block.splitlines():
            kv = _FM_KV_RE.match(line.strip())
            if not kv:
                continue
            key = kv.group(1).strip().lower()
            val = kv.group(2).strip().strip('"').strip("'").strip()
            if key == "name" and val:
                name = val
            elif key == "description" and val:
                description = val

    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("```") or stripped.startswith("---"):
                continue
            # Take first sentence up to 160 chars
            sentence = stripped.split(". ")[0].strip()
            description = (sentence[:157] + "...") if len(sentence) > 160 else sentence
            break
    if not description:
        description = f"Skill instructions for {name}."

    return name, description, body.strip()


def _scan_skill_dir(
    skill_dir: Path,
    *,
    provenance: str,
    source_scope: str,
    confine_root: Optional[Path] = None,
) -> List[SkillMeta]:
    """Scan a single skill directory for ``<name>/SKILL.md`` bundles and flat ``<name>.md`` files."""
    found: List[SkillMeta] = []
    if not skill_dir.exists() or not skill_dir.is_dir():
        return found

    candidates: List[Tuple[str, Path]] = []
    try:
        entries = sorted(skill_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found

    for child in entries:
        if child.is_dir():
            for fname in ("SKILL.md", "skill.md"):
                cand = child / fname
                if cand.is_file() or cand.is_symlink():
                    candidates.append((child.name, cand))
                    break
        elif (child.is_file() or child.is_symlink()) and child.suffix.lower() == ".md":
            if child.name.lower() in ("readme.md", "index.md"):
                continue
            candidates.append((child.stem, child))

    for fallback_name, cand_path in candidates:
        resolved_path = cand_path
        if confine_root is not None:
            try:
                resolved_path = safe_resolve(str(cand_path), confine_root)
            except (PermissionError, ValueError, OSError) as exc:
                logger.warning(
                    "discover_skills: rejecting out-of-workspace skill symlink %s: %s",
                    cand_path,
                    exc,
                )
                continue
        try:
            if not resolved_path.is_file():
                continue
            raw = resolved_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("discover_skills: unreadable skill %s: %s", resolved_path, exc)
            continue

        parsed_name, desc, _ = parse_skill_frontmatter(raw, fallback_name=fallback_name)
        safe_name = parsed_name.strip() or fallback_name
        if "/" in safe_name or "\\" in safe_name or ".." in safe_name:
            safe_name = fallback_name
        found.append(
            SkillMeta(
                name=safe_name,
                description=redact_secrets(desc),
                path=resolved_path,
                provenance=provenance,
                source_scope=source_scope,
            )
        )
    return found


def discover_skills(
    workdir: Optional[str | Path] = None,
    *,
    home_dir: Optional[Path] = None,
    bundled_dirs: Optional[Sequence[Path]] = None,
) -> Dict[str, SkillMeta]:
    """Discover skills across workspace, user, and bundled scopes with deterministic precedence.

    Precedence (highest wins on name collision):
      1. ``<workdir>/.codingagent/skills/`` (and ``.codingAgent/skills/``)
      2. ``<workdir>/.agents/skills/``
      3. ``<workdir>/.claude/skills/``
      4. ``~/.codingagent/skills/``
      5. Bundled ``src/config/skills/`` and ``src/config/agent-brain/skills/``
    """
    home = home_dir if home_dir is not None else Path.home()
    wd = Path(workdir).resolve() if workdir is not None else None
    b_dirs = list(bundled_dirs) if bundled_dirs is not None else list(_BUNDLED_SKILL_DIRS)

    scopes: List[Tuple[Path, str, str, Optional[Path]]] = []
    if wd is not None:
        seen_ws_dirs: set[Path] = set()
        for rel, label in (
            (".codingagent/skills", ".codingagent"),
            (".codingAgent/skills", ".codingagent"),
            (".agents/skills", ".agents"),
            (".claude/skills", ".claude"),
        ):
            d = wd / rel
            try:
                key = d.resolve() if d.exists() else d
            except OSError:
                key = d
            if key not in seen_ws_dirs:
                seen_ws_dirs.add(key)
                scopes.append((d, "project", label, wd))

    scopes.append((home / ".codingagent" / "skills", "user", "~/.codingagent", None))
    for bdir in b_dirs:
        scopes.append((bdir, "bundled", "bundled", None))

    discovered: Dict[str, SkillMeta] = {}
    seen_canonical: set[str] = set()
    for sdir, prov, scope_label, confine in scopes:
        for meta in _scan_skill_dir(
            sdir,
            provenance=prov,
            source_scope=scope_label,
            confine_root=confine,
        ):
            canon = meta.name.lower().replace("_", "-")
            if canon in seen_canonical:
                continue
            seen_canonical.add(canon)
            discovered[meta.name] = meta

    return dict(sorted(discovered.items(), key=lambda kv: kv[0].lower()))


def find_skill_meta(
    name: str,
    catalog: Mapping[str, SkillMeta],
) -> Optional[SkillMeta]:
    """Resolve *name* against *catalog* tolerating kebab-case/snake_case/case variants."""
    if not name:
        return None
    clean = name.strip()
    if clean in catalog:
        return catalog[clean]
    target = clean.lower().replace("_", "-")
    for key, meta in catalog.items():
        if key.lower().replace("_", "-") == target:
            return meta
    return None


def read_skill_content(meta: SkillMeta) -> str:
    """Read skill text, redact secrets, and wrap project-scoped skills in a nonce envelope."""
    raw = meta.path.read_text(encoding="utf-8", errors="replace")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    redacted = redact_secrets(normalized)
    if meta.provenance == "project":
        return wrap_untrusted(
            "project_skill",
            redacted,
            attrs={"name": meta.name, "scope": meta.source_scope},
        )
    return redacted


def render_skill_catalog(
    skills: Mapping[str, SkillMeta],
    *,
    max_desc_chars: int = 80,
) -> str:
    """Render compact progressive-disclosure skill index (~15 tokens/skill, KV-cache static)."""
    if not skills:
        return ""
    lines = ["Available skills (call load_skill(name) when relevant):"]
    for name in sorted(skills.keys(), key=str.lower):
        meta = skills[name]
        desc = " ".join((meta.description or "").split())
        if len(desc) > max_desc_chars:
            desc = desc[: max_desc_chars - 3].rstrip() + "..."
        lines.append(f"- {meta.name}: {desc}")
    return "\n".join(lines)
