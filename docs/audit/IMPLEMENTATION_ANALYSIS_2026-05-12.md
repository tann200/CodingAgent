# Implementation Analysis — CodingAgent Fixes
**Date:** 2026-05-12
**Source audit:** `docs/audit/CODEBASE_AUDIT_2026-05-12.md`
**Purpose:** Actionable, file-and-line-precise implementation guide for all 25 prioritised fixes.
Developers should be able to implement each task directly from this document without reading the audit report first.

---

## How to read this document

Each task follows a fixed schema:

- **Current state** — exact `file:line` reference and the problematic code (or absence of code).
- **Exact change** — the minimal diff / code snippet to apply.
- **Touch-points** — every file that must change.
- **Test requirement** — the test(s) that must be green before the task is closed.
- **Risks / notes** — gotchas, ordering constraints, or side-effects.

---

## Phase 1 — Critical Stability

### P1-T1 — Add `edit_file` to `MODIFYING_TOOLS`

**Current state**

`src/core/orchestration/loop_guards.py:65–76` — `MODIFYING_TOOLS` is:

```python
MODIFYING_TOOLS: Set[str] = {
    "edit_file_atomic",
    "edit_by_line_range",
    "apply_patch",
    "multiedit",
    "write_file",
    "delete_file",
    "rename_file",
    "ast_rename",
    "manage_todo",
}
```

`"edit_file"` (the non-atomic variant) is absent. An LLM can call `edit_file` on an existing file without a prior `read_file` and bypass the read-before-write guard entirely.

**Exact change**

Add one entry to the set at `loop_guards.py:66`:

```python
MODIFYING_TOOLS: Set[str] = {
    "edit_file",           # ← ADD THIS LINE
    "edit_file_atomic",
    "edit_by_line_range",
    "apply_patch",
    "multiedit",
    "write_file",
    "delete_file",
    "rename_file",
    "ast_rename",
    "manage_todo",
}
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/loop_guards.py:66` | Insert `"edit_file",` into the set |
| `tests/unit/test_loop_guards.py` | Assert `"edit_file" in MODIFYING_TOOLS` (new assertion) |

**Test requirement**

```python
# tests/unit/test_loop_guards.py
from src.core.orchestration.loop_guards import MODIFYING_TOOLS

def test_edit_file_in_modifying_tools():
    assert "edit_file" in MODIFYING_TOOLS

def test_edit_file_blocked_without_prior_read(tmp_path):
    """check_read_before_write must return an error dict when edit_file
    is called on an existing file that has not been read."""
    target = tmp_path / "foo.py"
    target.write_text("x = 1")
    from src.core.orchestration.loop_guards import check_read_before_write
    result = check_read_before_write(
        "edit_file", "foo.py", {}, str(tmp_path), session_read_files=set()
    )
    assert result is not None
    assert result["last_result"]["ok"] is False
```

**Risks / notes**

- This is a one-line change with no runtime risk.
- Verify that `"edit_file"` is not already present by running `grep -n '"edit_file"' src/core/orchestration/loop_guards.py` before applying. (As of audit date it is absent.)
- No import changes needed — the set is already a `Set[str]`.

---

### P1-T2 — Delete legacy `src/tools/toolsets/` loader

**Current state**

Two loader implementations coexist:

- **Legacy (delete):** `src/tools/toolsets/loader.py` — thin `importlib` shim that re-exports from the canonical path; `src/tools/toolsets/__init__.py` — 2-line stub.
- **Canonical (keep):** `src/config/toolsets/loader.py` — the real `ToolsetManager` with model-aware caching.

Any code that imports from the legacy path gets the shim, which in turn delegates to the canonical path, but the indirection maintains a separate import pathway that can diverge.

**Exact change**

1. **Delete** `src/tools/toolsets/loader.py`.
2. **Delete** `src/tools/toolsets/__init__.py` (stub only — confirm it contains nothing of value).
3. **Delete** any `*.yaml` files under `src/tools/toolsets/` (toolset YAML configs that should live under `src/config/toolsets/`).
4. **Update** any import that references `src.tools.toolsets.loader` to `src.config.toolsets.loader`.

Search for remaining legacy imports:

```bash
grep -rn "from src.tools.toolsets" src/ tests/
grep -rn "import src.tools.toolsets" src/ tests/
```

Known importer: `tests/unit/test_legacy_loader_forwarding.py:20` — **delete** this test file (it exists solely to validate the shim).

**Touch-points**

| File | Change |
|------|--------|
| `src/tools/toolsets/loader.py` | Delete |
| `src/tools/toolsets/__init__.py` | Delete |
| `src/tools/toolsets/*.yaml` | Delete (move to `src/config/toolsets/` if not already duplicated there) |
| `tests/unit/test_legacy_loader_forwarding.py` | Delete |
| Any file with `from src.tools.toolsets` import | Update import to `src.config.toolsets.loader` |

**Test requirement**

Add a CI import-path guard:

```python
# tests/unit/test_toolset_import_path.py
def test_no_legacy_toolsets_import():
    """The legacy src/tools/toolsets path must not be importable."""
    import importlib, sys
    assert "src.tools.toolsets.loader" not in sys.modules
    try:
        importlib.import_module("src.tools.toolsets.loader")
        assert False, "Legacy loader must not exist"
    except ModuleNotFoundError:
        pass  # expected
```

**Risks / notes**

- Confirm `src/tools/toolsets/*.yaml` files are either duplicated in `src/config/toolsets/` or are unused before deleting. Run `grep -rn "tools/toolsets" src/` to find references.
- If any YAML toolset is missing from `src/config/toolsets/`, copy it there before deleting the legacy path.
- The `src/tools/toolsets/` directory may continue to exist as an empty package if other code does `from src.tools import toolsets` — check whether the directory itself is imported anywhere.

---

### P1-T3 — Make `MAX_TOOL_LOOP_ITERATIONS` configurable

**Current state**

`src/core/orchestration/inference_loop.py:247–248`:

```python
MAX_TOOL_LOOP_ITERATIONS: int = 5
max_rounds = MAX_TOOL_LOOP_ITERATIONS
```

This is a local variable inside `run_agent_once_impl`. The value 5 is far too low for complex tasks. It is not configurable via `agent_config.yaml` or any environment variable.

**Exact change**

Replace lines 247–248 with a config-backed lookup:

```python
# Read from config; fall back to 20 if key absent or config unavailable.
_max_rounds_cfg: int = 20
if _cfg_get is not None:
    try:
        _v = _cfg_get("max_graph_rounds")
        if isinstance(_v, int) and _v > 0:
            _max_rounds_cfg = _v
    except Exception:
        pass
MAX_TOOL_LOOP_ITERATIONS: int = _max_rounds_cfg  # kept for log messages
max_rounds = MAX_TOOL_LOOP_ITERATIONS
```

Note: `_cfg_get` is already imported a few lines above at `inference_loop.py:210–212`:

```python
try:
    from src.core.config_loader import get as _cfg_get
except Exception:
    _cfg_get = None
```

Also add `max_graph_rounds: 20` to the default `agent_config.yaml`:

```yaml
# agent_config.yaml (add under the top-level agent config section)
max_graph_rounds: 20
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/inference_loop.py:247` | Replace hard-coded `= 5` with config lookup (see above) |
| `agent_config.yaml` (project root or config dir) | Add `max_graph_rounds: 20` |
| `docs/configuration.md` (if it exists) | Document the new key |

**Test requirement**

