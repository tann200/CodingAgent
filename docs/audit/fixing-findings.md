# CodingAgent — Engineering Fix Directives

**Document version:** 3.0 (March 2026)
**Based on:** `docs/audit/audit-report.md` + post-fix validation of actual source
**Status key:** ✅ Done · ⚠️ Partial · ❌ Not started

---

## Phase 1 — Critical Security Fixes ✅ COMPLETE

### 1.1 Bash Tool Allowlist ✅
**Target:** `src/tools/file_tools.py:246-384`
Tiered allowlist implemented. Tier 1 (safe read-only), Tier 2 (test/compile), Tier 3
(restricted with `requires_approval: True`). Shell operators blocked pre-parse.

### 1.2 Sandbox Fail-Closed ✅
**Target:** `src/core/orchestration/orchestrator.py:879-886`
`except Exception as e:` now returns `{"ok": False, "error": "Sandbox validation aborted: ..."}`.
Previously continued execution silently.

### 1.3 Symlink Path Traversal ✅
**Target:** `src/tools/file_tools.py:43-50` (`_safe_resolve`)
`os.path.realpath` comparison against resolved workdir. Raises `PermissionError` if
symlink target escapes working directory.

---

## Phase 2 — Fast-Path Routing ✅ COMPLETE

### 2.1 Conditional Fast-Path After Perception ✅
**Target:** `src/core/orchestration/graph/builder.py:78-93, 407`
`route_after_perception()` routes directly to `execution` when `state["next_action"]`
is set, skipping `analysis` and `planning` nodes.

### 2.2 Node Bypass Preservation ✅
**Target:** `analysis_node.py:28-37`, `planning_node.py:69-78`
Both nodes detect `next_action` in state and short-circuit without overwriting it.

---

## Phase 3 — Remaining Open Items

---

### 3.1 Extend Read-Before-Edit to `write_file` and `edit_by_line_range` ❌

**Severity:** HIGH
**Audit ref:** audit-report §3.3, §15.1 (orchestrator.py:810-821)

#### Current State

`orchestrator.py:828-839` — the `execute_tool` method contains this guard:

```python
# Hard Rule Enforcement: Read before Edit
path_arg = args.get("path") or args.get("file_path")
if path_arg and name == "edit_file":          # line 830 — only edit_file
    try:
        resolved_path = str((Path(self.working_dir) / path_arg).resolve())
        if resolved_path not in self._session_read_files:
            return {
                "ok": False,
                "error": f"Security/Logic violation: You must read '{path_arg}' "
                         "before editing it...",
            }
    except Exception:
        pass
```

Two other write tools bypass this guard entirely:
- `write_file` — registered at `file_tools.py:55-70`, side_effects `["write"]`.
  Can silently clobber a file the agent has never read.
- `edit_by_line_range` — registered at `orchestrator.py:309-351`,
  side_effects `["write"]`. Replaces arbitrary line ranges without reading first.

`_session_read_files` is populated at `orchestrator.py:889-894` only when
`name == "read_file"`. The tracking mechanism is already correct — only the
coverage of the guard is too narrow.

#### Implementation

**File:** `src/core/orchestration/orchestrator.py`

Replace line 830:
```python
# BEFORE (line 830):
if path_arg and name == "edit_file":

# AFTER:
WRITE_TOOLS_REQUIRING_READ = {"edit_file", "write_file", "edit_by_line_range", "apply_patch"}
if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
```

No other changes needed. The rest of the block (lines 831-839) stays identical.

The constant should be defined once at class level or module level, not inline, so
tests can reference it:
```python
# Add near top of orchestrator.py, before the Orchestrator class:
WRITE_TOOLS_REQUIRING_READ = frozenset({
    "edit_file",
    "write_file",
    "edit_by_line_range",
    "apply_patch",
})
```

Then the check becomes:
```python
if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
```

#### Tests

**File to update:** `tests/unit/test_orchestrator_rules.py`

Existing test `test_read_before_edit_enforcement` only covers `edit_file`. Add three
new test functions to the same file:

```python
def test_write_file_requires_read_first(tmp_path):
    """write_file must be blocked if file not read first."""
    orch = Orchestrator(working_dir=str(tmp_path))
    test_file = tmp_path / "data.txt"
    test_file.write_text("original")

    res = orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "data.txt", "content": "overwrite"},
    })
    assert res["ok"] is False
    assert "must read" in res["error"]

    # Read first, then write succeeds
    orch.execute_tool({"name": "read_file", "arguments": {"path": "data.txt"}})
    res = orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "data.txt", "content": "overwrite"},
    })
    assert res["ok"] is True


def test_edit_by_line_range_requires_read_first(tmp_path):
    """edit_by_line_range must be blocked if file not read first."""
    orch = Orchestrator(working_dir=str(tmp_path))
    test_file = tmp_path / "code.py"
    test_file.write_text("line1\nline2\nline3\n")

    res = orch.execute_tool({
        "name": "edit_by_line_range",
        "arguments": {"path": "code.py", "start_line": 1, "end_line": 1,
                      "new_content": "replaced"},
    })
    assert res["ok"] is False
    assert "must read" in res["error"]

    # Read first, then edit succeeds
    orch.execute_tool({"name": "read_file", "arguments": {"path": "code.py"}})
    res = orch.execute_tool({
        "name": "edit_by_line_range",
        "arguments": {"path": "code.py", "start_line": 1, "end_line": 1,
                      "new_content": "replaced"},
    })
    assert res["ok"] is True


def test_new_file_write_blocked_without_prior_read(tmp_path):
    """Writing a brand-new file (no prior existence) also requires no read
    since the file cannot be read — the guard must not block new-file creation.
    A file that does not exist yet has no resolved path in _session_read_files,
    but the intent check should allow new files (path does not exist on disk)."""
    orch = Orchestrator(working_dir=str(tmp_path))

    # File does not exist yet — write should be allowed
    res = orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "new_file.txt", "content": "hello"},
    })
    # New files have no prior content to protect — creation should succeed
    # Implementation note: guard should check if file exists before blocking
    assert res["ok"] is True
```

> **Implementation note for new-file case:** The guard at line 830 resolves the path
> and checks `_session_read_files`. For a file that does not yet exist on disk, the
> guard should be skipped (the file has no existing content to corrupt).
> Add an existence check before the `_session_read_files` lookup:
> ```python
> resolved_path = str((Path(self.working_dir) / path_arg).resolve())
> file_exists = (Path(self.working_dir) / path_arg).exists()
> if file_exists and resolved_path not in self._session_read_files:
>     return {"ok": False, "error": "...must read..."}
> ```

#### Acceptance Criteria

