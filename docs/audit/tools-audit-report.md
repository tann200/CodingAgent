# Tools System Audit Report

**Date:** 2026-04-14  
**Scope:** Full audit of `src/tools/` directory  
**Test Baseline:** 3844 tests passed

---

## 1. Tool Architecture Overview

The tools system is well-structured with clear separation of concerns:

| Component | File | Purpose |
|-----------|------|---------|
| Tool Definition | `_tool.py` | `@tool` decorator with metadata (name, description, permission_kind) |
| Tool Registry | `registry.py` + `_registry.py` | Central registration with thread-safe locking |
| Security Constants | `_security.py` | SAFE_COMMANDS, DANGEROUS_PATTERNS, RESTRICTED_COMMANDS |
| Bash Execution | `_bash_exec.py` | bash(), bash_readonly() with output truncation |
| Sandbox | `sandbox.py` | bubblewrap-based containment |
| Bash Security | `bash_security.py` | AST-level command risk analysis |
| Permission | `tools_config.py` | Per-tool permission levels |
| Rollback | `rollback_tools.py` | Snapshot-based file revert |

---

## 2. Tool Registration

### Mechanism
- `@tool` decorator attaches `__tool_meta__` (ToolDefinition) to functions
- Auto-discovery via `ToolRegistry.discover()`
- Thread-safe registry with locking

### Strengths
- ✅ `@tool` decorator is clean and composable
- ✅ Metadata includes `permission_kind` (TASK-3)
- ✅ Thread-safe registry implementation
- ✅ Tool timeout configuration via `tool_registry.py`

### Issues
- **Low**: `registry.py` is a duplicate of `_registry.py` - consolidated in Vol28

---

## 3. Security Analysis

### Shell Execution Security

| Layer | Implementation | Status |
|-------|---------------|--------|
| Tier 0 Block | `_BASE_DANGEROUS_PATTERNS` | ✅ Blocked |
| Tier 1 Restricted | `RESTRICTED_COMMANDS` | ✅ Approval required |
| Tier 2 Safe | `SAFE_COMMANDS` | ✅ Auto-allowed |
| Bash Risk Analysis | `bash_security.py` | ✅ AST-level detection |
| Sandbox | `sandbox.py` | ✅ bubblewrap (Linux) |

### Dangerous Patterns Blocked
- Command substitution: `$()`, backticks
- Pipe to shell: `| bash`
- Destructive: `rm -rf`, `dd`, `mkfs`
- Fork bombs
- Absolute path bypass: `/bin/sh -c`

### Issues Found
- **None** - All security layers properly implemented

---

## 4. Permission System

### Implementation
- 5 permission levels: READ_ONLY, WORKSPACE_WRITE, DANGER, PROMPT, ALLOW
- Per-tool permissions via `TOOL_PERMISSIONS` dict
- `--permission-mode` CLI flag for session-wide enforcement
- Approval gates with timeout (120s)

### Strengths
- ✅ Permission gates with proper timeout handling
- ✅ Autonomous mode bypasses approval
- ✅ Subagent spawning requires PROMPT level
- ✅ Permission mode ordering enforced

### Issues
- **None** - Permission system is robust

---

## 5. Sandbox Implementation

### Levels
| Level | Network | PID | Filesystem |
|-------|---------|-----|------------|
| off | ✅ | ✅ | No restriction |
| workspace | ❌ | ❌ | /tmp, /dev, cwd writable |
| full | ❌ | ❌ | Strictest |

### Implementation
- bubblewrap on Linux, fallback on macOS
- Read-only bind of `/`
- Writable cwd only

### Issues
- **Low**: macOS fallback is unsandboxed (by design, bwrap Linux-only)

---

## 6. Output Safety

### Truncation
- `_BASH_STDOUT_MAX = 16_384` bytes
- `_BASH_STDERR_MAX = 6_000` bytes
- Token-based caps: 2000 tokens stdout, 600 tokens stderr
- Binary search for exact token boundaries

### Strengths
- ✅ Both byte and token-based limits
- ✅ Clear truncation notices
- ✅ Binary search for precision

---

## 7. Rollback System

### Features
- Snapshot before every write tool
- `revert_last_tool()` restores last write
- `list_snapshots()` shows available snapshots
- RollbackManager in orchestrator

### Strengths
- ✅ Works with write_file, edit_file, delete_file, apply_patch
- ✅ Uses ContextVar for orchestrator access
- ✅ Graceful degradation when no orchestrator

---

## 8. Tool Categories

| Category | Tools | Status |
|----------|-------|--------|
| File I/O | read_file, write_file, glob, grep | ✅ |
| Edit | edit_file_atomic, edit_by_line_range | ✅ |
| Bash | bash, bash_readonly | ✅ |
| Git | git_status, git_log, git_commit | ✅ |
| Verification | run_tests, verify_syntax | ✅ |
| Memory | save_memory, get_memory | ✅ |
| Web | web_search, read_web_page | ✅ |
| Subagent | delegate_task, list_subagent_roles | ✅ |
| LSP | lsp_diagnostics, lsp_references | ✅ |
| Plan | submit_plan_for_review | ✅ |

---

## 9. Tool Execution Pipeline

### Flow
1. **Permission check** — gates dangerous tools
2. **Tool lookup** — registry retrieval
3. **Read-before-write** — files_read verification
4. **Loop guards** — doom-loop detection
5. **Execute** — actual tool call
6. **Normalize** — result formatting

### Strengths
- ✅ Permission gateway integration
- ✅ Read-before-write enforcement
- ✅ Tool cooldown (COOLDOWN_GAP=3)
- ✅ Doom-loop detection (2 patterns)

---

## 10. Test Coverage

| Test File | Coverage |
|-----------|----------|
| test_tools_file_io.py | File read/write |
| test_tools_edit.py | Edit operations |
| test_tools_bash.py | Bash execution |
| test_tools_git.py | Git operations |
| test_tools_system_extra.py | System tools |
| test_sandbox.py | Sandbox execution |

### Issues
- **None** - Comprehensive tool tests

---

## 11. Issues Summary

| Severity | Issue | Status |
|----------|-------|--------|
| Low | macOS sandbox fallback | By design |

The tools system is **production-ready**:

- **85+ tools** with @tool decorator
- **5-layer security** (patterns, restricted, safe, bash_security, sandbox)
- **Permission system** with 5 levels
- **Rollback** for all write operations
- **Output safety** with token/byte limits
- **Comprehensive tests**

**Recommendation:** No changes needed. The tools system is robust and secure.
