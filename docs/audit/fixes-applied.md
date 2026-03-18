# Security Fixes Applied

**Date:** March 18, 2026  
**Audit Reference:** `docs/audit/audit-report.md`

---

## Phase 1: Critical Security Fixes

### 1.1 Bash Tool Allowlist Secured ✅

**Status:** IMPLEMENTED  
**File:** `src/tools/file_tools.py`

**Changes:**
- Categorized commands into three tiers:
  - **Safe (Tier 1):** Read-only and utility commands (ls, cat, grep, find, git, etc.) - auto-allowed
  - **Test/Compile (Tier 2):** Build and test commands (pytest, npm test, cargo test, go build, etc.) - auto-allowed
  - **Restricted (Tier 3):** Package installers and network fetchers (pip, npm install, curl, wget) - require user approval

**Previous Vulnerability:** Allowed arbitrary code execution via pip, npm, curl, wget

**Fix Applied:**
- Added tiered allowlist with explicit restrictions
- Restricted commands now return error with `requires_approval: True`
- npm/node restricted to test/run commands only
- Shell operators (&&, ||, |, >) remain blocked

---

### 1.2 Sandbox Validation - Fail Closed ✅

**Status:** IMPLEMENTED  
**File:** `src/core/orchestration/orchestrator.py` (lines 846-872)

**Changes:**
- Changed sandbox validation from fail-open to fail-closed
- If sandbox import or validation fails, write operations are now BLOCKED
- Returns explicit error: `"Sandbox validation aborted: {error}. Write operation blocked for safety."`

**Previous Vulnerability:** If ExecutionSandbox import failed, writes proceeded without validation

**Fix Applied:**
```python
except Exception as e:
    guilogger.error(f"Sandbox validation failed (fail-closed): {e}")
    return {
        "ok": False,
        "error": f"Sandbox validation aborted: {str(e)}. Write operation blocked for safety.",
    }
```

---

### 1.3 Symlink Path Traversal Prevention ✅

**Status:** IMPLEMENTED  
**File:** `src/tools/file_tools.py` (`_safe_resolve` function)

**Changes:**
- Enhanced path resolution with `strict=True` mode
- Added explicit symlink target checking using `os.path.realpath`
- Validates resolved path is within workdir before allowing access

**Previous Vulnerability:** Symlinks pointing outside workdir could bypass path restrictions

**Fix Applied:**
```python
real_path = os.path.realpath(p)
real_workdir = os.path.realpath(workdir_resolved)

if not real_path.startswith(real_workdir + os.sep) and real_path != real_workdir:
    raise PermissionError(
        f"Path '{path}' resolves to '{real_path}' which is outside "
        f"working directory '{real_workdir}'. Symlink traversal blocked."
    )
```

---

## Phase 2: Performance Optimization

### 2.1 Fast-Path Routing ✅

**Status:** IMPLEMENTED  
**File:** `src/core/orchestration/graph/builder.py`

**Changes:**
- Added `route_after_perception()` conditional routing function
- Simple 1-step tasks now skip heavy analysis and planning
- Perception → Execution (fast path) vs Perception → Analysis → Planning → Execution (full path)

**Previous Issue:** All tasks forced through entire cognitive pipeline, wasting tokens and increasing latency

**Fix Applied:**
```python
def route_after_perception(state: AgentState) -> Literal["execution", "analysis"]:
    if state.get("next_action"):
        return "execution"  # Fast-path for simple tasks
    return "analysis"  # Full pipeline for complex tasks
```

---

### 2.2 Node State Preservation ✅

**Status:** VERIFIED  
**Files:** `analysis_node.py`, `planning_node.py`

**Changes:**
- Verified nodes preserve `next_action` if forced to run
- analysis_node has fast-path bypass
- planning_node wraps existing action in simple plan

---

## Summary

| Fix | Severity | Status |
|-----|----------|--------|
| Bash tool allowlist | CRITICAL | ✅ Fixed |
| Sandbox fail-closed | CRITICAL | ✅ Fixed |
| Symlink traversal | HIGH | ✅ Fixed |
| Fast-path routing | HIGH | ✅ Implemented |

---

## Testing Recommendations

1. **Bash Tool:** Test that restricted commands return proper error
2. **Sandbox:** Verify write operations fail when sandbox is unavailable
3. **Symlinks:** Create symlink outside workdir and verify it's blocked
4. **Fast-Path:** Run simple task (e.g., "list files") and verify skips analysis

---

*End of Fixes Applied Log*