- `write_file` on an existing, unread file returns `{"ok": False, "error": "...must read..."}`.
- `edit_by_line_range` on an existing, unread file returns the same error.
- `apply_patch` on an existing, unread file returns the same error.
- Reading the file first with `read_file`, then calling any of the above, succeeds.
- Creating a brand-new file with `write_file` (no prior file on disk) succeeds without a read.
- `delete_file` is NOT in the set — deletion does not require a prior read.
- The constant `WRITE_TOOLS_REQUIRING_READ` is importable from `orchestrator.py` so
  tests can assert tool membership.
- All three new tests pass; existing `test_read_before_edit_enforcement` still passes.

---

### 3.2 Wire RollbackManager into the Execution Flow ❌

**Severity:** HIGH
**Audit ref:** audit-report §3.5, fixes-applied gap note

#### Current State

`src/core/orchestration/rollback_manager.py` has a complete, tested API:
- `snapshot_files(file_paths, snapshot_id=None) -> str` — saves files to
  `.agent-context/snapshots/<id>.json`
- `rollback(snapshot_id=None) -> {"ok": bool, "restored_files": [...]}` — restores
  from the most recent snapshot
- `cleanup_old_snapshots(keep_last=5) -> int` — prunes old snapshots

`Orchestrator.__init__` already tracks `self._session_modified_files: set` (line 545)
but never populates it. This set is the natural trigger for snapshots.

Zero imports of `RollbackManager` exist anywhere other than its own test file.

`debug_node.py:53-58` is the correct rollback trigger point:
```python
if current_attempt >= max_attempts:
    return {
        "next_action": None,
        "errors": [f"Max debug attempts ({max_attempts}) reached"],
    }
```

#### Implementation — Three Steps

**Step A — Add RollbackManager to `Orchestrator.__init__`**
(`src/core/orchestration/orchestrator.py`, after line 548 where `self.seed` is set):

```python
# Automated rollback support
from src.core.orchestration.rollback_manager import RollbackManager
self.rollback_manager = RollbackManager(str(self.working_dir))
self._current_snapshot_id: Optional[str] = None
```

**Step B — Snapshot before each write, inside `execute_tool`**
(`src/core/orchestration/orchestrator.py`, insert after line 865 `if "write" in tool.get(...)`,
but BEFORE the sandbox validation block — snapshot only when the file exists):

```python
# Auto-snapshot before write operations for rollback support
if "write" in tool.get("side_effects", []) and path_arg:
    try:
        target_path = Path(self.working_dir) / path_arg
        if target_path.exists():            # only snapshot pre-existing files
            self._current_snapshot_id = self.rollback_manager.snapshot_files(
                [path_arg]
            )
            self._session_modified_files.add(path_arg)  # populate the tracking set
    except Exception as snap_err:
        guilogger.warning(f"Pre-write snapshot failed (non-blocking): {snap_err}")
```

Place this block between the read-before-edit guard (lines 828-839) and the sandbox
validation block (lines 864-886) so ordering is: guard → snapshot → sandbox → execute.

**Step C — Rollback when debug exhausts retries**
(`src/core/orchestration/graph/nodes/debug_node.py`, replace lines 53-58):

```python
if current_attempt >= max_attempts:
    logger.warning("debug_node: max attempts reached, attempting rollback")

    # Automated rollback: restore files to pre-edit state
    try:
        rollback_mgr = getattr(orchestrator, "rollback_manager", None)
        if rollback_mgr and rollback_mgr.current_snapshot:
            rb_result = rollback_mgr.rollback()
            logger.info(
                f"debug_node: rollback result — "
                f"restored {rb_result.get('restored_count', 0)} file(s): "
                f"{rb_result.get('restored_files', [])}"
            )
            rollback_mgr.cleanup_old_snapshots(keep_last=5)
        else:
            logger.warning("debug_node: no snapshot available for rollback")
    except Exception as rb_err:
        logger.warning(f"debug_node: rollback failed (non-fatal): {rb_err}")

    return {
        "next_action": None,
        "errors": [f"Max debug attempts ({max_attempts}) reached — rollback attempted"],
    }
```

#### Tests

**File to update:** `tests/unit/test_rollback_manager.py`

Add to `TestRollbackManager`:

```python
def test_orchestrator_creates_rollback_manager(self, temp_workdir):
    """Orchestrator.__init__ attaches a RollbackManager."""
    from src.core.orchestration.orchestrator import Orchestrator
    orch = Orchestrator(working_dir=temp_workdir)
    assert hasattr(orch, "rollback_manager")
    assert isinstance(orch.rollback_manager, RollbackManager)
    assert hasattr(orch, "_current_snapshot_id")

def test_snapshot_created_before_write(self, temp_workdir):
    """execute_tool snapshots an existing file before writing it."""
    from src.core.orchestration.orchestrator import Orchestrator
    orch = Orchestrator(working_dir=temp_workdir)
    target = Path(temp_workdir) / "output" / "target.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original")

    orch.execute_tool({"name": "read_file", "arguments": {"path": "target.txt"}})
    orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "target.txt", "content": "modified"},
    })

    # A snapshot JSON should exist
    snapshots = list((Path(temp_workdir) / "output" / ".agent-context" / "snapshots").glob("*.json"))
    assert len(snapshots) >= 1

def test_rollback_restores_file_after_debug_exhaustion(self, temp_workdir):
    """Full integration: write → snapshot → debug max → rollback restores."""
    from src.core.orchestration.orchestrator import Orchestrator
    orch = Orchestrator(working_dir=temp_workdir)

    workdir = Path(temp_workdir) / "output"
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "main.py"
    target.write_text("def foo(): pass\n")

    # Simulate: read, write (bad change), rollback manually
    orch.execute_tool({"name": "read_file", "arguments": {"path": "main.py"}})
    orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "main.py", "content": "BROKEN"},
    })
    assert target.read_text() == "BROKEN"

    # Trigger rollback via manager directly (integration with debug_node covered
    # by test_debug_node.py)
    result = orch.rollback_manager.rollback()
    assert result["ok"] is True
    assert target.read_text() == "def foo(): pass\n"
```

**File to update:** `tests/unit/test_debug_node.py`

Add to `TestDebugNode`:

