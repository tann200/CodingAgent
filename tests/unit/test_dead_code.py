"""tests/unit/test_dead_code.py — P1.4 CI gate: module reachability ratchet.

Audit finding HIGH-1: 8,222 lines across 14 ``src/`` modules were unreachable
from any entrypoint, including two security-relevant subsystems
(``server/exposure_policy.py`` and the 1,151-line
``PermissionGateway``/``permission_table`` pair).  Every one of them had passing
unit tests, which is exactly why the rot went unnoticed through a green build.

This file makes that class of finding a build failure.

Design: a **ratchet**, not a purge.  ``_KNOWN_DEAD`` is a reviewed baseline of
modules that are known-dead and slated for Phase-2 removal.  The test fails
only if a module *outside* that baseline becomes unreachable, so new rot is
blocked immediately while the existing cleanup proceeds independently.

Why a static import graph is sufficient for this purpose: the only legitimate
dynamic-import surface in this codebase is the inference adapter registry
(``importlib.import_module`` on a provider name) and Textual's string-based
widget/screen resolution.  Neither can make a module reachable for the purposes
of this check — the modules listed in ``_DYNAMIC_IMPORT_ROOTS`` are treated as
imported by everything, and everything else must appear in a real import edge.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules reachable only through importlib/dynamic resolution.  Any module
# whose dotted name is one of these, or a submodule of one, is treated as live.
_DYNAMIC_IMPORT_ROOTS = {
    # src/tools/_registry.py walks the package with pkgutil.iter_modules and
    # imports every module it finds, so the whole tools package is live.
    "src.tools",
    # Provider adapters are imported by name, e.g.
    # importlib.import_module("src.core.inference.adapters.ollama_adapter").
    "src.core.inference.adapters",
    "src.core.plugin",
    "src.core.skills",
    "src.core.messaging.event_bus",
    "src.config.toolsets",
    # The TUI ships as two byte-identical mirrors.  Runtime imports tui_src;
    # scripts and `python -m tui.src.ui.main` import tui.src.  Both trees are
    # load-bearing, and Textual resolves widgets/screens by string at runtime,
    # so neither tree can be proven reachable from import edges alone.
    "tui.src",
    "tui.tui_src",
}

# Real entrypoints: the console script, the TUI, the server, the CLI, and the
# `python -m src.core.evaluation` harness.
_ENTRYPOINTS = {
    "src.main",
    "tui.tui_src.ui.main",
    "tui.src.ui.main",
    "src.server.app",
    "src.cli",
    "src.core.evaluation.__main__",
}

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    "output",
    "results",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".codingAgent",
    ".agent-context",
    ".localAgent",
    ".agent",
    "tmp_test_orch",
    "docs",
    "tests",
}

# Reviewed baseline of known-dead modules (audit §HIGH-1, Phase 2 removal).
# Each entry is "why it is dead" so the baseline is auditable rather than a
# silent allowlist.  A module may be removed from this set once it is deleted.
_KNOWN_DEAD: dict[str, str] = {
    "src.core.credentials": "no production importer; only server_config docstring refs",
    "src.core.env_registry": "no production consumer; self-reference only",
    "src.core.inference.adapter_wrappers": "no production consumer; self-reference only",
    "src.core.inference.hardware_capability_profile": "re-exported by inference __init__ but nothing imports it",
    "src.core.inference.model_capability_profile": "only consumed by runtime_profile, itself dead",
    "src.core.inference.runtime_profile": "only consumed by kv_cache_governor/workflow_selector, themselves dead",
    "src.core.interfaces": "only referenced by abstract_session_store and docstrings",
    "src.core.memory.abstract_session_store": "only referenced by jsonl_session_store and docstrings",
    "src.core.orchestration.agent_hooks": "no production consumer; self-reference only",
    "src.core.orchestration.commands": "matches are the word 'commands' in slash-command code, not this module",
    "src.core.orchestration.context_manager": "no production consumer; only its own tests",
    "src.core.orchestration.event_log": "no production consumer; self-reference only",
    "src.core.orchestration.event_persistence": "no production consumer; only its own tests",
    "src.core.orchestration.graph_factory": "role graphs reachable only from tests",
    "src.core.orchestration.permission_gateway": "authorization layer never imported (docstring refs only)",
    "src.core.orchestration.permission_table": "imported only by permission_gateway",
    "src.core.orchestration.session_watcher": "no production consumer; self-reference only",
    "src.core.orchestration.shell_hooks": "only docstring cross-references in event_types/tool_execution_service",
    "src.core.orchestration.wave_coordinator": "no production consumer; self-reference only",
    "src.core.prompts": "package only re-exports system_prompt_builder; nothing imports either",
    "src.core.prompts.system_prompt_builder": "no production importer; only its own tests",
    "src.core.settings": "the TUI ships its own tui/src/ui/settings.py; this package is unused",
    "src.core.settings.controller": "imported only by its own package __init__",
    "src.core.startup": "no production consumer; self-reference only",
    "src.core.telemetry.consumer": "matches are EventBus 'consumer' bookkeeping, not this module",
    "src.core.telemetry.metrics": "matches are the word 'metrics' across server/messaging, not this module",
    "src.server.exposure_policy": "server never imports it; only its own test references it",
    "tui.main": "placeholder stub printing a greeting; superseded by tui/src/ui/main.py",
}


def _module_name(path: Path) -> str | None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if not rel.endswith(".py"):
        return None
    mod = rel[:-3].replace("/", ".")
    mod = mod.removesuffix(".__init__")
    # Paths under a dot-directory (e.g. tui/.local/skills/...) are local data,
    # not importable modules: their dotted name would contain an empty segment.
    if not mod or ".." in mod or any(p.startswith(".") for p in path.parts):
        return None
    return mod


def _iter_source_files() -> list[Path]:
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return out


def _build_module_set() -> set[str]:
    mods: set[str] = set()
    for path in _iter_source_files():
        mod = _module_name(path)
        if mod and (mod.startswith("src.") or mod.startswith("tui.")):
            mods.add(mod)
    return mods


def _add_with_ancestors(found: set[str], target: str, mods: set[str]) -> None:
    """Credit *target* and every parent package it implies.

    ``import src.core.settings.controller`` executes ``src.core.settings`` and
    ``src.core`` on the way.  Without crediting the ancestors, every package
    ``__init__`` in the tree looks unreachable and the gate drowns in false
    positives.
    """
    if target not in mods:
        return
    found.add(target)
    parts = target.split(".")
    for i in range(1, len(parts)):
        ancestor = ".".join(parts[:i])
        if ancestor in mods:
            found.add(ancestor)


def _build_import_edges(mods: set[str]) -> dict[str, set[str]]:
    """Map each module to the in-tree modules it imports.

    Relative imports are resolved against the importing module's own package.
    For a non-``__init__`` module ``a.b.c``, ``level=1`` means ``a.b``; for a
    package ``a/b/__init__.py`` (module ``a.b``), ``level=1`` means ``a.b``
    itself.  Getting this wrong silently drops intra-package edges and reports
    live modules as dead, so both forms are handled explicitly.
    """
    edges: dict[str, set[str]] = {}
    for path in _iter_source_files():
        mod = _module_name(path)
        if not mod:
            continue
        is_package = path.name == "__init__.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        parts = mod.split(".")
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_with_ancestors(found, alias.name, mods)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(parts) - node.level if not is_package else len(parts) - node.level + 1
                    base = ".".join(parts[: max(0, keep)])
                else:
                    base = node.module or ""
                _add_with_ancestors(found, base, mods)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    _add_with_ancestors(found, candidate, mods)
        edges.setdefault(mod, set()).update(found)
    return edges


def _is_dynamic(name: str) -> bool:
    """True when *name* is a dynamic-import root or one of its submodules.

    These are resolved by ``importlib``/Textual at runtime, so no static import
    edge can prove them live or dead.  Treating them as live is the only sound
    choice: a false "dead" verdict would demand baselining working code.
    """
    for root in _DYNAMIC_IMPORT_ROOTS:
        if name == root or name.startswith(root + "."):
            return True
    return False


def _reachable(edges: dict[str, set[str]], mods: set[str]) -> set[str]:
    """Transitive closure from the entrypoints, seeded with every dynamic module.

    Dynamically loaded modules are seeds rather than just "already live":
    their own import edges must still be walked, because e.g.
    ``src/tools/memory_tools.py`` is loaded by the pkgutil registry and imports
    ``src.core.memory.security``, which is therefore live transitively.
    """
    seen: set[str] = set()
    stack = list(_ENTRYPOINTS) + [m for m in mods if _is_dynamic(m)]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return seen


@pytest.fixture(scope="module")
def unreachable_modules() -> set[str]:
    mods = _build_module_set()
    edges = _build_import_edges(mods)
    live = _reachable(edges, mods) & mods
    return mods - live



class TestNoNewDeadCode:
    """P1.4: no module may become unreachable without an explicit baseline entry."""

    def test_unreachable_set_matches_reviewed_baseline(self, unreachable_modules: set[str]) -> None:
        new_dead = {m for m in unreachable_modules if m not in _KNOWN_DEAD}
        assert not new_dead, (
            "Newly unreachable production module(s) detected — a subsystem lost its "
            "last production importer. Either wire it up or add it to _KNOWN_DEAD with a "
            f"justification:\n  {sorted(new_dead)}"
        )

    def test_baseline_entries_are_still_dead(self, unreachable_modules: set[str]) -> None:
        """Baseline entries that became live should be removed from the list.

        Keeps ``_KNOWN_DEAD`` honest: it is a ratchet, not a graveyard.
        """
        resurrected = {m for m in _KNOWN_DEAD if m not in unreachable_modules}
        assert not resurrected, (
            "These modules are now reachable — remove them from _KNOWN_DEAD so the "
            f"baseline does not mask future regressions:\n  {sorted(resurrected)}"
        )

    def test_baseline_entries_still_exist_on_disk(self) -> None:
        """Every baseline entry must still name a real module or package."""
        missing = []
        for mod in _KNOWN_DEAD:
            base = REPO_ROOT / mod.replace(".", "/")
            if not (base.with_suffix(".py").exists() or (base / "__init__.py").exists()):
                missing.append(mod)
        assert not missing, (
            f"_KNOWN_DEAD names module(s) that no longer exist: {sorted(missing)}"
        )

    def test_baseline_is_documented(self) -> None:
        """Every baseline entry must carry a non-empty justification."""
        undocumented = [mod for mod, why in _KNOWN_DEAD.items() if not why.strip()]
        assert not undocumented, f"Baseline entries missing justification: {undocumented}"