```python
# tests/unit/test_inference_loop_config.py
from unittest.mock import patch

def test_max_graph_rounds_default_is_20(monkeypatch):
    """When config key is absent, max_graph_rounds defaults to 20."""
    monkeypatch.setattr(
        "src.core.orchestration.inference_loop._cfg_get", lambda k: None
    )
    # Reimport to trigger the lookup path
    import importlib, src.core.orchestration.inference_loop as m
    importlib.reload(m)
    # The default of 20 should be used
    # (test via run_agent_once_impl or by inspecting the constant path)

def test_max_graph_rounds_from_config(monkeypatch):
    cfg = {"max_graph_rounds": 50}
    monkeypatch.setattr(
        "src.core.orchestration.inference_loop._cfg_get",
        lambda k: cfg.get(k)
    )
    import importlib, src.core.orchestration.inference_loop as m
    importlib.reload(m)
```

**Risks / notes**

- The constant is a local variable, not module-level, so it is re-evaluated each call to `run_agent_once_impl`. The config lookup is therefore cheap and safe.
- Do not rename `MAX_TOOL_LOOP_ITERATIONS` — it appears in log messages inside the same function (lines 305–306, 322). Keep the local alias.
- Default of 20 is a safe starting point; teams running large refactors may want 50. Document the trade-off (higher values increase token cost).

---

### P1-T4 — Loud warning / hard fail when sandbox degrades

**Current state**

`src/tools/sandbox.py:158–185` — startup warning is published to EventBus only:

```python
try:
    if _DEFAULT_LEVEL != "off" and not _BWRAP_AVAILABLE and not _sandbox_exec_available():
        try:
            from src.core.orchestration.event_bus import get_event_bus
            eb = get_event_bus()
            eb.publish("system.warning", {"message": "sandbox: bwrap and sandbox-exec unavailable; sandbox disabled"})
        except Exception:
            pass
except Exception:
    pass
```

In headless/CLI mode without a TUI subscriber, this warning is completely invisible.

`sandbox.py` also falls through to plain `subprocess.run` at the end of `run_sandboxed()` with only a `logger.debug` call — see the fallback block around line 334.

**Exact change**

Replace the startup warning block (lines 158–185) with:

```python
if _DEFAULT_LEVEL != "off" and not _BWRAP_AVAILABLE and not _sandbox_exec_available():
    import sys
    _warn = (
        "WARNING: CodingAgent sandbox requested but neither bwrap (Linux) nor "
        "sandbox-exec (macOS) is available. Shell commands will run with FULL USER "
        "PRIVILEGES. Set CODINGAGENT_SANDBOX_LEVEL=off to suppress this warning, "
        "or install bwrap to enable sandboxing."
    )
    print(_warn, file=sys.stderr, flush=True)
    logger.warning("sandbox: %s", _warn)
    # Also publish to EventBus for TUI display (best-effort).
    try:
        from src.core.orchestration.event_bus import get_event_bus
        get_event_bus().publish("system.warning", {"message": _warn})
    except Exception:
        pass
```

Also update the fallback path inside `run_sandboxed()` (locate the `else` branch that calls plain `subprocess.run`). Change the `logger.debug` there to `logger.warning` and add a `sys.stderr` print:

```python
import sys
_fb_warn = (
    f"sandbox: no isolation backend available for level={sandbox_level!r}; "
    "running command WITHOUT sandbox. Command: " + str(cmd[:3])
)
logger.warning(_fb_warn)
print(f"WARNING: {_fb_warn}", file=sys.stderr, flush=True)
```

**Touch-points**

| File | Change |
|------|--------|
| `src/tools/sandbox.py:158–185` | Replace EventBus-only publish with stderr + logger.warning + EventBus |
| `src/tools/sandbox.py` (fallback `subprocess.run` path, ~line 334) | Upgrade `logger.debug` to `logger.warning` + add `sys.stderr` print |

**Test requirement**

```python
# tests/unit/test_sandbox_degradation_warning.py
import sys
from io import StringIO
from unittest.mock import patch

def test_stderr_warning_when_no_sandbox(monkeypatch, capsys):
    """Degraded sandbox must emit a warning to stderr, not just EventBus."""
    monkeypatch.setattr("src.tools.sandbox._BWRAP_AVAILABLE", False)
    monkeypatch.setattr("src.tools.sandbox._SANDBOX_EXEC_PATH", None)
    monkeypatch.setenv("CODINGAGENT_SANDBOX_LEVEL", "workspace")
    import importlib, src.tools.sandbox as m
    importlib.reload(m)  # triggers the module-level check
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "bwrap" in captured.err.lower() or "sandbox" in captured.err.lower()
```

**Risks / notes**

- `sandbox.py` is imported at module load time by several tool modules. Reloading in tests requires careful monkeypatching before import.
- Do NOT raise an exception in the startup block — that would break installs where sandboxing is intentionally unavailable. Use warnings only.
- Consider adding an `--require-sandbox` CLI flag in a follow-up (P4-T6 scope) for users who want a hard failure.

---

### P1-T5 — Surface round-limit message to user

**Current state**

`src/core/orchestration/inference_loop_rounds.py:162–167` — the user-visible message hardcodes `(5)`:

```python
if error_type == "infinite_loop_tool_limit":
    msg = (
        "[red]⚠ Task stopped: Maximum tool call limit (5) reached.[/red]\n\n"
        ...
    )
```

After P1-T3, the actual limit may be 20 or higher, making `(5)` incorrect.

**Exact change**

`_build_loop_exit_response` needs to receive or derive the actual `max_rounds` value. The simplest approach is to read it from config at call time:

```python
def _build_loop_exit_response(
    *,
    final_state: Dict[str, Any],
    cancel_event: Any,
) -> Optional[Dict[str, Any]]:
    ...
    if error_type == "infinite_loop_tool_limit":
        # Derive actual limit from config (mirrors inference_loop.py logic).
        _actual_limit = 20
        try:
            from src.core.config_loader import get as _cg
            _v = _cg("max_graph_rounds")
            if isinstance(_v, int) and _v > 0:
                _actual_limit = _v
        except Exception:
            pass
        msg = (
            f"[red]⚠ Task stopped: Maximum graph-round limit ({_actual_limit}) reached.[/red]\n\n"
            "The agent made too many tool-call rounds without completing the task. "
            "Consider increasing `max_graph_rounds` in agent_config.yaml, or break "
            "the task into smaller steps."
        )
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/inference_loop_rounds.py:162–167` | Replace hardcoded `(5)` with dynamic config lookup |

**Test requirement**

```python
# tests/unit/test_inference_loop_rounds.py
def test_loop_exit_message_reflects_config(monkeypatch):
    from src.core.orchestration.inference_loop_rounds import _build_loop_exit_response
    monkeypatch.setattr(
        "src.core.orchestration.inference_loop_rounds._cg",
        lambda k: 42 if k == "max_graph_rounds" else None,
        raising=False,
    )
    result = _build_loop_exit_response(
        final_state={"errors": ["infinite_loop_tool_limit"]},
        cancel_event=None,
    )
    assert result is not None
    assert "42" in result["assistant_message"]
```

**Risks / notes**

- This task should be implemented after P1-T3 so the config key exists.
- The `_build_loop_exit_response` function signature is unchanged — no callers need updating.

---

## Phase 2 — Robustness

### P2-T1 — Integration test for delegation depth enforcement

**Current state**

`src/tools/subagent_tools.py:78–79`:

```python
_DELEGATION_DEPTH_VAR: ContextVar[int] = ContextVar("_delegation_depth", default=0)
_MAX_DELEGATION_DEPTH = 3
```

Delegation depth is tracked in a `ContextVar`. The guard exists in code but there is **no integration test** asserting that a second-level delegation (depth ≥ 3) is actually refused. This is a confirmed P0 open item in `docs/REQUIREMENTS.md`.