```python
@pytest.mark.asyncio
async def test_debug_node_calls_rollback_on_max_attempts(self, mock_config):
    """debug_node triggers rollback when max attempts reached."""
    mock_rollback_mgr = MagicMock()
    mock_rollback_mgr.current_snapshot = "snap_001"
    mock_rollback_mgr.rollback.return_value = {"ok": True, "restored_count": 1,
                                                "restored_files": ["main.py"]}
    mock_config["configurable"]["orchestrator"].rollback_manager = mock_rollback_mgr

    state = {
        "task": "Fix bug", "history": [], "debug_attempts": 3,
        "max_debug_attempts": 3, "last_result": {}, "verification_result": {},
    }
    from src.core.orchestration.graph.nodes.debug_node import debug_node
    result = await debug_node(state, mock_config)

    assert result["next_action"] is None
    mock_rollback_mgr.rollback.assert_called_once()
    mock_rollback_mgr.cleanup_old_snapshots.assert_called_once_with(keep_last=5)

@pytest.mark.asyncio
async def test_debug_node_rollback_failure_is_non_fatal(self, mock_config):
    """If rollback raises, debug_node still returns max-attempts error."""
    mock_rollback_mgr = MagicMock()
    mock_rollback_mgr.current_snapshot = "snap_001"
    mock_rollback_mgr.rollback.side_effect = RuntimeError("disk full")
    mock_config["configurable"]["orchestrator"].rollback_manager = mock_rollback_mgr

    state = {
        "task": "Fix bug", "history": [], "debug_attempts": 3,
        "max_debug_attempts": 3, "last_result": {}, "verification_result": {},
    }
    from src.core.orchestration.graph.nodes.debug_node import debug_node
    result = await debug_node(state, mock_config)

    assert result["next_action"] is None
    assert "Max debug attempts" in result["errors"][0]
```

#### Acceptance Criteria

- `Orchestrator` constructor attaches `self.rollback_manager` (a `RollbackManager`
  instance) and `self._current_snapshot_id` (initially `None`).
- When `execute_tool` executes a write-side-effect tool against an existing file, a
  snapshot JSON appears in `.agent-context/snapshots/`.
- `self._session_modified_files` is populated with the path of every written file.
- When `debug_node` is called with `debug_attempts >= max_debug_attempts`,
  `rollback_manager.rollback()` is called exactly once.
- If `rollback_manager` raises or `current_snapshot` is `None`, `debug_node` still
  returns the max-attempts error without crashing.
- After rollback, the modified file's content matches the pre-write snapshot.
- `test_rollback_manager.py` — 3 new tests pass alongside existing 10.
- `test_debug_node.py` — 2 new tests pass alongside existing 9.

---

### 3.3 Wire SkillLearner in `memory_update_node` ❌

**Severity:** MEDIUM
**Audit ref:** audit-report §16.0 (post-fix finding)

#### Current State

`memory_update_node.py:12` imports `SkillLearner` from `advanced_features.py` but
never instantiates it. The import is dead.

`SkillLearner` in `advanced_features.py:277-328`:
- `create_skill(name, description, patterns, examples) -> str` — writes a Markdown
  skill file to `agent-brain/skills/<name>.md`
- `list_skills() -> List[str]` — returns stem names of existing skill files
- `get_skill(name) -> Optional[str]` — reads a skill by name

The memory_update_node already has `task`, `task_success`, `tool_sequence`, and
`current_plan` in scope (lines 54-59). These are exactly the inputs needed.

#### Implementation

**File:** `src/core/orchestration/graph/nodes/memory_update_node.py`

After the existing step 5 (RefactoringAgent block, ending around line 138), insert
step 6 before the closing `logger.info("=== memory_update_node END ===")`:

```python
# 6. Advanced Memory: Skill Learner
# Auto-generate a reusable skill from non-trivial successful completions.
# Only fires when: task succeeded, at least 2 tools were used (non-trivial),
# and no skill with this slug already exists.
try:
    if task_success and task and len(tool_sequence) >= 2:
        import re as _re
        skill_learner = SkillLearner(str(workdir_path))

        # Derive deterministic slug from task text
        skill_slug = _re.sub(r"[^a-z0-9]+", "_", task[:50].lower()).strip("_")

        if skill_slug and skill_slug not in skill_learner.list_skills():
            # Build pattern list from tool names used in this run
            tool_names = []
            for t in tool_sequence[:8]:
                content = t.get("content", "")
                # Extract first word (tool name) from content string
                first_word = content.strip().split()[0] if content.strip() else ""
                if first_word and first_word not in tool_names:
                    tool_names.append(first_word)

            skill_learner.create_skill(
                name=skill_slug,
                description=f"Auto-learned from: {task[:120]}",
                patterns=tool_names or ["General task pattern"],
                examples=[{
                    "task": task[:200],
                    "solution": str(current_plan)[:400],
                }],
            )
            logger.info(f"memory_update_node: created skill '{skill_slug}'")
except Exception as e:
    logger.warning(f"memory_update_node: skill learning failed (non-fatal): {e}")
```

#### Tests

**File to update:** `tests/unit/test_advanced_memory.py`

Existing tests cover `SkillLearner.create_skill`, `list_skills`, `get_skill` directly.
Add a test that covers the `memory_update_node` integration:

```python
class TestMemoryUpdateNodeSkillLearner:
    """Integration tests for SkillLearner wiring in memory_update_node."""

    @pytest.mark.asyncio
    async def test_skill_created_on_successful_task(self, tmp_path):
        """memory_update_node creates a skill file for non-trivial success."""
        from unittest.mock import patch, MagicMock

        workdir = str(tmp_path)
        state = {
            "task": "add logging to the application",
            "working_dir": workdir,
            "history": [],
            "evaluation_result": "complete",
            "current_plan": [{"description": "read file"}, {"description": "edit file"}],
            "session_id": "test-001",
        }

        # Supply a fake tool_sequence with 2+ entries
        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context"
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_tool_sequence",
            return_value=[{"content": "read_file arg"}, {"content": "write_file arg"}],
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_patch_from_history",
            return_value="",
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_modified_files",
            return_value=[],
        ):
            from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
            config = {"configurable": {"orchestrator": MagicMock()}}
            await memory_update_node(state, config)

        # Skill file must exist
        skill_dir = tmp_path / "agent-brain" / "skills"
        created_skills = list(skill_dir.glob("*.md")) if skill_dir.exists() else []
        assert len(created_skills) >= 1

    @pytest.mark.asyncio
    async def test_skill_not_created_for_single_tool_task(self, tmp_path):
        """memory_update_node does NOT create a skill for trivial (1-tool) tasks."""
        from unittest.mock import patch, MagicMock

        state = {
            "task": "list files",
            "working_dir": str(tmp_path),
            "history": [],
            "evaluation_result": "complete",
            "current_plan": [],
            "session_id": "test-002",
        }

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context"
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_tool_sequence",
            return_value=[{"content": "list_dir"}],   # only 1 tool
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_patch_from_history",
            return_value="",
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_modified_files",
            return_value=[],
        ):
            from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
            config = {"configurable": {"orchestrator": MagicMock()}}
            await memory_update_node(state, config)

        skill_dir = tmp_path / "agent-brain" / "skills"
        created_skills = list(skill_dir.glob("*.md")) if skill_dir.exists() else []
        assert len(created_skills) == 0

    @pytest.mark.asyncio
    async def test_duplicate_skill_not_created(self, tmp_path):
        """memory_update_node does not overwrite an existing skill with the same slug."""
        from src.core.memory.advanced_features import SkillLearner
        from unittest.mock import patch, MagicMock

        # Pre-create the skill
        sl = SkillLearner(str(tmp_path))
        sl.create_skill("add_logging_to_the_applicat", "existing", [], [])

        state = {
            "task": "add logging to the applicat",
            "working_dir": str(tmp_path),
            "history": [],
            "evaluation_result": "complete",
            "current_plan": [],
            "session_id": "test-003",
        }

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context"
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_tool_sequence",
            return_value=[{"content": "read_file"}, {"content": "write_file"}],
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_patch_from_history",
            return_value="",
        ), patch(
            "src.core.orchestration.graph.nodes.memory_update_node._extract_modified_files",
            return_value=[],
        ):
            from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
            config = {"configurable": {"orchestrator": MagicMock()}}
            await memory_update_node(state, config)

        # Still exactly 1 skill (not duplicated)
        skill_dir = tmp_path / "agent-brain" / "skills"
        created_skills = list(skill_dir.glob("*.md"))
        assert len(created_skills) == 1
```

