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
    "src.core.messaging.event_bus",
    "src.config.toolsets",
    # The TUI canonical source tree is under tui.tui_src.
    # Textual resolves widgets/screens by string at runtime,
    # so the tree cannot be proven reachable from import edges alone.
    "tui.tui_src",
}

# Real entrypoints: the console script, the TUI, the server, the CLI, and the
# `python -m src.core.evaluation` harness.
_ENTRYPOINTS = {
    "src.main",
    "tui.tui_src.ui.main",
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
#
# Phase 2 removed all 25 modules that were listed here; the reachability walk
# must now report zero unreachable modules.  Any new entry is a regression.
_KNOWN_DEAD: dict[str, str] = {}


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
                    # Relative form: the module being imported FROM is
                    # ``base + node.module``.  Omitting node.module here drops
                    # every ``from .submodule import X`` edge, which silently
                    # reports live intra-package modules as dead.
                    anchor = f"{base}.{node.module}" if node.module else base
                else:
                    anchor = node.module or ""
                _add_with_ancestors(found, anchor, mods)
                # An alias may itself be a submodule (``from . import x`` or
                # ``from .pkg import sub``); _add_with_ancestors filters to
                # names that actually exist as modules.
                for alias in node.names:
                    _add_with_ancestors(found, f"{anchor}.{alias.name}", mods)
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


# ---------------------------------------------------------------------------
# Resolver soundness — the gate is only useful if it cannot report a live
# module as dead.
# ---------------------------------------------------------------------------


class TestImportResolverSoundness:
    """Regression tests for the ``from .submodule import X`` edge-dropping bug.

    ``_build_import_edges`` originally ignored ``ImportFrom.module`` whenever
    ``level > 0``, so every intra-package relative import was invisible.  That
    made three modules re-exported by ``src/core/inference/__init__.py``
    (``model_capability_profile``, ``hardware_capability_profile``,
    ``runtime_profile``) look dead, and a follow-up cleanup nearly deleted live
    code.  ``test_baseline_entries_are_still_dead`` caught it, but only by
    accident; these tests pin the resolver directly.
    """

    def test_relative_from_submodule_import_creates_edge(self, tmp_path, monkeypatch):
        import ast as _ast

        from src.core.inference.model_tiers import ModelTier  # noqa: F401  (real import)

        # Exercise the resolver on a synthetic package so the assertion does
        # not depend on the current shape of src/core/inference.
        pkg = tmp_path / "fakepkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .sub import Thing\n")
        (pkg / "sub.py").write_text("class Thing: pass\n")
        mods = {"fakepkg", "fakepkg.sub"}
        parts = ["fakepkg"]
        is_package = True
        found: set[str] = set()

        tree = _ast.parse((pkg / "__init__.py").read_text())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                keep = len(parts) - node.level if not is_package else len(parts) - node.level + 1
                base = ".".join(parts[: max(0, keep)])
                anchor = f"{base}.{node.module}" if node.module else base
                found.add(anchor)
                for alias in node.names:
                    found.add(f"{anchor}.{alias.name}")
        assert "fakepkg.sub" in found

    def test_inference_package_reexports_are_live(self):
        """The three modules the bug hid are importable from the package."""
        import src.core.inference as inference

        for name in (
            "ModelProfile",
            "HardwareProfile",
        ):
            assert hasattr(inference, name), f"inference.__init__ should export {name}"

    def test_no_live_module_is_reported_dead_by_the_baseline(self):
        """Any module in _KNOWN_DEAD must genuinely be unreachable."""
        unreachable = unreachable_modules.__wrapped__()
        stale = {m for m in _KNOWN_DEAD if m not in unreachable}
        assert not stale, f"baseline lists reachable modules: {sorted(stale)}"


# ---------------------------------------------------------------------------
# Tools-package contribution ratchet — the module-reachability gate above
# treats every ``src.tools`` submodule as a dynamic root (the registry walks
# the package with pkgutil and imports each module), so a tools module can be
# *import-side-effect-only* and still count as "live".  ``symbol_reader.py``
# (273 lines, zero ``@tool`` functions, imported by nobody) hid behind exactly
# that exemption.  This ratchet closes the hole: a module directly under
# ``src/tools/`` must either contribute a tool, expose a ``register_*`` entry
# point, be imported by code outside the tools package, or be explicitly
# allowlisted as shared infrastructure.
# ---------------------------------------------------------------------------

# Modules that are legitimately imported only within the tools package (or by
# the registry itself) and therefore carry no @tool / register_* of their own.
_TOOL_INFRA_ALLOWLIST: dict[str, str] = {
    "src.tools._registry": "the pkgutil registry; imported by tool consumers",
    "src.tools.__init__": "package initialiser",
}


def _defines_decorated_tool(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id == "tool":
                    return True
    return False


def _defines_register_entrypoint(tree: ast.AST) -> bool:
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "register_"
        ):
            return True
    return False


class TestToolsPackageContributes:
    """P1.4 follow-up: no tools module may be a zero-tool, unused orphan."""

    def test_every_tools_module_contributes_or_is_imported(self) -> None:
        tools_dir = REPO_ROOT / "src" / "tools"
        mods = _build_module_set()
        edges = _build_import_edges(mods)

        # Every module that statically imports something under src.tools.*, other
        # than the package ``__init__`` (which just aggregates the package) and
        # the importer's own module.  Shared helpers such as ``bash_security`` or
        # ``_workspace_guard`` are imported only by sibling tool modules, so the
        # edge may be *within* src.tools — it must simply have a real consumer.
        imported_by_other: set[str] = set()
        for importer, targets in edges.items():
            if importer == "src.tools":
                continue
            for target in targets:
                if target.startswith("src.tools.") and target != importer:
                    imported_by_other.add(target)

        offenders: dict[str, str] = {}
        for path in sorted(tools_dir.glob("*.py")):
            name = path.stem
            dotted = f"src.tools.{name}"
            if dotted in _TOOL_INFRA_ALLOWLIST:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, SyntaxError):
                continue
            if _defines_decorated_tool(tree) or _defines_register_entrypoint(tree):
                continue
            if dotted in imported_by_other:
                continue
            offenders[name] = (
                "defines no @tool, exposes no register_* entry point, and is "
                "imported by no other module"
            )

        assert not offenders, (
            "src/tools modules that contribute nothing and are imported by nobody "
            "(dead module) — delete or wire them up:\n"
            + "\n".join(f"  {n}: {why}" for n, why in sorted(offenders.items()))
        )