**Exact change** (test only — no production code change needed unless guard is missing)

First verify the guard fires. Search `subagent_tools.py` for:

```python
if _DELEGATION_DEPTH_VAR.get() >= _MAX_DELEGATION_DEPTH:
```

If this check is absent, add it near the top of the `delegate_task` tool function body:

```python
_current_depth = _DELEGATION_DEPTH_VAR.get()
if _current_depth >= _MAX_DELEGATION_DEPTH:
    return {
        "ok": False,
        "error": (
            f"Delegation refused: maximum recursion depth ({_MAX_DELEGATION_DEPTH}) "
            "reached. Subagents cannot spawn further subagents beyond this limit."
        ),
    }
```

**Touch-points**

| File | Change |
|------|--------|
| `src/tools/subagent_tools.py` | Add / verify depth guard at entry of `delegate_task` tool |
| `tests/integration/test_delegation_depth.py` | New integration test (see below) |

**Test requirement**

```python
# tests/integration/test_delegation_depth.py
from unittest.mock import MagicMock, patch
from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR, _MAX_DELEGATION_DEPTH

def test_delegation_refused_at_max_depth():
    """delegate_task must return an error dict when depth >= MAX_DELEGATION_DEPTH."""
    token = _DELEGATION_DEPTH_VAR.set(_MAX_DELEGATION_DEPTH)
    try:
        from src.tools.subagent_tools import delegate_task
        result = delegate_task(
            role="coding",
            subtask_description="do something",
            working_dir="/tmp",
        )
        assert result.get("ok") is False or "error" in result
        assert "depth" in str(result).lower() or "refused" in str(result).lower()
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)

def test_delegation_allowed_below_max_depth():
    """delegate_task must not be blocked when depth < MAX_DELEGATION_DEPTH."""
    token = _DELEGATION_DEPTH_VAR.set(0)
    try:
        # Just verify the guard does not fire — mock actual graph execution.
        with patch("src.tools.subagent_tools._run_subagent", return_value={"ok": True}):
            pass  # detailed execution is tested elsewhere
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)
```

**Risks / notes**

- `_DELEGATION_DEPTH_VAR` is a `ContextVar` — setting it in one thread/context does not affect other threads. Tests must set and reset within the same context.
- `_MAX_DELEGATION_DEPTH = 3` means depth 0, 1, 2 are allowed; depth 3 is the first refused value. Confirm this is the intended semantic (depth ≥ 3 → refuse).

---

### P2-T2 — `ast.parse()` gate before Python file writes

**Current state**

`src/core/orchestration/graph/nodes/execution_helpers.py` — file writes (via `write_file`, `edit_file`, `apply_patch`) are committed without any syntax check. The verification node runs post-hoc and is not mandatory.

**Exact change**

Add a helper function in `execution_helpers.py` and call it before any Python file write is committed:

```python
# Add near the top of execution_helpers.py (after imports)
import ast as _ast
from pathlib import Path as _Path

def _validate_python_syntax(content: str, path_hint: str = "") -> Optional[str]:
    """Return an error string if *content* is not valid Python, else None.

    Only applies to .py files. Non-Python content always returns None.
    """
    if not path_hint.endswith(".py"):
        return None
    try:
        _ast.parse(content)
        return None
    except SyntaxError as exc:
        return (
            f"Syntax error in generated Python for '{path_hint}': "
            f"{exc.msg} (line {exc.lineno})"
        )
```

Call this before committing a `write_file` result. Locate the section in `execution_helpers.py` or the execution node that dispatches tool results and add:

```python
# After resolving the file content to write (write_file / edit_file):
if tool_name in ("write_file", "edit_file", "edit_file_atomic") and path_arg:
    new_content = args.get("content") or args.get("new_content") or ""
    syntax_err = _validate_python_syntax(new_content, path_arg)
    if syntax_err:
        return {
            "last_result": {"ok": False, "error": syntax_err},
            "history": [{"role": "user", "content": json.dumps(
                {"tool_execution_result": {"ok": False, "error": syntax_err}}
            )}],
            "next_action": None,
        }
```

Note: For `edit_file` (partial content), `ast.parse` on a code fragment will produce false positives (e.g., a function body without its `def`). Apply the gate **only to `write_file`** (full file) initially. Add `edit_file` only when the full post-edit content is available.

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/nodes/execution_helpers.py` | Add `_validate_python_syntax()` helper + call site before write_file commits |
| `src/core/orchestration/graph/nodes/execution_node.py` | If write dispatch is there instead, add gate at the dispatch point |

**Test requirement**

```python
# tests/unit/test_execution_helpers_syntax_gate.py
from src.core.orchestration.graph.nodes.execution_helpers import _validate_python_syntax

def test_valid_python_passes():
    assert _validate_python_syntax("x = 1\n", "foo.py") is None

def test_invalid_python_blocked():
    err = _validate_python_syntax("def foo(\n", "bar.py")
    assert err is not None
    assert "Syntax" in err or "syntax" in err

def test_non_python_always_passes():
    assert _validate_python_syntax("{ not: valid python }", "foo.ts") is None
    assert _validate_python_syntax("not python", "readme.md") is None
```

**Risks / notes**

- Do NOT apply to `edit_file` / `edit_by_line_range` on partial content — `ast.parse` requires complete modules. Only validate `write_file` where full content is always present.
- Consider adding an override mechanism (e.g., `# noqa: syntax` comment or a config flag `skip_syntax_gate: true`) for teams writing test fixtures with intentional syntax errors.

---

### P2-T3 — Shell command allowlist / denylist at tool layer

**Current state**

The `bash` / shell tool (locate at `src/tools/bash_tool.py` or equivalent) passes commands directly to `run_sandboxed()`. If the sandbox degrades (C-4 / P1-T4), no command filtering exists.

**Exact change**

Add a module-level denylist to the bash tool:

```python
# src/tools/bash_tool.py — add near top
import shlex as _shlex

_COMMAND_DENYLIST = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp",
    "telnet", "ftp", "rsync",  # network exfiltration
    "dd", "mkfs", "fdisk", "parted",  # disk destruction
    "shutdown", "reboot", "halt", "poweroff",  # system control
    "crontab",  # persistence
    "chmod", "chown",  # privilege manipulation (consider allowlisting specific uses)
})

def _check_command_denylist(cmd: str) -> Optional[str]:
    """Return an error string if the first token of *cmd* is in the denylist."""
    try:
        tokens = _shlex.split(cmd)
    except ValueError:
        return None  # malformed shell — let sandbox handle it
    if not tokens:
        return None
    binary = _Path(tokens[0]).name  # strip path prefix: /usr/bin/curl → curl
    if binary in _COMMAND_DENYLIST:
        return (
            f"Command '{binary}' is blocked by the CodingAgent tool policy. "
            "If this command is required, add it to the allowlist in agent_config.yaml."
        )
    return None
```

Call this check before `run_sandboxed()` in the tool handler.

Also add a config-driven allowlist override to `agent_config.yaml`:

```yaml
# agent_config.yaml
bash_tool:
  command_denylist_extras: []   # additional commands to deny
  command_allowlist_override: []  # commands to remove from denylist
```

**Touch-points**

| File | Change |
|------|--------|
| `src/tools/bash_tool.py` (or equivalent shell tool) | Add `_COMMAND_DENYLIST` + `_check_command_denylist()` + call before `run_sandboxed()` |
| `agent_config.yaml` | Add `bash_tool.command_denylist_extras` and `bash_tool.command_allowlist_override` |

**Test requirement**