#### Acceptance Criteria

- `memory_update_node` instantiates `SkillLearner` on successful task completion
  (`evaluation_result == "complete"`) when `len(tool_sequence) >= 2`.
- The created skill file is at `<workdir>/agent-brain/skills/<slug>.md` and contains
  the task description and tool patterns.
- Single-tool tasks (`len(tool_sequence) < 2`) produce no skill file.
- Failed tasks (`evaluation_result != "complete"`) produce no skill file.
- Re-running on the same task does not overwrite or duplicate the skill file.
- All failures are caught and logged as warnings — the node always returns `{}`.
- 3 new tests in `test_advanced_memory.py` pass.

---

### 3.4 Query `SymbolGraph` During Analysis ❌

**Severity:** HIGH
**Audit ref:** audit-report §6.2 — Symbol Graph Not Used

#### Current State

`src/core/indexing/symbol_graph.py` implements:
- `SymbolGraph(workdir)` — loads existing graph from `.agent-context/symbol_graph.json`
- `update_file(path: str)` — incremental AST parse, hash-based change detection,
  updates `self.nodes[rel_path]` and persists
- `find_calls(function_name: str) -> List[Dict]` — returns
  `[{"file": rel_path, "line": lineno}, ...]` for all files containing that function
- `find_tests_for_module(module_name: str) -> List[Dict]` — returns
  `[{"file": rel_path}, ...]` for test files matching the module name
- `get_all_symbols() -> Dict[str, Any]` — `{symbol_name: {"type", "file"}}` map

`analysis_node.py` calls `search_code` and `find_symbol` (repo tools), then collects
`relevant_files` and `key_symbols`. The `planning_node` receives these as
`analysis_summary`, `relevant_files`, `key_symbols` — but they contain only filenames
and no call-graph or test-location data.

**Known bug in `symbol_graph.py:129`:** `self.file_hashes[p]` uses a `Path` object as
the key when checking, but line 141 stores `self.file_hashes[str(p)]` with a string
key. The hash check therefore never hits, causing re-index on every call. This must
be fixed in the same diff.

#### Implementation

**File:** `src/core/orchestration/graph/nodes/analysis_node.py`

After the existing `relevant_files` and `key_symbols` collection block (around
lines 96-130), before the `return` statement, insert:

```python
# Symbol graph enrichment — call-graph and test-location context for planning
symbol_context_lines = []
try:
    from src.core.indexing.symbol_graph import SymbolGraph
    from pathlib import Path as _Path

    sg = SymbolGraph(working_dir)

    # Incrementally index the relevant .py files found above
    for fp in relevant_files[:10]:
        full_path = _Path(working_dir) / fp
        if full_path.exists() and full_path.suffix == ".py":
            sg.update_file(str(full_path))

    # Query callers for each key symbol
    for sym_name in key_symbols[:5]:
        callers = sg.find_calls(sym_name)
        if callers:
            caller_files = [c["file"] for c in callers[:4]]
            symbol_context_lines.append(
                f"  '{sym_name}' defined in: {', '.join(caller_files)}"
            )

    # Locate tests for the primary relevant module
    if relevant_files:
        module_stem = _Path(relevant_files[0]).stem
        test_hits = sg.find_tests_for_module(module_stem)
        if test_hits:
            test_files = [h["file"] for h in test_hits[:3]]
            symbol_context_lines.append(
                f"  Tests for '{module_stem}': {', '.join(test_files)}"
            )

    if symbol_context_lines:
        logger.info(
            f"analysis_node: symbol graph enriched {len(symbol_context_lines)} entries"
        )
except Exception as e:
    logger.warning(f"analysis_node: symbol graph enrichment failed (non-fatal): {e}")

# Append symbol context to analysis summary
if symbol_context_lines:
    analysis_summary += "\nSymbol graph:\n" + "\n".join(symbol_context_lines)
```

Update the `return` dict so `analysis_summary` is constructed before the append:

```python
return {
    "analysis_summary": analysis_summary,
    "relevant_files": relevant_files,
    "key_symbols": key_symbols,
    "repo_summary_data": repo_summary_data,
}
```

**Bug fix in `src/core/indexing/symbol_graph.py:129`:**
```python
# BEFORE (line 129):
if p in self.file_hashes and self.file_hashes[p] == current_hash:

# AFTER:
if str(p) in self.file_hashes and self.file_hashes[str(p)] == current_hash:
```

#### Tests

**File to update:** `tests/unit/test_graph_nodes.py`

Add a new test class after the existing `TestPlanningNode`:

```python
class TestAnalysisNodeSymbolGraph:
    """Tests for symbol graph enrichment in analysis_node."""

    @pytest.fixture
    def mock_orchestrator(self, tmp_path):
        orch = MagicMock()
        orch.tool_registry = MagicMock()
        orch.tool_registry.get.return_value = None  # disable repo tools
        return orch

    @pytest.fixture
    def mock_config(self, mock_orchestrator):
        return {"configurable": {"orchestrator": mock_orchestrator}}

    @pytest.mark.asyncio
    async def test_analysis_node_enriches_summary_with_symbol_graph(
        self, tmp_path, mock_config
    ):
        """analysis_node appends symbol graph data to analysis_summary."""
        # Create a real Python file so SymbolGraph can parse it
        py_file = tmp_path / "mymodule.py"
        py_file.write_text("def my_function():\n    pass\n")

        state = _make_state(
            task="fix my_function",
            working_dir=str(tmp_path),
            relevant_files=["mymodule.py"],
            key_symbols=["my_function"],
            analysis_summary="Found 1 file.",
        )

        with patch(
            "src.core.orchestration.graph.nodes.analysis_node.generate_repo_summary",
            return_value={"status": "ok", "summary": "test repo"},
        ), patch.object(
            mock_config["configurable"]["orchestrator"],
            "tool_registry",
        ):
            result = await analysis_node(state, mock_config)

        assert "analysis_summary" in result
        # Symbol graph should have indexed mymodule.py and found my_function
        assert "symbol" in result["analysis_summary"].lower() or \
               "my_function" in result["analysis_summary"] or \
               "mymodule" in result["analysis_summary"]

    @pytest.mark.asyncio
    async def test_analysis_node_symbol_graph_failure_is_non_fatal(
        self, mock_config
    ):
        """If symbol graph raises, analysis_node still returns a valid result."""
        state = _make_state(
            task="fix something",
            working_dir="/nonexistent/path",
            relevant_files=["foo.py"],
        )

        with patch(
            "src.core.orchestration.graph.nodes.analysis_node.generate_repo_summary",
            return_value={"status": "error"},
        ):
            result = await analysis_node(state, mock_config)

        assert "analysis_summary" in result
        assert result["relevant_files"] is not None
```

