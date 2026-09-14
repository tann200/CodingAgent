"""PHASE-4 item 4.7: every built-in tool declares an explicit permission_kind.

Each ``@tool``-decorated tool must pass ``permission_kind=`` at definition
time (rather than relying on the side_effects-implied default), so the
registry's ``get_permission_kind`` and the permission gateway produce precise
table kinds.  The invariant is enforced on the decorator metadata
(``permission_kind_explicit``) and only dict-schema tools (e.g. LSP) use the
plain-string ``permission_kind`` entry key.
"""

from src.tools import build_registry
from src.tools._tool import PermissionKind

# Test-only tool registered without a permission_kind entry; excluded from the
# exhaustive built-in check (it is not a built-in @tool).
_KNOWN_NON_DECLARED = {"echo"}


def _builtin_registry():
    return build_registry()


def test_all_decorated_builtins_declare_explicit_permission_kind():
    reg = _builtin_registry()
    assert len(reg.list()) > 50  # sanity: full suite registered
    undeclared = []
    for name in reg.list():
        if name in _KNOWN_NON_DECLARED:
            continue
        entry = reg.get(name)
        defn = entry.get("__tool_meta__")
        if defn is not None:
            if not defn.permission_kind_explicit:
                undeclared.append(f"{name} (implicit default)")
        elif not entry.get("permission_kind"):
            undeclared.append(f"{name} (dict-style, no permission_kind)")
    assert not undeclared, f"tools missing explicit permission_kind: {undeclared}"


def test_known_tool_kind_mappings():
    reg = _builtin_registry()
    assert reg.get_permission_kind("write_file") == PermissionKind.WRITE_FILE
    assert reg.get_permission_kind("edit_file_atomic") == PermissionKind.WRITE_FILE
    assert reg.get_permission_kind("multiedit") == PermissionKind.WRITE_FILE
    assert reg.get_permission_kind("delete_file") == PermissionKind.WRITE_FILE
    assert reg.get_permission_kind("bash") == PermissionKind.EXECUTE_BASH
    assert reg.get_permission_kind("run_tests") == PermissionKind.EXECUTE_BASH
    assert reg.get_permission_kind("read_file") == PermissionKind.READ_FILE
    assert reg.get_permission_kind("grep") == PermissionKind.READ_FILE
    assert reg.get_permission_kind("git_commit") == PermissionKind.GIT_WRITE
    assert reg.get_permission_kind("git_diff") == PermissionKind.GIT_READ
    assert reg.get_permission_kind("delegate_task") == PermissionKind.DELEGATE
    assert reg.get_permission_kind("submit_plan_for_review") == PermissionKind.PLAN
    assert reg.get_permission_kind("batch") == PermissionKind.NONE


def test_read_only_flagship_tools_are_not_write_kind():
    reg = _builtin_registry()
    for name in ("read_file", "list_files", "grep", "summarize_structure", "ask_user"):
        kind = reg.get_permission_kind(name)
        assert kind not in (PermissionKind.WRITE_FILE, PermissionKind.EXECUTE_BASH)


def test_decorator_records_explicitness():
    import src.tools._tool as tool_mod

    def _noop():
        pass

    implicit_defn = tool_mod.tool(side_effects=["write"])(_noop).__tool_meta__
    assert implicit_defn.permission_kind is PermissionKind.WRITE_FILE
    assert implicit_defn.permission_kind_explicit is False

    explicit_defn = tool_mod.tool(
        side_effects=["write"], permission_kind=PermissionKind.WRITE_FILE
    )(_noop).__tool_meta__
    assert explicit_defn.permission_kind_explicit is True

    bare_defn = tool_mod.tool(_noop).__tool_meta__
    assert bare_defn.permission_kind is PermissionKind.NONE
    assert bare_defn.permission_kind_explicit is False