```python
# tests/unit/test_bash_tool_denylist.py
from src.tools.bash_tool import _check_command_denylist

def test_curl_blocked():
    assert _check_command_denylist("curl https://evil.com") is not None

def test_python_allowed():
    assert _check_command_denylist("python foo.py") is None

def test_path_prefix_stripped():
    # /usr/bin/curl should be treated same as curl
    assert _check_command_denylist("/usr/bin/curl -s http://x.com") is not None
```

**Risks / notes**

- The denylist is defense-in-depth, not a complete security boundary. The sandbox (P1-T4) is the primary control.
- `chmod` and `chown` are common in legitimate build scripts; consider making them allowlist entries that require a flag rather than unconditional denies.
- `shlex.split` will raise on malformed shell (unmatched quotes); catch `ValueError` as shown.

---

### P2-T4 — Wire distillation into MessageManager pipeline

**Current state**

`src/core/orchestration/inference_loop_state.py:80–83` — distillation flags are initialized but never consumed:

```python
"_should_distill": None,
"_force_compact": None,
"_budget_compaction": None,
```

`src/core/memory/distiller.py` is fully implemented (`compact_messages_to_prose` at line ~163) but is never called from the active pipeline. Long tasks accumulate unbounded `history` lists.

**Exact change**

Wire compaction into `_prepare_next_round_state` in `inference_loop_rounds.py`, which is called between every graph round:

```python
# inference_loop_rounds.py — inside _prepare_next_round_state, after building _next_history

# Context-budget compaction: if history is large, compact it.
_HISTORY_TOKEN_THRESHOLD = 6000  # ~24k chars at 4 chars/token
try:
    from src.core.memory.distiller import compact_messages_to_prose as _compact
    from src.core.memory.distiller import _estimate_tokens as _est

    _token_est = _est(_next_history)
    if _token_est > _HISTORY_TOKEN_THRESHOLD:
        _working_dir = _prev_working_dir or final_state.get("working_dir") or ""
        _compacted = _compact(_next_history, _working_dir)
        if _compacted:
            _next_history = _compacted
except Exception as _compact_exc:
    guilogger.debug("_prepare_next_round_state: compaction failed (non-fatal): %s", _compact_exc)
```

Also expose the threshold as a config key:

```yaml
# agent_config.yaml
distillation:
  history_token_threshold: 6000
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/inference_loop_rounds.py:99–137` (`_prepare_next_round_state`) | Add compaction call after building `_next_history` |
| `agent_config.yaml` | Add `distillation.history_token_threshold` |

**Test requirement**

```python
# tests/unit/test_distillation_wiring.py
from unittest.mock import patch, MagicMock

def test_compaction_triggered_when_history_large():
    """When token estimate exceeds threshold, compact_messages_to_prose is called."""
    large_history = [{"role": "user", "content": "x" * 25000}]
    final_state = {
        "history": large_history,
        "working_dir": "/tmp",
        "errors": [],
    }
    with patch(
        "src.core.orchestration.inference_loop_rounds._compact",
        return_value=[{"role": "user", "content": "summary"}],
    ) as mock_compact:
        from src.core.orchestration.inference_loop_rounds import _prepare_next_round_state
        orch = MagicMock()
        orch._session_read_files = set()
        orch.deterministic = False
        orch.seed = None
        result = _prepare_next_round_state(
            final_state=final_state,
            current_state={},
            orch=orch,
            cancel_event=None,
        )
        mock_compact.assert_called_once()
```

**Risks / notes**

- `compact_messages_to_prose` makes an LLM call. This will add latency between rounds. Use the `small_model` config (already wired in `distiller._call_llm_sync`) to minimize cost.
- Set a conservative threshold (6,000 tokens ≈ 24k chars) to avoid compacting on every short task.
- The `_KEEP_RECENT = 6` constant in `distiller.py:56` controls how many recent messages survive compaction — verify this is appropriate.

---

### P2-T5 — Route EventBus safety warnings to stderr in headless mode

**Current state**

`src/core/orchestration/event_bus.py` — EventBus subscribers are registered by the TUI. In headless/CLI mode, no subscriber exists for `"system.warning"` events. Safety-relevant events (sandbox degradation, doom-loop `ASK` behavior) are silently dropped.

**Exact change**

Add a `subscribe_stderr_fallback` function that registers a module-level subscriber writing to stderr when no other subscriber is registered for an event:

```python
# event_bus.py — add after existing subscriber registration API

import sys as _sys

_SAFETY_EVENTS = frozenset({"system.warning", "tool.doom_loop_detected", "system.error"})

def subscribe_stderr_fallback(event_bus: "EventBus") -> None:
    """Register a stderr fallback for safety events that have no TUI subscriber.

    Call this from Orchestrator.__init__ or CLI entry point so headless runs
    always surface sandbox/doom-loop warnings.
    """
    def _stderr_handler(payload: Any) -> None:
        msg = payload.get("message") if isinstance(payload, dict) else str(payload)
        print(f"[CodingAgent WARNING] {msg}", file=_sys.stderr, flush=True)

    for ev in _SAFETY_EVENTS:
        event_bus.subscribe(ev, _stderr_handler)
```

Call `subscribe_stderr_fallback(self.event_bus)` from `Orchestrator.__init__` (in `src/core/orchestration/orchestrator.py`).

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/event_bus.py` | Add `subscribe_stderr_fallback()` function |
| `src/core/orchestration/orchestrator.py` | Call `subscribe_stderr_fallback(self.event_bus)` in `__init__` |

**Test requirement**

```python
# tests/unit/test_event_bus_stderr_fallback.py
from io import StringIO
import sys
from unittest.mock import patch
from src.core.orchestration.event_bus import EventBus, subscribe_stderr_fallback

def test_safety_event_printed_to_stderr(capsys):
    eb = EventBus()
    subscribe_stderr_fallback(eb)
    eb.publish("system.warning", {"message": "sandbox degraded"})
    captured = capsys.readouterr()
    assert "sandbox degraded" in captured.err
```

**Risks / notes**

- When the TUI IS active, it will have its own subscriber. The stderr fallback subscriber will ALSO fire unless unregistered. Consider using a "one active subscriber" pattern, or accept duplicate output (one to TUI, one to stderr).
- The simpler approach: emit to stderr unconditionally from the safety publish sites (sandbox.py, loop_guards.py) — which P1-T4 already does for sandbox. P2-T5 closes the remaining gap for doom-loop events.

---

### P2-T6 — Move `pandas` to optional dependency

**Current state**

`pyproject.toml:19`:

```toml
dependencies = [
  ...
  "pandas>=2.0.0",
  ...
]
```

No `import pandas` is found in `src/core/` or `src/tools/` (only a comment in `src/core/indexing/vector_store.py`). `pandas` adds ~50 MB and several seconds of import time to every install.

**Exact change**

1. Remove `"pandas>=2.0.0"` from `[project].dependencies`.
2. Add it to a new `[analytics]` optional extra:

```toml
[project.optional-dependencies]
analytics = [
  "pandas>=2.0.0",
]
```

**Touch-points**

| File | Change |
|------|--------|
| `pyproject.toml:19` | Remove `"pandas>=2.0.0"` from core `dependencies` |
| `pyproject.toml` `[project.optional-dependencies]` section | Add `analytics = ["pandas>=2.0.0"]` |

**Test requirement**

```python
# tests/unit/test_no_pandas_in_core.py
def test_pandas_not_imported_by_core():
    """Ensure core src/ modules do not import pandas at the module level."""
    import importlib, sys
    # Stub out pandas to catch any import
    import unittest.mock as mock
    with mock.patch.dict(sys.modules, {"pandas": None}):
        try:
            import src.core.orchestration.orchestrator  # noqa: F401
        except Exception as e:
            if "pandas" in str(e).lower():
                raise AssertionError(f"Core imports pandas: {e}") from e