**File to create (or update):** `tests/unit/test_indexing.py` — add to existing file:

```python
class TestSymbolGraphHashBug:
    """Test the Path vs str hash key bug fix."""

    def test_update_file_uses_str_key_for_hash(self, tmp_path):
        """file_hashes must store and check str keys, not Path keys."""
        from src.core.indexing.symbol_graph import SymbolGraph

        py_file = tmp_path / "test_mod.py"
        py_file.write_text("def foo(): pass\n")

        sg = SymbolGraph(str(tmp_path))
        sg.update_file(str(py_file))

        # Key must be a string, not a Path object
        for key in sg.file_hashes.keys():
            assert isinstance(key, str), f"Expected str key, got {type(key)}"

    def test_second_update_hits_cache(self, tmp_path):
        """Unchanged file must not re-index (hash cache works)."""
        from src.core.indexing.symbol_graph import SymbolGraph

        py_file = tmp_path / "test_mod.py"
        py_file.write_text("def foo(): pass\n")

        sg = SymbolGraph(str(tmp_path))
        sg.update_file(str(py_file))
        first_update_time = sg.nodes.get(
            str(py_file.relative_to(tmp_path)), {}
        ).get("updated_at")

        # Update again without changing the file
        sg.update_file(str(py_file))
        second_update_time = sg.nodes.get(
            str(py_file.relative_to(tmp_path)), {}
        ).get("updated_at")

        # Timestamps should be identical (cache hit = no re-parse)
        assert first_update_time == second_update_time
```

#### Acceptance Criteria

- After `analysis_node` runs on a task with `.py` files in `relevant_files`, the
  returned `analysis_summary` contains a "Symbol graph:" section when callers or
  tests are found.
- `SymbolGraph.update_file` is called for each `.py` file in `relevant_files` (up
  to 10 files).
- `find_calls` results are formatted as `'symbol_name' defined in: file1, file2`.
- `find_tests_for_module` results are formatted as `Tests for 'module': test_file`.
- Symbol graph enrichment failure (import error, parse error, disk error) is caught
  and logged as a warning; `analysis_node` returns normally with unmodified results.
- **Bug fix:** `file_hashes` keys are always strings. A file updated twice without
  modification is parsed only once (cache hit on second call).
- 2 new tests in `test_graph_nodes.py` pass.
- 2 new tests in `test_indexing.py` pass.

---

### 3.5 Enable Plan Validator `enforce_warnings` by Default ❌

**Severity:** MEDIUM
**Audit ref:** audit-report §4.2 — Plan Validator Superficial

#### Current State

`plan_validator_node.py:181-182`:
```python
enforce_warnings = state.get("plan_enforce_warnings", False)   # always lenient
strict_mode = state.get("plan_strict_mode", False)
```

With both defaults `False`, the following happen silently (warnings, not errors):
- Plan has no verification step → warning only, plan passes, execution proceeds
- Edit-without-read in action objects → warning only, plan passes

The validator is effectively decorative unless the caller explicitly sets
`plan_enforce_warnings=True` in the initial state — which nothing does.

`test_plan_validator.py:27-38` asserts `result["valid"] is True` for a plan without
verification (`test_valid_plan_no_verification_warning`). This test calls
`validate_plan(plan)` directly (not through the node), so it tests the function with
`enforce_warnings=False` and will not break. But `test_plan_validator.py:78-122`
tests `plan_validator_node` with the state — this test will be affected by the default change.

#### Implementation

**File:** `src/core/orchestration/graph/nodes/plan_validator_node.py`

Change lines 181-182:
```python
# BEFORE:
enforce_warnings = state.get("plan_enforce_warnings", False)
strict_mode = state.get("plan_strict_mode", False)

# AFTER:
enforce_warnings = state.get("plan_enforce_warnings", True)   # default: treat warnings as errors
strict_mode = state.get("plan_strict_mode", False)             # strict remains opt-in
```

No other changes needed in `plan_validator_node.py`.

#### Tests to Update

**File:** `tests/unit/test_plan_validator.py`

`test_valid_plan_passes` (line 78-122) uses a plan with steps `"Read file"` and
`"Run tests"`. `"Run tests"` matches the `has_verification` keywords, so this test
still passes ✅.

`test_invalid_plan_fails` (line 125-166) uses an empty plan — still fails ✅.

`test_no_plan_returns_error` (line 169-210) uses `current_plan=None` — still
returns error ✅.

Add two new test methods to `TestPlanValidatorNode` to cover the new default:

```python
@pytest.mark.asyncio
async def test_plan_without_verification_now_fails_by_default(self):
    """With enforce_warnings=True default, a plan missing verification is rejected."""
    state = {  # minimal valid state dict
        **{k: None for k in [
            "task", "history", "verified_reads", "next_action", "last_result",
            "analysis_summary", "relevant_files", "key_symbols", "debug_attempts",
            "verification_passed", "verification_result", "task_decomposed",
            "tool_last_used", "files_read", "repo_summary_data", "replan_required",
            "action_failed", "plan_progress", "evaluation_result", "cancel_event",
        ]},
        "task": "add feature",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "errors": [],
        "current_plan": [
            {"description": "Read main.py"},
            {"description": "Edit main.py to add feature"},
            # NOTE: no verification/test step
        ],
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "max_debug_attempts": 3,
        "step_controller_enabled": False,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        # enforce_warnings NOT set — uses new default True
    }
    result = await plan_validator_node(state, None)
    # Plan should now be rejected due to missing verification step
    assert result["action_failed"] is True
    assert any("verification" in w.lower()
               for w in result["plan_validation"].get("warnings", []) +
                        result["plan_validation"].get("errors", []))

@pytest.mark.asyncio
async def test_plan_validator_lenient_mode_via_state_flag(self):
    """Setting plan_enforce_warnings=False restores lenient behaviour."""
    state = {
        **{k: None for k in [
            "history", "verified_reads", "next_action", "last_result",
            "analysis_summary", "relevant_files", "key_symbols", "debug_attempts",
            "verification_passed", "verification_result", "task_decomposed",
            "tool_last_used", "files_read", "repo_summary_data", "replan_required",
            "action_failed", "plan_progress", "evaluation_result", "cancel_event",
        ]},
        "task": "read a file",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "errors": [],
        "current_plan": [
            {"description": "Read config.yaml"},
            {"description": "Display contents"},
        ],
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "max_debug_attempts": 3,
        "step_controller_enabled": False,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        "plan_enforce_warnings": False,  # explicit opt-out
    }
    result = await plan_validator_node(state, None)
    assert result["action_failed"] is False
```

