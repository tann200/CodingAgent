from __future__ import annotations

import pytest

from src.tools._capabilities import (
    CAPABILITIES,
    FRONTIER_FULL_CAPABILITIES,
    LEGACY_TO_CAPABILITY,
    MEDIUM_CORE_CAPABILITIES,
    CapabilityTier,
    get_capability,
    get_schemas_for_tier,
    resolve_to_capability,
)
from src.tools._tool import PermissionKind


def test_twenty_seven_canonical_capabilities():
    assert len(CAPABILITIES) == 27
    assert len(FRONTIER_FULL_CAPABILITIES) == 27
    assert len(MEDIUM_CORE_CAPABILITIES) == 18


def test_tier_partition_consistency():
    frontier_only = FRONTIER_FULL_CAPABILITIES - MEDIUM_CORE_CAPABILITIES
    assert len(frontier_only) == 9
    expected_frontier_only = {
        "files_delete",
        "files_move",
        "lsp",
        "web_search",
        "web_read",
        "memory",
        "skills",
        "delegate",
        "batch",
    }
    assert frontier_only == expected_frontier_only


def test_capability_descriptors_valid():
    for name, cap in CAPABILITIES.items():
        assert cap.name == name
        assert len(cap.operations) >= 1
        assert cap.description
        assert isinstance(cap.permission_kind, PermissionKind)
        assert cap.audit_classification in ("read", "write", "exec", "network", "admin")
        assert cap.schema["type"] == "function"
        assert cap.schema["function"]["name"] == name
        assert "parameters" in cap.schema["function"]


def test_operation_specific_permissions():
    shell_cap = get_capability("shell")
    assert shell_cap is not None
    assert shell_cap.get_permission_kind("readonly") == PermissionKind.NONE
    assert shell_cap.get_permission_kind("execute") == PermissionKind.EXECUTE_BASH

    git_cap = get_capability("git")
    assert git_cap is not None
    assert git_cap.get_permission_kind("status") == PermissionKind.GIT_READ
    assert git_cap.get_permission_kind("commit") == PermissionKind.GIT_WRITE


def test_resolve_to_capability_canonical():
    cap, op, args = resolve_to_capability("files_read", {"path": "a.txt"})
    assert cap == "files_read"
    assert op == "read"
    assert args == {"path": "a.txt", "operation": "read"}


def test_resolve_to_capability_legacy_mappings():
    # read_file -> files_read, read
    cap, op, args = resolve_to_capability("read_file", {"path": "src/main.py"})
    assert cap == "files_read"
    assert op == "read"

    # edit_file_atomic -> files_edit, exact
    cap, op, args = resolve_to_capability("edit_file_atomic", {"path": "src/main.py", "old_str": "a", "new_str": "b"})
    assert cap == "files_edit"
    assert op == "exact"

    # bash -> shell, execute
    cap, op, args = resolve_to_capability("bash", {"command": "ls"})
    assert cap == "shell"
    assert op == "execute"

    # bash_readonly -> shell, readonly
    cap, op, args = resolve_to_capability("bash_readonly", {"command": "pwd"})
    assert cap == "shell"
    assert op == "readonly"

    # run_tests -> verify, test
    cap, op, args = resolve_to_capability("run_tests", {"path": "tests"})
    assert cap == "verify"
    assert op == "test"

    # git_commit -> git, commit
    cap, op, args = resolve_to_capability("git_commit", {"message": "initial"})
    assert cap == "git"
    assert op == "commit"


def test_get_schemas_for_tier():
    medium_schemas = get_schemas_for_tier("medium")
    assert len(medium_schemas) == 18
    medium_names = {s["function"]["name"] for s in medium_schemas}
    assert medium_names == set(MEDIUM_CORE_CAPABILITIES)

    frontier_schemas = get_schemas_for_tier("frontier")
    assert len(frontier_schemas) == 27
    frontier_names = {s["function"]["name"] for s in frontier_schemas}
    assert frontier_names == set(FRONTIER_FULL_CAPABILITIES)