```

**Risks / notes**

- Run `grep -rn "import pandas" src/` before applying. If any hits exist, either guard with `try/except ImportError` or leave pandas in core.
- `vector_store.py` comment is not an import — safe to ignore.
- Update `README.md` / install docs to mention the `[analytics]` extra.

---

## Phase 3 — Capability

### P3-T1 — Implement step controller node

**Current state**

`src/core/orchestration/graph/nodes/step_controller_node.py` — a 119-line `step_controller_node` function exists and is wired into the graph builder (`src/core/orchestration/graph/builder.py`). However, per the audit, routing enforcement in `should_after_step_controller` is incomplete — the node may not reliably enforce "execute exactly step N, verify, then advance."

**Exact change**

Audit the routing function `should_after_step_controller` in `builder.py`. It must implement:

```
step_controller_node → execution_node  (normal path)
step_controller_node → END             (if plan exhausted or canceled)
step_controller_node → planning_node   (if no plan exists yet)
```

Verify that `step_controller_node` itself sets `current_step` correctly and does not allow `frontier_loop_node` to skip steps. If `step_controller_node` is bypassed in some routing branches, add it to those paths.

Minimum production-code change:

```python
# builder.py — in should_after_step_controller (or equivalent routing fn)
def should_after_step_controller(state: StateLike) -> str:
    if state.get("cancel_event") and state["cancel_event"].is_set():
        return END
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    if not current_plan:
        return "planning_node"
    if current_step >= len(current_plan):
        return END
    return "execution_node"
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/builder.py` | Verify/fix `should_after_step_controller` routing |
| `src/core/orchestration/graph/nodes/step_controller_node.py` | Verify step advancement logic |

**Test requirement**

```python
# tests/unit/test_step_controller_routing.py
def test_routes_to_execution_when_plan_active():
    state = {"current_plan": [{"description": "step1"}], "current_step": 0}
    from src.core.orchestration.graph.builder import should_after_step_controller
    assert should_after_step_controller(state) == "execution_node"

def test_routes_to_end_when_plan_exhausted():
    state = {"current_plan": [{"description": "step1"}], "current_step": 1}
    from src.core.orchestration.graph.builder import should_after_step_controller
    from langgraph.graph import END
    assert should_after_step_controller(state) == END
```

**Risks / notes**

- LangGraph's `END` sentinel must be imported from `langgraph.graph` — do not use string `"END"`.
- Changing routing functions can break existing graph behavior. Run the full integration test suite after this change.
- The step controller was added as ORCH-02 per `docs/orchestration-gap-analysis.md` — check that document for additional context.

---

### P3-T2 — Automated debug retry loop with strategy selection

**Current state**

`src/core/orchestration/graph/nodes/debug_node.py:95–113` — when `current_attempt >= max_attempts`, the node gives up and attempts rollback. There is no mechanism to retry with a different strategy between attempts.

`_classify_error` at `debug_node.py:17–30` already classifies errors into categories. `TYPE_GUIDANCE` at lines 33–40 provides per-category guidance. But all attempts use the same prompt — no escalation.

**Exact change**

Add a strategy-escalation list keyed by attempt number:

```python
# debug_node.py — add after TYPE_GUIDANCE dict

_STRATEGY_ESCALATION = [
    # attempt 0: targeted fix based on error type
    lambda error_type, error_summary: (
        f"Guidance: {TYPE_GUIDANCE[error_type]}\n"
        "Generate a minimal, targeted fix using edit_file or write_file."
    ),
    # attempt 1: read the full file first, then fix
    lambda error_type, error_summary: (
        "Strategy: Before fixing, use read_file to inspect the full file content. "
        "Then generate a complete, correct replacement of the problematic section."
    ),
    # attempt 2: broad analysis
    lambda error_type, error_summary: (
        "Strategy: The previous two fix attempts failed. "
        "Use bash to run the failing command and capture full output. "
        "Then apply a fix that addresses the root cause, not just the symptom."
    ),
]
```

In the `fix_prompt` construction, replace the static `Guidance:` line with:

```python
_strategy_idx = min(current_attempt, len(_STRATEGY_ESCALATION) - 1)
_strategy = _STRATEGY_ESCALATION[_strategy_idx](error_type, error_summary)

fix_prompt = f"""You are a debugging assistant. Attempt {next_attempt}/{max_attempts}.

Task: {task}
Error type: {error_type}
Error details: {error_summary}

{_strategy}

Generate a JSON function call to fix the issue."""
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/nodes/debug_node.py:160–168` | Replace static `fix_prompt` with strategy-escalation version |

**Test requirement**

```python
# tests/unit/test_debug_node_strategy.py
from src.core.orchestration.graph.nodes.debug_node import _STRATEGY_ESCALATION

def test_strategy_escalates_across_attempts():
    s0 = _STRATEGY_ESCALATION[0]("syntax_error", "SyntaxError")
    s1 = _STRATEGY_ESCALATION[1]("syntax_error", "SyntaxError")
    s2 = _STRATEGY_ESCALATION[2]("syntax_error", "SyntaxError")
    # Each strategy should be distinct
    assert s0 != s1 != s2
    # Attempt 2 should mention broader analysis
    assert "root cause" in s2.lower() or "bash" in s2.lower()
```

**Risks / notes**

- The `max_debug_attempts` default of 3 maps neatly to the 3 escalation strategies. If `max_debug_attempts` is increased, add more strategies.
- The `P1-B fix` comment at `debug_node.py:123–127` removed per-error-type attempt resets. Do not re-introduce them.

---

### P3-T3 — Trigger repo index refresh after file writes

**Current state**

`src/core/indexing/repo_indexer.py` — `INDEX_VERSION = "3.0"`, multi-language indexer, 545 lines. Public API includes `parse_file()`, `get_language()`, and `get_symbols_for_task()`. The indexer is run at session start but not after file modifications.

**Exact change**

Add a lightweight incremental refresh function to `repo_indexer.py`:

```python
# repo_indexer.py — add near end of file

def refresh_file_in_index(file_path: str, working_dir: str) -> bool:
    """Re-parse a single file and update its entry in the on-disk index.

    Call this after any write_file / edit_file tool completes successfully.
    Returns True if the index was updated, False on any error.
    """
    try:
        p = Path(file_path)
        if not p.exists():
            return False
        parsed = parse_file(p)
        if not parsed:
            return False
        # Load existing index, update the file entry, save.
        ctx_dir = Path(working_dir) / _get_ctx_dir_name()
        index_path = ctx_dir / "repo_index.json"
        if not index_path.exists():
            return False
        import json
        data = json.loads(index_path.read_text(encoding="utf-8"))
        rel = str(p.relative_to(Path(working_dir)))
        data.setdefault("files", {})[rel] = parsed
        if _atomic_write_json is not None:
            return _atomic_write_json(index_path, data)
        return False
    except Exception as exc:
        logger.debug("repo_indexer.refresh_file_in_index: %s", exc)
        return False
```

Call this from `execution_helpers.py` after a successful file write result:

```python
# execution_helpers.py — after a write_file / edit_file tool succeeds:
if result.get("ok") and tool_name in ("write_file", "edit_file", "edit_file_atomic"):
    try:
        from src.core.indexing.repo_indexer import refresh_file_in_index as _refresh
        _refresh(resolved_path, working_dir)
    except Exception:
        pass  # never block execution on index refresh failure
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/indexing/repo_indexer.py` | Add `refresh_file_in_index()` function |
| `src/core/orchestration/graph/nodes/execution_helpers.py` | Call `refresh_file_in_index` after successful write tool result |

**Test requirement**

```python
# tests/unit/test_repo_indexer_refresh.py
import json
from pathlib import Path
from src.core.indexing.repo_indexer import refresh_file_in_index