**File:** `tests/unit/test_plan_validator_enhanced.py`

`test_enforce_warnings_treats_warnings_as_errors` (line 26-37) calls `validate_plan`
directly with explicit `enforce_warnings=False/True`. Not affected by node default.
No changes needed ✅.

#### Acceptance Criteria

- `plan_validator_node` with a plan that has no verification step and no
  `plan_enforce_warnings` key in state returns `action_failed=True` (new default).
- `plan_validator_node` with `plan_enforce_warnings=False` in state returns
  `action_failed=False` for the same plan (lenient opt-out works).
- `validate_plan(plan)` called directly (without going through the node) is
  unaffected — it still accepts a `enforce_warnings=False` argument that defaults
  to `False` at the function level. The default change is only in the node.
- All existing `test_plan_validator.py` tests still pass (none of them use a plan
  without verification and expect `action_failed=False`).
- 2 new node-level tests added to `test_plan_validator.py` pass.

---

### 3.6 Wire `SessionStore` for Tool Call and Plan Persistence ❌

**Severity:** MEDIUM
**Audit ref:** audit-report §13 — SessionStore Unused

#### Current State

`src/core/memory/session_store.py` is complete:
- `add_tool_call(session_id, tool_name, args, result, success)` — inserts row into
  `tool_calls` table
- `add_plan(session_id, plan, status)` — inserts into `plans` table
- `add_error(session_id, error_type, error_message, context)` — inserts into `errors`
- `get_session_summary(session_id)` — returns counts

`Orchestrator` already has `self._current_task_id: Optional[str]` (line 556) which
is the natural `session_id`. The `execute_tool` method runs every tool call — ideal
insertion point.

The SQLite file will be created at `<workdir>/.agent-context/session.db` on first
`SessionStore` init.

#### Implementation — Three Steps

**Step A — Init `SessionStore` in `Orchestrator.__init__`**
(after `self.rollback_manager` line from 3.2, around line 549-552):

```python
from src.core.memory.session_store import SessionStore
self.session_store = SessionStore(str(self.working_dir))
```

**Step B — Log each tool call in `execute_tool`**
(after line 929 `res = self._normalize_tool_result(res)`, before the role-setter
check at line 931):

```python
# Persist tool call to session store (best-effort)
try:
    import time as _time
    session_id = str(getattr(self, "_current_task_id", None) or "default")
    success_flag = not (isinstance(res, dict) and res.get("ok") is False)
    self.session_store.add_tool_call(
        session_id=session_id,
        tool_name=name,
        args={k: str(v)[:200] for k, v in args.items()},   # truncate for storage
        result={"status": res.get("status", "ok")} if isinstance(res, dict) else {},
        success=success_flag,
    )
except Exception:
    pass  # never block execution
```

**Step C — Log each plan in `planning_node`**
(`src/core/orchestration/graph/nodes/planning_node.py`, after `if steps:` at line 156):

```python
# Persist plan to session store
try:
    import json as _json
    orch = _resolve_orchestrator(state, config)
    if orch and hasattr(orch, "session_store"):
        orch.session_store.add_plan(
            session_id=str(state.get("session_id") or "default"),
            plan=_json.dumps(steps[:20]),  # cap at 20 steps
        )
except Exception:
    pass
```

**Step D — Log debug errors in `debug_node`**
(`src/core/orchestration/graph/nodes/debug_node.py`, after extracting `error_summary`
around line 51):

```python
# Persist error to session store
try:
    if error_summary and hasattr(orchestrator, "session_store"):
        session_id = str(state.get("session_id") or "default")
        orchestrator.session_store.add_error(
            session_id=session_id,
            error_type=_classify_error(error_summary),   # reuse from 3.7
            error_message=error_summary[:500],
            context={"attempt": current_attempt, "task": task[:100]},
        )
except Exception:
    pass
```

#### Tests

**New file:** `tests/unit/test_session_store_integration.py`

```python
"""Integration tests for SessionStore wiring in Orchestrator and graph nodes."""
import pytest
from pathlib import Path
from src.core.orchestration.orchestrator import Orchestrator
from src.core.memory.session_store import SessionStore


def test_orchestrator_creates_session_store(tmp_path):
    """Orchestrator attaches a SessionStore on init."""
    orch = Orchestrator(working_dir=str(tmp_path))
    assert hasattr(orch, "session_store")
    assert isinstance(orch.session_store, SessionStore)


def test_session_db_created_on_init(tmp_path):
    """SQLite file is created when Orchestrator starts."""
    Orchestrator(working_dir=str(tmp_path))
    db_path = Path(tmp_path) / "output" / ".agent-context" / "session.db"
    assert db_path.exists()


def test_tool_call_logged_after_execution(tmp_path):
    """execute_tool persists a row to session_store.tool_calls."""
    orch = Orchestrator(working_dir=str(tmp_path))
    orch._current_task_id = "task-001"

    # Execute a safe, read-only tool
    target = Path(tmp_path) / "output" / "file.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello")

    orch.execute_tool({"name": "read_file", "arguments": {"path": "file.txt"}})

    calls = orch.session_store.get_tool_calls("task-001")
    assert len(calls) >= 1
    assert calls[0]["tool_name"] == "read_file"
    assert calls[0]["success"] is True


def test_failed_tool_call_logged_as_failure(tmp_path):
    """execute_tool logs success=False for a rejected call."""
    orch = Orchestrator(working_dir=str(tmp_path))
    orch._current_task_id = "task-002"

    # Call a nonexistent tool
    orch.execute_tool({"name": "nonexistent_tool", "arguments": {}})

    calls = orch.session_store.get_tool_calls("task-002")
    # tool-not-found returns {"ok": False} — should be logged as failure
    assert any(not c["success"] for c in calls)


def test_session_store_failure_does_not_block_execution(tmp_path):
    """If session_store.add_tool_call raises, execute_tool still returns result."""
    from unittest.mock import MagicMock
    orch = Orchestrator(working_dir=str(tmp_path))
    orch.session_store = MagicMock()
    orch.session_store.add_tool_call.side_effect = RuntimeError("db locked")

    target = Path(tmp_path) / "output" / "file.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("content")

    result = orch.execute_tool({"name": "read_file", "arguments": {"path": "file.txt"}})
    assert result["ok"] is True   # execution completed despite store failure
```

#### Acceptance Criteria

- `Orchestrator.__init__` attaches `self.session_store` as a `SessionStore` instance.
- After every `execute_tool` call, a row exists in the `tool_calls` table for the
  matching `_current_task_id`.