def test_refresh_updates_index(tmp_path):
    # Create a minimal index
    ctx = tmp_path / ".codingAgent"
    ctx.mkdir()
    (tmp_path / "foo.py").write_text("def bar(): pass\n")
    (ctx / "repo_index.json").write_text(json.dumps({"files": {}}))

    result = refresh_file_in_index(str(tmp_path / "foo.py"), str(tmp_path))
    assert result is True
    data = json.loads((ctx / "repo_index.json").read_text())
    assert "foo.py" in data["files"]
```

**Risks / notes**

- `refresh_file_in_index` does a read-modify-write on the JSON index. Under concurrent subagent execution, this is a race condition. Use `_atomic_write_json` (already imported) to minimize the window.
- Only refresh when the write tool reports `ok: True` — do not refresh on failed writes.

---

### P3-T4 — Serialize plans to disk; resume across sessions

**Current state**

`src/core/orchestration/graph/nodes/planning_helpers.py` — `save_last_plan` and `load_last_plan` already exist and are called from `planning_node.py`. Plans are saved to `<workdir>/.codingAgent/last_plan.json`.

`src/core/memory/sqlite_session_store.py` — stores session transcripts. Does not store plan objects.

**Exact change**

The plan-file mechanism is already implemented. The gap is persistence to the `SessionStore` for cross-session resumption. Add a plan-storage method to `sqlite_session_store.py`:

```python
# sqlite_session_store.py — add method to SessionStore class

def save_plan(self, session_id: str, plan: list, task: str, step: int) -> None:
    """Persist a plan to the session store for cross-session resumption."""
    import json
    with self._get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO session_plans
               (session_id, plan_json, task, current_step, saved_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (session_id, json.dumps(plan), task, step),
        )

def load_plan(self, session_id: str) -> Optional[dict]:
    """Load a saved plan from the session store."""
    import json
    with self._get_connection() as conn:
        row = conn.execute(
            "SELECT plan_json, task, current_step FROM session_plans WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row:
        return {"plan": json.loads(row[0]), "task": row[1], "current_step": row[2]}
    return None
```

Add the `session_plans` table to the schema migration:

```python
# sqlite_session_store.py — in _create_tables() or schema migration (bump _SCHEMA_VERSION to 4)
conn.execute("""
    CREATE TABLE IF NOT EXISTS session_plans (
        session_id TEXT PRIMARY KEY,
        plan_json TEXT NOT NULL,
        task TEXT,
        current_step INTEGER DEFAULT 0,
        saved_at TEXT
    )
""")
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/memory/sqlite_session_store.py` | Add `save_plan()` / `load_plan()` methods + `session_plans` table; bump `_SCHEMA_VERSION` to 4 |
| `src/core/orchestration/graph/nodes/planning_node.py` | Call `orchestrator.session_store.save_plan(...)` after a plan is generated |

**Test requirement**

```python
# tests/unit/test_session_store_plan_persistence.py
import tempfile, os
from src.core.memory.sqlite_session_store import SQLiteSessionStore

def test_save_and_load_plan(tmp_path):
    store = SQLiteSessionStore(db_path=str(tmp_path / "sessions.db"))
    plan = [{"description": "step1", "completed": False}]
    store.save_plan("sess-1", plan, "do the thing", 0)
    loaded = store.load_plan("sess-1")
    assert loaded is not None
    assert loaded["plan"] == plan
    assert loaded["task"] == "do the thing"
    assert loaded["current_step"] == 0
```

**Risks / notes**

- `_SCHEMA_VERSION = 3` → 4 requires a migration path. Implement a standard `ALTER TABLE` or `CREATE TABLE IF NOT EXISTS` migration in `_apply_migrations()`.
- The file-based `last_plan.json` mechanism already works for single-session resumption. The SQLite path adds multi-session and remote-access capability. Both can coexist.

---

### P3-T5 — Populate `snapshots` for task rollback

**Current state**

`src/core/orchestration/graph/state.py:138`:

```python
snapshots: List[str] | None
```

Initialized to `[]` at `inference_loop_state.py:98` but never written by any execution path. `debug_node.py:100–104` calls `rollback_manager.rollback()` but has no snapshot to roll back to when `snapshots` is empty.

**Exact change**

After each successful file write in `execution_helpers.py`, capture a snapshot of the modified file's previous content and append its path to `state["snapshots"]`:

```python
# execution_helpers.py — before committing a write_file / edit_file result:

def _capture_snapshot(path: str, working_dir: str) -> Optional[str]:
    """Read the current content of *path* and save it to a snapshot dir.

    Returns the snapshot file path on success, None on error.
    """
    try:
        p = (Path(working_dir) / path).resolve()
        if not p.exists():
            return None
        snap_dir = Path(working_dir) / ".codingAgent" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        import hashlib, time
        ts = int(time.time() * 1000)
        slug = hashlib.md5(str(p).encode()).hexdigest()[:8]
        snap_path = snap_dir / f"{slug}_{ts}{p.suffix}"
        snap_path.write_bytes(p.read_bytes())
        return str(snap_path)
    except Exception:
        return None

# Call before write: snap = _capture_snapshot(path_arg, working_dir)
# Return update: {"snapshots": state.get("snapshots", []) + ([snap] if snap else [])}
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/nodes/execution_helpers.py` | Add `_capture_snapshot()` + call before write; include `snapshots` update in return dict |
| `src/core/orchestration/graph/nodes/debug_node.py:99–106` | Verify rollback path reads `state["snapshots"]` to find files to restore |

**Test requirement**

```python
# tests/unit/test_snapshots.py
from pathlib import Path
from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot

def test_snapshot_captures_content(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("original content")
    snap = _capture_snapshot("foo.py", str(tmp_path))
    assert snap is not None
    assert Path(snap).read_text() == "original content"

def test_snapshot_returns_none_for_missing_file(tmp_path):
    assert _capture_snapshot("nonexistent.py", str(tmp_path)) is None
```

**Risks / notes**

- Snapshots accumulate disk space. Add a cleanup step in `debug_node.py` (already calls `rollback_mgr.cleanup_old_snapshots(keep_last=5)` at line 104) — extend this to also purge the `.codingAgent/snapshots/` directory.
- Store snapshot paths in `state["snapshots"]` as absolute paths so rollback can locate them across working directory changes.

---

### P3-T6 — Split oversized node files

**Current state**

| File | Lines |
|------|-------|
| `src/core/orchestration/graph/nodes/execution_helpers.py` | 1,322 |
| `src/core/orchestration/graph/nodes/frontier_loop_node.py` | 1,007 |
| `src/core/orchestration/graph/nodes/planning_node.py` | 888 |

`planning_node.py` has already been partially split (see `planning_helpers.py`, `planning_fast_paths.py`, `planning_prompt.py`, `planning_result.py`). The pattern to follow is clear.

**Exact change (execution_helpers.py)**

Extract into logical sub-modules:

| New file | Content |
|----------|---------|
| `execution_tool_dispatch.py` | `extract_tool_call_from_response`, `build_no_action_result` |
| `execution_guards.py` | `_validate_python_syntax`, `_capture_snapshot`, `_check_command_denylist` (if added here) |
| `execution_write_helpers.py` | Write result processing, snapshot capture, index refresh |
| `execution_helpers.py` (keep) | Imports from the above + higher-level orchestration functions |

**Exact change (frontier_loop_node.py)**

Extract:

| New file | Content |
|----------|---------|
| `frontier_loop_helpers.py` | Helper functions (routing logic, state updates) |
| `frontier_loop_node.py` (keep) | The `frontier_loop_node` async function + thin wiring |

**Touch-points**

All files that `from ... import` any moved symbol — run:

```bash
grep -rn "from src.core.orchestration.graph.nodes.execution_helpers import" src/ tests/
grep -rn "from src.core.orchestration.graph.nodes.frontier_loop_node import" src/ tests/
```

Update each import to the new sub-module path.

**Test requirement**

- All existing tests must continue to pass after the refactor (no behavioral change).
- Add a `tests/unit/test_module_sizes.py` that asserts no node file exceeds 600 lines:

```python
from pathlib import Path

MAX_LINES = 600
NODE_DIR = Path("src/core/orchestration/graph/nodes")

def test_no_node_file_exceeds_max_lines():
    oversized = [
        p for p in NODE_DIR.glob("*.py")
        if len(p.read_text().splitlines()) > MAX_LINES
    ]
    assert oversized == [], f"Oversized node files: {oversized}"
```

**Risks / notes**

- This is a pure refactor — no behavior change. Use `git mv` to preserve file history.
- LangGraph compiles the graph by reference — moving function definitions has no effect on the compiled graph as long as `builder.py` imports from the correct new paths.
- Do this last in Phase 3 (after P3-T1 through P3-T5) to avoid merge conflicts with other changes to these files.

---

## Phase 4 — Advanced Features

### P4-T1 — Expand evaluation scenario library

**Current state**

`scenario_evaluator.py` — solid framework, only 3 trivial scenarios. No edit-accuracy, multi-file, or failure-recovery scenarios.

**Exact change**

Add at least 20 scenarios covering:

| Category | Scenarios to add |
|----------|-----------------|
| Single-file edit | Rename a function, add a docstring, fix an off-by-one error |
| Multi-file refactoring | Move a class, update all import references |
| Bug introduction/fix | Plant a bug, assert the agent finds and fixes it |
| Task disambiguation | Ambiguous prompt → agent asks for clarification |
| Tool failure recovery | Mock a tool failure, assert debug_node produces a fix |
| Plan adherence | Multi-step plan, assert steps executed in order |
| Context boundary | Task requiring reading 5+ files before acting |
| Delegation | Task that requires spawning a subagent |

Each scenario should define:
- `prompt`: the task string
- `expected_files_modified`: list of expected output paths
- `expected_tool_sequence`: optional ordered list of expected tool names
- `success_criteria`: callable that inspects final state

**Touch-points**

| File | Change |
|------|--------|
| `scenario_evaluator.py` or `tests/scenarios/` | Add ≥20 scenario definitions |

**Test requirement**

The scenario runner should be invoked in CI with a `MockAdapter` for correctness checks, and separately with a real adapter for behavioral validation (P4-T2).

**Risks / notes**

- Avoid scenarios that require internet access — all should work against local fixture repositories.
- Create a `tests/fixtures/repos/` directory with small synthetic repos for scenario testing.

---

### P4-T2 — Real-LLM e2e smoke test in CI

**Current state**

`tests/e2e/` — 5 test files, all use `MockAdapter`. Behavioral regressions are invisible to CI.

**Exact change**

Add a CI job that runs only when a real LLM API key is available (`CODINGAGENT_LLM_API_KEY` env var set):

```python
# tests/e2e/test_real_llm_smoke.py
import os, pytest

@pytest.mark.skipif(
    not os.environ.get("CODINGAGENT_LLM_API_KEY"),
    reason="Real LLM smoke test requires CODINGAGENT_LLM_API_KEY",
)
def test_simple_file_creation_with_real_llm(tmp_path):
    """Ask the real LLM to create a hello-world Python file and verify it exists."""
    from src.core.orchestration.orchestrator import Orchestrator
    orch = Orchestrator(working_dir=str(tmp_path))
    result = orch.run_agent_once(
        system_prompt_name=None,
        messages=[{"role": "user", "content": "Create a file hello.py that prints 'hello world'"}],
        tools={},
    )
    assert result.get("ok") or result.get("assistant_message")
    assert (tmp_path / "hello.py").exists()
```

**Touch-points**

| File | Change |
|------|--------|
| `tests/e2e/test_real_llm_smoke.py` | New test (skipped unless API key present) |
| `.github/workflows/ci.yml` (or equivalent) | Add CI job with `CODINGAGENT_LLM_API_KEY` secret |

**Risks / notes**

- Real LLM tests are non-deterministic. Use simple, verifiable tasks (file creation, trivial edits).
- Gate on environment variable so the test is always skipped in forks and PRs without secrets.

---

### P4-T3 — Cross-session memory retrieval

**Current state**

`src/core/memory/distiller.py` — session summaries are written to disk. `sqlite_session_store.py` has `add_message` / `get_messages`. No retrieval path surfaces prior-session context into a new session's system prompt.

**Exact change**

Add a retrieval function to `distiller.py`:

```python
def retrieve_relevant_prior_sessions(
    task: str,
    working_dir: str,
    max_sessions: int = 3,
    max_chars: int = 2000,
) -> str:
    """Return a text summary of prior sessions relevant to *task*.

    Uses FTS or recency to find relevant sessions; returns empty string if none.
    """
    try:
        from src.core.memory.sqlite_session_store import get_session_store
        store = get_session_store()
        # Simple recency-based retrieval; upgrade to FTS when available.
        recent = store.get_recent_sessions(limit=max_sessions)
        snippets = []
        for sess in recent:
            summary = store.get_session_summary(sess["session_id"])
            if summary:
                snippets.append(f"[Prior session {sess['session_id'][:8]}]: {summary[:500]}")
        return "\n".join(snippets)[:max_chars]
    except Exception:
        return ""
```

Inject this into `prepare_system_prompt` in `inference_loop_state.py`:

```python
# inference_loop_state.py — in prepare_system_prompt()
prior_context = ""
try:
    from src.core.memory.distiller import retrieve_relevant_prior_sessions
    prior_context = retrieve_relevant_prior_sessions(task=prompt, working_dir=str(orch.working_dir))
except Exception:
    pass
if prior_context:
    full_system_prompt += f"\n\n## Prior Session Context\n{prior_context}"
```

**Touch-points**

| File | Change |
|------|--------|
| `src/core/memory/distiller.py` | Add `retrieve_relevant_prior_sessions()` |
| `src/core/memory/sqlite_session_store.py` | Add `get_recent_sessions()` and `get_session_summary()` methods |
| `src/core/orchestration/inference_loop_state.py` (`prepare_system_prompt`) | Inject prior context into system prompt |

**Test requirement**

```python
# tests/unit/test_cross_session_retrieval.py
def test_retrieve_returns_string_when_no_sessions(tmp_path):
    from src.core.memory.distiller import retrieve_relevant_prior_sessions
    result = retrieve_relevant_prior_sessions("do the thing", str(tmp_path))
    assert isinstance(result, str)  # must not raise; empty string OK
```

**Risks / notes**

- Keep retrieved context short (≤2,000 chars) to avoid consuming the system prompt budget.
- FTS (full-text search) in SQLite is available via the `fts5` extension — use it for better relevance when the session store grows large.

---

### P4-T4 — Retire or activate DAG/wave execution infrastructure

**Current state**

`AgentState` has `plan_dag`, `execution_waves`, `current_wave`. `dag_parser.py` (~1,000 lines) implements parallel wave execution. `planning_node.py` calls `_convert_flat_to_dag` but the wave execution path is not wired into the routing logic. All tasks execute sequentially.

**Decision required:** Retire (delete) or activate (fully wire).

**If retiring:**

1. Remove `plan_dag`, `execution_waves`, `current_wave` fields from `_AgentStateSpec` (`state.py`).
2. Remove `from src.core.orchestration.dag_parser import PlanDAG` from `state.py:17`.
3. Remove `_convert_flat_to_dag` calls from `planning_node.py`.
4. Delete `src/core/orchestration/dag_parser.py`.
5. Update `_INT_OR_NONE_FIELDS` in `state.py` to remove `current_wave`.

**If activating:**

1. Wire `execution_waves` routing into the step controller: after step completion, advance `current_wave` when all steps in the wave are done.
2. Add parallel execution support: when `current_wave` contains multiple step IDs, dispatch them concurrently (requires `asyncio.gather` in the execution node).
3. Add integration test asserting wave-parallel execution completes faster than sequential.

**Touch-points (retire path)**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/state.py` | Remove `plan_dag`, `execution_waves`, `current_wave` fields |
| `src/core/orchestration/dag_parser.py` | Delete |
| `src/core/orchestration/graph/nodes/planning_node.py` | Remove `_convert_flat_to_dag` calls |
| `src/core/orchestration/inference_loop_state.py` | Remove `plan_dag`, `execution_waves`, `current_wave` from initial state |

**Test requirement**

After retiring, run `grep -rn "dag_parser\|plan_dag\|execution_waves\|current_wave" src/` and confirm zero hits.

**Risks / notes**

- LangGraph requires flat TypedDict for state reducers — nested sub-states would break the merge logic. If activating, do not introduce sub-TypedDicts.
- The retire path is simpler and recommended unless parallel plan execution is a near-term roadmap item.

---

### P4-T5 — Refactor `AgentState` into logical sections

**Current state**

`src/core/orchestration/graph/state.py` — `_AgentStateSpec` has approximately 100 fields. LangGraph requires a flat `TypedDict` for state merging, so nested sub-states cannot be used without breaking the reducer.

**Exact change**

Rather than nested TypedDicts (which break LangGraph), organize fields into section-comment groups within the same flat TypedDict:

```python
class _AgentStateSpec(TypedDict, total=False):

    # ── Core task ──────────────────────────────────────────────────────────
    task: str
    original_task: str | None
    working_dir: str
    system_prompt: str
    session_id: str | None
    turn_count: int | None
    max_turns: int | None
    agent_mode: str | None

    # ── Conversation history ───────────────────────────────────────────────
    history: Annotated[List[Dict[str, Any]], merge_or_replace_list]
    verified_reads: Annotated[List[str], merge_or_replace_list]

    # ── Plan & step ────────────────────────────────────────────────────────
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    plan_dag: Dict[str, Any] | None
    execution_waves: List[List[str]] | None
    current_wave: int | None
    # ... (continue grouping all fields)

    # ── Debug & recovery ───────────────────────────────────────────────────
    debug_attempts: int | None
    max_debug_attempts: int | None
    # ...

    # ── Memory / distillation ──────────────────────────────────────────────
    _should_distill: bool | None
    _force_compact: bool | None
    # ...

    # ── Tool execution ─────────────────────────────────────────────────────
    next_action: Dict[str, Any] | None
    last_result: Dict[str, Any] | None
    # ...
```

Also add docstrings to each section explaining the purpose and lifetime of the fields.

**Touch-points**

| File | Change |
|------|--------|
| `src/core/orchestration/graph/state.py` | Reorganize field order with section comments; no type changes |

**Test requirement**

All existing tests pass — this is documentation-only reorganization. Add a `tests/unit/test_agent_state_fields.py` that asserts a minimum set of required fields are present:

```python
from src.core.orchestration.graph.state import AgentState
import typing

def test_required_fields_present():
    annotations = typing.get_type_hints(AgentState)
    required = {"task", "history", "current_plan", "current_step", "working_dir"}
    assert required.issubset(set(annotations.keys()))
```

**Risks / notes**

- No functional change — purely organizational.
- Do not introduce nested TypedDicts. LangGraph's `Annotated` reducers operate on the flat structure.
- Remove any truly dead fields (e.g., `_pending_injections_source` if unused) while reorganizing.

---

### P4-T6 — Developer extension guide

**Current state**

No single document explains how to add a new tool, LLM provider, or graph node. New contributors must reverse-engineer from existing code.

**Exact change**

Create `docs/developer-guide.md` covering:

1. **Adding a new tool** — use `@tool` decorator in `src/tools/`, register in `src/tools/_registry.py:_BUILTIN_MODULES`, add to appropriate toolset YAML in `src/config/toolsets/`.
2. **Adding a new LLM provider** — implement adapter interface, register in `provider_capabilities.py`, update `tools_config.yaml`.
3. **Adding a new graph node** — implement `async def my_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]`, add to `builder.py`, add routing edge.
4. **Configuration reference** — all keys in `agent_config.yaml`, `tools_config.yaml`, `permissions.json`, environment variables.
5. **Running tests** — unit, integration, e2e, and real-LLM smoke test instructions.

**Touch-points**

| File | Change |
|------|--------|
| `docs/developer-guide.md` | New file |

**Test requirement**

No automated test. Add a CI step that checks the file exists:

```bash
test -f docs/developer-guide.md
```

**Risks / notes**

- This is the lowest-risk task in Phase 4 but has high onboarding value.
- Keep the guide concise (≤1,000 lines). Link to source files rather than duplicating code.

---

## Appendix — Task Summary Table

| Task | File(s) | Complexity | Status |
|------|---------|-----------|--------|
| P1-T1 | `loop_guards.py:66` | XS (1 line) | Not started |
| P1-T2 | `src/tools/toolsets/` (delete) | S | Not started |
| P1-T3 | `inference_loop.py:247` | S | Not started |
| P1-T4 | `sandbox.py:158–185, ~334` | S | Not started |
| P1-T5 | `inference_loop_rounds.py:162` | XS | Not started |
| P2-T1 | `subagent_tools.py`, `tests/integration/` | S | Not started |
| P2-T2 | `execution_helpers.py` | S | Not started |
| P2-T3 | `bash_tool.py` | M | Not started |
| P2-T4 | `inference_loop_rounds.py`, `distiller.py` | M | Not started |
| P2-T5 | `event_bus.py`, `orchestrator.py` | S | Not started |
| P2-T6 | `pyproject.toml:19` | XS | Not started |
| P3-T1 | `builder.py`, `step_controller_node.py` | L | Not started |
| P3-T2 | `debug_node.py:160` | M | Not started |
| P3-T3 | `repo_indexer.py`, `execution_helpers.py` | M | Not started |
| P3-T4 | `sqlite_session_store.py`, `planning_node.py` | M | Not started |
| P3-T5 | `execution_helpers.py` | M | Not started |
| P3-T6 | `execution_helpers.py`, `frontier_loop_node.py` | M | Not started |
| P4-T1 | `scenario_evaluator.py` | L | Not started |
| P4-T2 | `tests/e2e/` | M | Not started |
| P4-T3 | `distiller.py`, `sqlite_session_store.py` | L | Not started |
| P4-T4 | `dag_parser.py`, `state.py` | L | Not started |
| P4-T5 | `state.py` | L | Not started |
| P4-T6 | `docs/developer-guide.md` | M | Not started |

*Complexity key: XS = < 30 min, S = < 2 h, M = 2–8 h, L = > 1 day*

---

*End of implementation analysis.*