- Successful tool calls have `success=1`; calls returning `{"ok": False}` have
  `success=0`.
- After `planning_node` generates a plan, a row exists in the `plans` table.
- After `debug_node` encounters an error, a row exists in the `errors` table.
- If `add_tool_call` raises for any reason, `execute_tool` still completes normally.
- The SQLite file is created at `<workdir>/.agent-context/session.db` on first
  `Orchestrator` instantiation.
- 5 new tests in `test_session_store_integration.py` pass.

---

### 3.7 Structured Error Classification in `debug_node` ❌

**Severity:** MEDIUM
**Audit ref:** audit-report §4.1 — No Structured Debug Loop

#### Current State

`debug_node.py:62-74` assembles `fix_prompt` with raw `error_summary` but no
classification. The LLM receives an undifferentiated blob and must infer the error
type itself. `TestDebugNodeErrorClassification` in `test_debug_node.py` (lines 112-144)
checks string contents directly rather than testing a real classifier function.

#### Implementation

**File:** `src/core/orchestration/graph/nodes/debug_node.py`

Add `_classify_error` as a module-level function before `debug_node`:

```python
# Error type constants
_ERROR_TYPES = {
    "syntax_error":  (
        "Fix the syntax error. Check indentation, colons after def/class/if, "
        "and unclosed brackets/parentheses."
    ),
    "import_error":  (
        "Fix the import. Verify the module name spelling and that it is installed. "
        "Check for circular imports."
    ),
    "test_failure":  (
        "A test assertion failed. Read the failing test carefully to understand what "
        "it expects, then fix the implementation to match."
    ),
    "lint_error":    (
        "Fix the linting issue. Common fixes: split long lines (>79 chars), add "
        "blank lines between functions, remove unused imports."
    ),
    "runtime_error": (
        "Fix the runtime error. Check attribute names, verify types match, and add "
        "None-safety guards where needed."
    ),
    "unknown_error": (
        "Analyze the error output carefully and generate a targeted fix tool call."
    ),
}


def _classify_error(error_summary: str) -> str:
    """Classify an error string into a known error category."""
    s = error_summary.lower()
    if any(t in s for t in ("syntaxerror", "indentationerror", "invalid syntax",
                             "unexpected indent", "unexpected eof")):
        return "syntax_error"
    if any(t in s for t in ("importerror", "modulenotfounderror", "no module named",
                             "cannot import name")):
        return "import_error"
    if ("assertionerror" in s
            or ("failed" in s and "test" in s)
            or "pytest" in s
            or "assert " in s):
        return "test_failure"
    if any(t in s for t in ("e501", "e302", "e303", "e401", "flake8", "pylint",
                             "ruff", "line too long", "pep8")):
        return "lint_error"
    if any(t in s for t in ("typeerror", "attributeerror", "nameerror",
                             "valueerror", "keyerror", "indexerror")):
        return "runtime_error"
    return "unknown_error"
```

Then replace `fix_prompt` construction (lines 62-74) with:

```python
error_type = _classify_error(error_summary)
type_guidance = _ERROR_TYPES[error_type]

fix_prompt = f"""You are a debugging assistant. Attempt {next_attempt}/{max_attempts}.

Task: {task}

Error type: {error_type.replace("_", " ").title()}
Error details:
{error_summary}

Guidance: {type_guidance}

Respond with ONLY a YAML tool call to fix the issue."""
```

#### Tests

**File to update:** `tests/unit/test_debug_node.py`

Replace the existing `TestDebugNodeErrorClassification` class (which only checks raw
string values and has no real function to call) with tests that import and call
`_classify_error` directly:

```python
class TestClassifyError:
    """Unit tests for _classify_error function."""

    def setup_method(self):
        from src.core.orchestration.graph.nodes.debug_node import _classify_error
        self.classify = _classify_error

    def test_syntax_error(self):
        assert self.classify("SyntaxError: invalid syntax on line 5") == "syntax_error"

    def test_indentation_error(self):
        assert self.classify("IndentationError: unexpected indent") == "syntax_error"

    def test_import_error(self):
        assert self.classify("ModuleNotFoundError: No module named 'requests'") == "import_error"

    def test_cannot_import_name(self):
        assert self.classify("ImportError: cannot import name 'foo' from 'bar'") == "import_error"

    def test_test_failure_from_pytest(self):
        assert self.classify("FAILED tests/test_foo.py::test_bar - AssertionError") == "test_failure"

    def test_assertion_error(self):
        assert self.classify("AssertionError: expected 1 but got 2") == "test_failure"

    def test_lint_e501(self):
        assert self.classify("E501 line too long (92 > 79 characters)") == "lint_error"

    def test_lint_ruff(self):
        assert self.classify("ruff: F401 unused import") == "lint_error"

    def test_type_error(self):
        assert self.classify("TypeError: 'NoneType' object is not subscriptable") == "runtime_error"

    def test_name_error(self):
        assert self.classify("NameError: name 'x' is not defined") == "runtime_error"

    def test_attribute_error(self):
        assert self.classify("AttributeError: 'Foo' object has no attribute 'bar'") == "runtime_error"

    def test_unknown_error(self):
        assert self.classify("Something went wrong") == "unknown_error"

    def test_empty_string(self):
        assert self.classify("") == "unknown_error"

    def test_case_insensitive(self):
        assert self.classify("SYNTAXERROR: INVALID SYNTAX") == "syntax_error"


class TestDebugNodePromptEnrichment:
    """Tests that _classify_error result is embedded in the LLM prompt."""

    @pytest.mark.asyncio
    async def test_prompt_contains_error_type(self, mock_state, mock_config,
                                               monkeypatch):
        """The LLM receives an error-type header in the prompt."""
        captured_messages = []

        def fake_call_model(messages, **kwargs):
            captured_messages.extend(messages)
            return {"choices": [{"message": {"content": ""}}]}

        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "test"}]
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda: mock_builder,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            fake_call_model,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            lambda x: None,
        )

        from src.core.orchestration.graph.nodes.debug_node import debug_node
        await debug_node(mock_state, mock_config)

        # The task_description passed to build_prompt must reference the error type
        call_args = mock_builder.build_prompt.call_args
        task_desc = call_args.kwargs.get("task_description", "")
        assert "error type" in task_desc.lower() or "Error type" in task_desc
```

#### Acceptance Criteria

- `_classify_error` is a public module-level function in `debug_node.py`, importable
  in tests.
- Classification is deterministic and case-insensitive.
- All 6 error categories are correctly identified for representative inputs.
- Unrecognised input returns `"unknown_error"` (never raises).
- The `fix_prompt` passed to `call_model` contains an "Error type:" line with the
  classification and a "Guidance:" line with category-specific advice.
- Existing 9 tests in `TestDebugNode` and `TestDebugNodeRetry` pass unchanged.
- `TestDebugNodeErrorClassification` is replaced by `TestClassifyError` (14 tests).
- 1 new test in `TestDebugNodePromptEnrichment` passes.

---

## Phase 4 — Advanced Items ✅ COMPLETE

### 4.1 Integrate `ContextController` for Token Budget Enforcement ✅

**Severity:** LOW | **Target:** `src/core/context/context_controller.py`,
`src/core/context/context_builder.py`

`ContextController` exists with per-section budget tracking but `ContextBuilder` uses
`len(s)/4` estimates and drops oldest messages without controller oversight.

**Fix direction:**
- Import `ContextController` in `ContextBuilder.__init__`.
- Before assembling each section, call `controller.check_budget(section, tokens)`.
- Replace `len(s)/4` with `tiktoken.encode` when available (graceful fallback).

**Tests needed:** Add `test_context_controller_budget_enforcement` to
`tests/unit/test_context_builder.py`.

---

### 4.2 Wire Hub-and-Spoke Multi-Agent ✅

**Severity:** LOW | **Target:** `src/core/orchestration/graph_factory.py`

`create_planner_graph()`, `create_coder_graph()`, `create_reviewer_graph()`,
`create_researcher_graph()`, and `HubAndSpokeCoordinator` (lines 44-209) are dead code.

**Fix direction:**
- Design a task-routing layer that inspects task type and selects the sub-graph.
- Research-only tasks → `researcher_graph`. Code-review tasks → `reviewer_graph`.
- Complex multi-step → `HubAndSpokeCoordinator`.
- Requires significant architectural work; implement only after 3.1–3.7 complete.

---

### 4.3 Semantic Search via `vector_store` in Analysis ✅

**Severity:** HIGH (long-term) | **Target:** `src/core/indexing/vector_store.py`,
`analysis_node.py`

`vector_store.py` has LanceDB + sentence-transformer embeddings but `analysis_node`
uses keyword-based `search_code`. Semantic search on `task_description` would surface
more relevant symbols.

**Fix direction:**
- After `index_repository()`, embed symbols into the vector store.
- In `analysis_node`, call `vector_store.search(task, top_k=10)` before keyword search.
- Fall back to keyword search when `sentence-transformers` is unavailable.

**Tests needed:** `tests/unit/test_vector_store_content.py` (already exists —
extend it).

---

### 4.4 Plan Persistence Between Sessions ✅

**Severity:** MEDIUM (long-term) | **Target:** `src/core/orchestration/graph/nodes/planning_node.py`

**Fix direction:**
- At end of `planning_node`, write `current_plan` to
  `.agent-context/last_plan.json`.
- At start, if `current_plan` is empty and `last_plan.json` exists, load it and
  set `task_decomposed=True`.

**Tests needed:** Add to `tests/unit/test_graph_nodes.py`.

---

## Implementation Order

```
3.1 (1 line change, highest safety impact)
  → 3.5 (1 line change, immediate validator improvement)
  → 3.7 (pure addition, no existing code modified)
  → 3.2 (3 callsites, high safety value)
  → 3.4 (analysis enrichment + bug fix)
  → 3.3 (memory node addition)
  → 3.6 (persistence layer)
  → 4.3 → 4.4 → 4.1 → 4.2
```

---

## Current Status Summary

| # | Item | Severity | Status | Primary File |
|---|------|----------|--------|--------------|
| 1.1 | Bash allowlist tiered | CRITICAL | ✅ Done | `file_tools.py:246` |
| 1.2 | Sandbox fail-closed | CRITICAL | ✅ Done | `orchestrator.py:879` |
| 1.3 | Symlink traversal | HIGH | ✅ Done | `file_tools.py:43` |
| 2.1 | Fast-path routing | HIGH | ✅ Done | `builder.py:78` |
| 2.2 | Node bypass preservation | MEDIUM | ✅ Done | `analysis_node.py:28`, `planning_node.py:69` |
| 2.3 | Advanced memory 4/5 | HIGH | ✅ Done | `memory_update_node.py` |
| — | Context builder dup return | HIGH | ✅ Done | `context_builder.py:255` |
| — | Plan parsing 4-strategy | MEDIUM | ✅ Done | `planning_node.py:165` |
| — | Incremental indexing | HIGH | ✅ Done | `repo_indexer.py` |
| — | Multi-language indexing | MEDIUM | ✅ Done | `repo_indexer.py` |
| — | Deterministic + seed | HIGH | ✅ Done | `orchestrator.py:533` |
| — | Scenario evaluator | HIGH | ✅ Done | `src/core/evaluation/scenario_evaluator.py` |
| — | WorkspaceGuard in tools | MEDIUM | ✅ Done | `file_tools.py` |
| 3.1 | Read-before-edit all writes | HIGH | ✅ Done | `orchestrator.py:828` |
| 3.2 | RollbackManager wired | HIGH | ✅ Done | `orchestrator.py`, `debug_node.py` |
| 3.3 | SkillLearner wired | MEDIUM | ✅ Done | `memory_update_node.py` |
| 3.4 | SymbolGraph in analysis | HIGH | ✅ Done | `analysis_node.py` |
| 3.5 | Plan validator default | MEDIUM | ✅ Done | `plan_validator_node.py:181` |
| 3.6 | SessionStore wired | MEDIUM | ✅ Done | `orchestrator.py` |
| 3.7 | Debug error classification | MEDIUM | ✅ Done | `debug_node.py` |
| 4.1 | ContextController | LOW | ✅ Done | Wired as Phase 3 of `analysis_node.py`; bug in `get_relevant_snippets` also fixed |
| 4.2 | Hub-and-spoke multi-agent | LOW | ✅ Done | Verified already wired via `subagent_tools.py`; doc corrected |
| 4.3 | Vector store semantic search | HIGH | ✅ Done | Bug fixed: `top_k` → `limit`; `sg.update()` → `sg.update_file()` |
| 4.4 | Plan persistence | MEDIUM | ✅ Done | `planning_node.py` - saves/loads `last_plan.json` |

---

## Non-Negotiable Implementation Rules

1. **Non-fatal principle.** Every new integration block (`RollbackManager`,
   `SessionStore`, `SkillLearner`, `SymbolGraph`) must be wrapped in `try/except`.
   Failures are `logger.warning`, never `logger.error` that blocks flow.

2. **No new files without justification.** `test_session_store_integration.py` is
   the only new file permitted. All other tests go into existing test files.

3. **Read before edit.** Before modifying any source file, confirm current line
   numbers by reading the file. The line numbers above are accurate for March 2026
   but may drift after earlier fixes are applied.

4. **One change per item.** Do not fold multiple items into a single commit. Each
   item should be independently reviewable and revertable.

5. **Tests must call real code.** Avoid tests that only assert string membership
   (like the existing `TestDebugNodeErrorClassification`). Replace with tests that
   import the actual function under test.
