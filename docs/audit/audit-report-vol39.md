# Code Quality Audit — Vol 39

**Date:** 2026-05-02
**Auditor:** OpenCode (automated structural review)
**Scope:** 20 files across `src/core/inference/` and `src/core/`

---

## Files Audited

| # | File |
|---|------|
| 1 | `src/core/inference/adapter_wrappers.py` |
| 2 | `src/core/inference/hardware_capability_profile.py` |
| 3 | `src/core/inference/kv_cache_governor.py` |
| 4 | `src/core/inference/model_capability_profile.py` |
| 5 | `src/core/inference/model_tiers.py` |
| 6 | `src/core/inference/runtime_profile.py` |
| 7 | `src/core/inference/thinking_utils.py` |
| 8 | `src/core/inference/tokenizer.py` |
| 9 | `src/core/inference/workflow_selector.py` |
| 10 | `src/core/inference/adapters/anthropic_adapter.py` |
| 11 | `src/core/inference/adapters/github_copilot_adapter.py` |
| 12 | `src/core/inference/adapters/github_copilot_auth.py` |
| 13 | `src/core/inference/adapters/mock_adapter.py` |
| 14 | `src/core/config_loader.py` |
| 15 | `src/core/credentials.py` |
| 16 | `src/core/errors.py` |
| 17 | `src/core/logger.py` |
| 18 | `src/core/paths.py` |
| 19 | `src/core/user_prefs.py` |
| 20 | `src/core/utils/retry.py` |

---

## Summary Table

| ID | Severity | File | Line(s) | Category | Short Description |
|----|----------|------|---------|----------|-------------------|
| H-1 | High | `model_capability_profile.py` | 205–212 | Dead code / logic bug | Duplicate `elif tier == ModelTier.MEDIUM` branch — second branch is unreachable; `MEDIUM` logic is wrong |
| H-2 | High | `adapter_wrappers.py` | 45 | Inline import | `from src.core.inference.telemetry import publish_model_response` inside a hot-path method |
| M-1 | Medium | `model_capability_profile.py` | 229–243 | Missing constants | Tool/turn limit dicts in `get_model_profile()` duplicate the `_TOOL_LIMITS` / `_MAX_TURNS` dicts already defined in `model_tiers.py` |
| M-2 | Medium | `hardware_capability_profile.py` | 161–178 | Inline import | `import ctypes` and class `MEMORYSTATUS` defined inside `_detect_ram()` body on every call |
| M-3 | Medium | `hardware_capability_profile.py` | 206–214 | Inline import | `import os` inside `_detect_cpu_cores()` — `os` is a standard library module and should be at module level |
| M-4 | Medium | `config_loader.py` | 124–125 | Redundant assignment | `ctx_name = ctx_name = ".codingAgent"` — double assignment is a typo artifact |
| M-5 | Medium | `config_loader.py` | 85–89 | Dead branch | `_get_workspace_config_paths()` constructs `candidate` then returns the same list regardless of whether `candidate.exists()` — the `if/else` is functionally identical |
| M-6 | Medium | `credentials.py` | 103–132 / 163–194 | Duplicate logic | The atomic-write + chmod + fallback block in `_prefs_set()` is copy-pasted verbatim into `_prefs_delete()` — should be extracted to a shared helper |
| M-7 | Medium | `thinking_utils.py` | 24–27 | Duplicate type definition | `ThinkingMode` enum is defined here **and** in `model_capability_profile.py` (line 28); they are identical but live in two modules |
| M-8 | Medium | `tokenizer.py` | 69–92 | Duplicate inline import | `from transformers import AutoTokenizer` is imported twice inside `_get_hf_tokenizer()` (local try/local path and Hub path) — one module-level conditional import would be cleaner |
| M-9 | Medium | `github_copilot_auth.py` | 131–139 | Inline import | `import shutil` inside `_auth_json_path()` migration block — runs on every auth.json path lookup |
| M-10 | Medium | `anthropic_adapter.py` | 211 / 284 | Inline import | `import requests` inside `get_models_from_api()` and `validate_connection()` — repeated inline import of the same third-party library |
| M-11 | Medium | `github_copilot_adapter.py` | 77 / 131 / 218 / 256 / 276 | Inline import | Multiple inline imports of `github_copilot_auth` functions scattered across methods; a single top-level import would be clearer and cheaper |
| L-1 | Low | `errors.py` | 229–261 | Module-level constant inside function | `_LABELS` dict is rebuilt on every call to `error_code_label()` — should be a module-level constant |
| L-2 | Low | `hardware_capability_profile.py` | 310–333 | Duplicate logic | `compute_safe_context_tokens()` (module-level) is functionally identical to `ModelProfile.estimate_safe_context()` in `model_capability_profile.py` line 74–80; one should delegate to the other |
| L-3 | Low | `kv_cache_governor.py` | 134–139 | Duplicate logic | `KVCacheGovernor.estimate_tokens_for_vram()` duplicates the formula in `compute_safe_context_tokens()` from `hardware_capability_profile.py` |
| L-4 | Low | `paths.py` | 25–32 | Redundant function | `get_config_dir()` is an alias for `get_data_dir()` with no distinction; the docstrings claim different purposes (config vs data) but they return the same value |
| L-5 | Low | `user_prefs.py` | 74–128 | Over-complicated pattern | `save()` has four nested layers of try/except with `_fd = None` / `_tmp = None` pre-declarations to handle `mkstemp` — the existing `atomic_write_json` helper (already tried first) makes the fallback block unnecessary complexity |
| L-6 | Low | `tokenizer.py` | 217–220 | Dead code | `_check_cache_size()` is defined but never called; the bounded LRU approach it attempts is not wired anywhere |
| L-7 | Low | `logger.py` | 451 | Dead variable | `_installed_handler = False` is set to `True` on line 473 inside `install_stdlib_handler()` but this module-level flag is never read by anything — the idempotency guard on line 464 already works without it |
| L-8 | Low | `github_copilot_auth.py` | 516–517 | Redundant assignment | `domain = entry.get("enterpriseUrl") or "github.com"` and `enterprise_url = entry.get("enterpriseUrl") or None` extract the same key twice; one variable is enough |
| L-9 | Low | `config_loader.py` | 110–121 | Inline import | `from src.tools.tools_config import agent_context_path` and `get_context_dir_name` are imported inline inside `load_merged_config()` on every call |

---

## Detailed Findings

---

### H-1 · High — Dead / unreachable branch that silently applies wrong values
**File:** `src/core/inference/model_capability_profile.py` · Lines 205–212

**Problem:**
```python
if tier == ModelTier.SMALL:
    params = 14; kv = 1.2; weights = 8.0
elif tier == ModelTier.MEDIUM:   # ← first MEDIUM branch
    params = 14; kv = 1.2; weights = 8.0
elif tier == ModelTier.MEDIUM:   # ← second MEDIUM branch — DEAD, never reached
    params = 27; kv = 1.6; weights = 14.0
elif tier == ModelTier.LARGE:
    ...
```
The second `elif tier == ModelTier.MEDIUM` is unreachable because the first one already matches. The intended third-tier values (`params=27, kv=1.6, weights=14.0`) are never applied. This is almost certainly a copy-paste error where one branch was meant to be `ModelTier.MEDIUM` and the next `ModelTier.LARGE` (with `ModelTier.LARGE` pushed down). As written, any model in the MEDIUM tier gets SMALL-tier parameter assumptions.

**Fix:** Correct the branch structure — the second duplicate `ModelTier.MEDIUM` should be the correct successor tier (likely was intended to be an additional explicit tier, or the first should be deleted):
```python
if tier == ModelTier.SMALL:
    params = 9; kv = 1.2; weights = 6.0
elif tier == ModelTier.MEDIUM:
    params = 27; kv = 1.6; weights = 14.0
elif tier == ModelTier.LARGE:
    params = 70; kv = 2.0; weights = 35.0
else:  # FRONTIER
    params = 200; kv = 2.0; weights = 0
```

---

### H-2 · High — Inline import inside a hot-path method
**File:** `src/core/inference/adapter_wrappers.py` · Line 45

**Problem:**
```python
def _publish_telemetry(self, out: Dict[str, Any]) -> None:
    try:
        if self.event_bus is not None:
            try:
                from src.core.inference.telemetry import publish_model_response
            except Exception:
                publish_model_response = None
```
`_publish_telemetry()` is called on every `generate()` invocation. The inline import runs the module resolution machinery on every call. When the telemetry module is absent the import silently fails and assigns `None` — but this happens repeatedly rather than once.

**Fix:** Move the import to module level with a graceful fallback:
```python
try:
    from src.core.inference.telemetry import publish_model_response as _publish_model_response
except Exception:
    _publish_model_response = None
```
Then call `_publish_model_response(...)` directly.

---

### M-1 · Medium — Duplicated tier→limit lookup tables
**File:** `src/core/inference/model_capability_profile.py` · Lines 229–243

**Problem:**
```python
tool_limit={
    ModelTier.SMALL: 20,
    ModelTier.MEDIUM: 35,
    ModelTier.LARGE: 50,
    ModelTier.FRONTIER: 60,
}[tier],
max_turns={
    ModelTier.SMALL: 25,
    ModelTier.MEDIUM: 40,
    ModelTier.LARGE: 60,
    ModelTier.FRONTIER: 80,
}[tier],
```
`model_tiers.py` already defines `_TOOL_LIMITS` (line 37) and `_MAX_TURNS` (line 203) with the same keys. The hardcoded inline dicts create a second source of truth. Any future limit adjustment must be made in both places.

**Fix:** Import and reuse the existing constants:
```python
from .model_tiers import get_tool_limit, get_max_turns
...
tool_limit=get_tool_limit(tier),
max_turns=get_max_turns(tier),
```

---

### M-2 · Medium — `import ctypes` and local class definition inside function body
**File:** `src/core/inference/hardware_capability_profile.py` · Lines 180–200

**Problem:**
```python
def _detect_ram() -> float:
    ...
    else:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        c_ulong = ctypes.c_ulong
        class MEMORYSTATUS(ctypes.Structure):
            _fields_ = [...]
```
Defining a `ctypes.Structure` subclass inside a function body re-executes the class body on every call. On Windows, `_detect_ram()` may be called repeatedly (e.g. by `detect_hardware()` which can be invoked per-request via `get_hardware_profile("auto")`). The class creation cost and repeated import are unnecessary.

**Fix:** Extract the Windows code path into a module-level helper or use a `functools.lru_cache` on `_detect_ram`.

---

### M-3 · Medium — `import os` inside `_detect_cpu_cores()`
**File:** `src/core/inference/hardware_capability_profile.py` · Lines 209–211

**Problem:**
```python
def _detect_cpu_cores() -> tuple[int, int]:
    try:
        import os
        cores = os.cpu_count() or 4
```
`os` is already imported at the top of the file (line 9 implicitly through platform; actually `os` is **not** imported at module level in this file — which is why it appears inline). However `platform` and `subprocess` are at module level. This is inconsistent and surprising. `os` is a standard library module and should always be a module-level import.

**Fix:** Add `import os` at module level alongside the existing imports and remove the inline import.

---

### M-4 · Medium — Double assignment typo
**File:** `src/core/config_loader.py` · Line 125

**Problem:**
```python
ctx_name = ctx_name = ".codingAgent"
```
Self-assignment `ctx_name = ctx_name = ...` is a clear typo (copy-paste remnant). Python parses this without error but any linter will flag it. The intent is simply `ctx_name = ".codingAgent"`.

**Fix:** `ctx_name = ".codingAgent"`

---

### M-5 · Medium — Dead if/else branch in `_get_workspace_config_paths()`
**File:** `src/core/config_loader.py` · Lines 85–89

**Problem:**
```python
candidate = cwd / ctx_name
if candidate.exists():
    return [candidate / "config.json", candidate / "config.local.json"]
return [candidate / "config.json", candidate / "config.local.json"]
```
Both branches of the `if/else` return exactly the same list. The `if candidate.exists()` check is pointless — it changes no behaviour. The callers that actually load these paths already check `path.exists()` before reading.

**Fix:** Remove the condition and return directly:
```python
return [candidate / "config.json", candidate / "config.local.json"]
```

---

### M-6 · Medium — Duplicated atomic-write + fallback block in `credentials.py`
**File:** `src/core/credentials.py` · Lines 103–150 and 163–210

**Problem:**
The try/except block that attempts `atomic_write_json`, then falls back to `mkstemp+replace`, then applies `chmod 0o600` appears twice — once in `_prefs_set()` and once in `_prefs_delete()`. The two copies are structurally identical, differing only in which `data` dict is being serialised. Any bug fix or improvement to the write flow must be applied to both copies.

**Fix:** Extract a `_write_prefs(data: dict) -> None` helper and call it from both `_prefs_set()` and `_prefs_delete()`.

---

### M-7 · Medium — Duplicate `ThinkingMode` enum definition
**File:** `src/core/inference/thinking_utils.py` (line 24) **and** `src/core/inference/model_capability_profile.py` (line 28)

**Problem:**
Both files define a `ThinkingMode(str, Enum)` with identical members `OFF`, `AUTO`, `ON`. Downstream code in `runtime_profile.py` imports `ThinkingMode` from `model_capability_profile`, while `thinking_utils.py` exports its own copy. This risks `isinstance` mismatches if both are ever used together, and any future value addition must be made in both places.

**Fix:** Delete the `ThinkingMode` definition from `thinking_utils.py` and import it from `model_capability_profile`:
```python
from src.core.inference.model_capability_profile import ThinkingMode
```

---

### M-8 · Medium — Duplicate inline import of `AutoTokenizer`
**File:** `src/core/inference/tokenizer.py` · Lines 70 and 83

**Problem:**
```python
try:
    from transformers import AutoTokenizer  # attempt 1: local path
    ...
except Exception:
    pass

try:
    from transformers import AutoTokenizer  # attempt 2: Hub path
    ...
```
`AutoTokenizer` is imported twice in the same function. If `transformers` is not installed, both imports fail independently. If it is installed, the second import is a no-op redundant lookup. The module-unavailability should be tested once.

**Fix:** Check `transformers` availability once at module level (or once at the top of the function) and branch on local vs Hub path without repeating the import:
```python
try:
    from transformers import AutoTokenizer as _AutoTokenizer
    _HF_AVAILABLE = True
except ImportError:
    _HF_AVAILABLE = False
```

---

### M-9 · Medium — `import shutil` inside `_auth_json_path()` migration block
**File:** `src/core/inference/adapters/github_copilot_auth.py` · Line 139

**Problem:**
```python
def _auth_json_path() -> Path:
    ...
    import shutil
    shutil.copy2(old_path, new_path)
```
`_auth_json_path()` is called by `_read_auth_json()` and `_write_auth_json()` — both of which are invoked on every token load/save (i.e. on every API request). The migration block includes an inline `import shutil`. While Python caches modules, the import lookup still runs each time and obscures intent.

**Fix:** Move `import shutil` to module level alongside the other standard library imports.

---

### M-10 · Medium — Repeated inline `import requests` in `AnthropicAdapter`
**File:** `src/core/inference/adapters/anthropic_adapter.py` · Lines 211 and 284

**Problem:**
```python
def get_models_from_api(self) -> Dict[str, Any]:
    import requests  # local import to stay consistent with rest of codebase
    ...

def validate_connection(self) -> bool:
    import requests  # local import
    ...
```
`requests` is imported inline in two separate methods with duplicate comments. The parent class `OpenAICompatibleAdapter` presumably already depends on `requests`. Inline imports here add unnecessary overhead and split the dependency declaration.

**Fix:** Use a single module-level `import requests` (already a hard dependency of the adapter chain). Remove the duplicate inline imports and their comments.

---

### M-11 · Medium — Multiple inline imports of `github_copilot_auth` across methods
**File:** `src/core/inference/adapters/github_copilot_adapter.py` · Lines 77, 131, 218, 256, 276

**Problem:**
Five separate inline imports from `github_copilot_auth` are scattered across `__init__`, `_headers()`, `_chat_internal()`, `get_models_from_api()`, and `validate_connection()`:
```python
from src.core.inference.adapters.github_copilot_auth import load_enterprise_url
from src.core.inference.adapters.github_copilot_auth import load_token
from src.core.inference.adapters.github_copilot_auth import clear_token
from src.core.inference.adapters.github_copilot_auth import is_authenticated
```
The inline pattern was presumably used to avoid circular imports, but since `github_copilot_auth` imports from `src.core.auth.device_flow` (not from this adapter), there is no circular dependency.

**Fix:** Import all needed symbols at module level in a single import block:
```python
from src.core.inference.adapters.github_copilot_auth import (
    load_enterprise_url, load_token, clear_token, is_authenticated,
)
```

---

### L-1 · Low — `_LABELS` dict rebuilt on every call
**File:** `src/core/errors.py` · Lines 229–261

**Problem:**
```python
def error_code_label(code: ErrorCode) -> str:
    _LABELS: dict[str, str] = {
        ErrorCode.TOOL_PERMISSION_DENIED: "Permission denied",
        ...  # 20+ entries
    }
    return _LABELS.get(code, code.value)
```
The `_LABELS` dict is allocated and populated on every invocation of `error_code_label()`. Since `ErrorCode` values are immutable, the dict content is constant and should be defined once at module level.

**Fix:** Move `_LABELS` to module scope above `error_code_label()`.

---

### L-2 · Low — Duplicate safe-context calculation across two modules
**File:** `src/core/inference/hardware_capability_profile.py` (lines 310–333) and `src/core/inference/model_capability_profile.py` (lines 74–80)

**Problem:**
`compute_safe_context_tokens(vram_gb, model_weights_gb, kv_per_token_mb, overhead_gb)` in `hardware_capability_profile.py` and `ModelProfile.estimate_safe_context(self, vram_gb, overhead_gb)` in `model_capability_profile.py` implement the same formula:
```
available = vram_gb - weights - overhead
tokens = available * (1024 / kv_per_token_mb)
return max(tokens, 8192)
```
The only difference is that `estimate_safe_context` clamps to `max_context` while `compute_safe_context_tokens` returns an unclamped value. Both callers in `runtime_profile.py` then clamp anyway.

**Fix:** Have `ModelProfile.estimate_safe_context()` delegate to the module-level function, or vice versa. Eliminate one implementation.

---

### L-3 · Low — Duplicated token-estimation formula in `KVCacheGovernor`
**File:** `src/core/inference/kv_cache_governor.py` · Lines 134–139

**Problem:**
```python
def estimate_tokens_for_vram(self, vram_gb: float) -> int:
    available = vram_gb - self.model_weights_gb - self.overhead_gb
    if available <= 0:
        return 8192
    return int((available * 1024 / self.kv_per_token_mb) * 1000)
```
This is the same formula as `compute_safe_context_tokens()` in `hardware_capability_profile.py`. The `* 1000` factor appears to be a unit inconsistency (the module-level function does not multiply by 1000; the governor's `max_tokens` init on line 98 already accounts for the 1000-token unit).

**Fix:** Verify the unit convention and unify under one helper. The `* 1000` inconsistency between line 98 and line 139 may itself be a latent bug worth confirming.

---

### L-4 · Low — `get_config_dir()` is a redundant alias for `get_data_dir()`
**File:** `src/core/paths.py` · Lines 25–31

**Problem:**
```python
def get_config_dir() -> Path:
    """Return the config directory.
    Windows: %LOCALAPPDATA%/CodingAgent
    Unix/macOS: ~/.coding_agent
    """
    return get_data_dir()
```
`get_config_dir()` and `get_data_dir()` return identical paths. The distinction suggested by the names (config vs data) is not enforced — they both point to the same location. Any caller of `get_config_dir()` could equivalently call `get_data_dir()`. This creates false conceptual separation.

**Fix:** Either (a) remove `get_config_dir()` and update all callers to use `get_data_dir()`, or (b) give them genuinely different paths (e.g., `~/.config/codingagent` for config vs `~/.local/share/codingagent` for data, following XDG conventions).

---

### L-5 · Low — Over-complicated fallback chain in `UserPrefs.save()`
**File:** `src/core/user_prefs.py` · Lines 72–141

**Problem:**
The `save()` method has four nested `try/except` layers:
1. Attempt `atomic_write_json` (imported inline)
2. If that fails, attempt `tempfile.mkstemp` + `os.replace`
3. If that fails, attempt `shutil.move`
4. If that fails, call `write_text` directly

The `atomic_write_json` helper in `src/core/io_utils` already handles the mkstemp+replace pattern internally. Layers 2–4 of this fallback chain duplicate what `atomic_write_json` does, so they can never be reached unless `io_utils` itself has a bug — in which case the fallback to a non-atomic `write_text` is unsafe anyway.

Additionally `import tempfile` and `import traceback` appear inline within the fallback blocks.

**Fix:** Simplify to two paths only: (1) `atomic_write_json`, (2) bare `write_text` as last resort with a clear warning. Move `import tempfile` and `import traceback` to module level.

---

### L-6 · Low — Dead function `_check_cache_size()`
**File:** `src/core/inference/tokenizer.py` · Lines 217–220

**Problem:**
```python
def _check_cache_size():
    """Internal: clear cache if it exceeds reasonable size."""
    if len(_HF_TOKENIZERS) > 10:  # Arbitrary limit
        _HF_TOKENIZERS.clear()
```
This function is defined but never called anywhere in the file or (based on the audit scope) elsewhere. The cache-size problem it aims to solve is real (unbounded `_HF_TOKENIZERS` dict), but the solution is wired to nothing. It either needs to be called (e.g., at the top of `_get_hf_tokenizer()`) or removed.

**Fix:** Call `_check_cache_size()` at the start of `_get_hf_tokenizer()`, or remove the function and replace `_HF_TOKENIZERS` with an `lru_cache`-based approach.

---

### L-7 · Low — `_installed_handler` flag is never read
**File:** `src/core/logger.py` · Lines 451 and 473

**Problem:**
```python
_installed_handler = False   # module level

def install_stdlib_handler(level: int = logging.INFO) -> None:
    global _installed_handler
    ...
    _installed_handler = True   # set but never read
```
The idempotency guard inside `install_stdlib_handler()` works by inspecting existing root logger handlers (line 464–466), not by reading `_installed_handler`. The module-level flag and the `global` statement are dead — setting `_installed_handler = True` has no observable effect.

**Fix:** Remove the `_installed_handler` module-level variable and the `global _installed_handler` / assignment inside the function.

---

### L-8 · Low — Redundant double-read of `enterpriseUrl` key in `_load_token_impl()`
**File:** `src/core/inference/adapters/github_copilot_auth.py` · Lines 516–517

**Problem:**
```python
domain = entry.get("enterpriseUrl") or "github.com"
enterprise_url = entry.get("enterpriseUrl") or None
```
The same dictionary key is accessed twice to produce two variables that differ only in their default values. This is a minor clarity issue but a real redundancy.

**Fix:**
```python
enterprise_url = entry.get("enterpriseUrl") or None
domain = enterprise_url or "github.com"
```

---

### L-9 · Low — Repeated inline imports inside `load_merged_config()`
**File:** `src/core/config_loader.py` · Lines 110–121

**Problem:**
```python
def load_merged_config(working_dir=None):
    ...
    try:
        from src.tools.tools_config import agent_context_path
        ...
    except Exception:
        try:
            from src.tools.tools_config import get_context_dir_name
            ...
```
Two separate `from src.tools.tools_config import ...` statements live inside `load_merged_config()`, which is called on every config read (including via the hot-path `get_global_config()` during cache-miss). While Python caches module imports, the repeated attribute lookups and try/except overhead accumulates unnecessarily.

**Fix:** Move both imports to module level with a single graceful fallback:
```python
try:
    from src.tools.tools_config import agent_context_path, get_context_dir_name
except Exception:
    agent_context_path = None
    get_context_dir_name = None
```

---

## Clean Files

The following files from the audit set had **no findings** — they are well-structured, avoid the listed anti-patterns, and contain no dead code or duplicate logic identified:

| File | Notes |
|------|-------|
| `src/core/inference/kv_cache_governor.py` | Clean aside from L-3 (shared formula with hardware profile); the governor itself is well-structured |
| `src/core/inference/model_tiers.py` | Clean — well-defined constants, clear classification logic, no duplication within the file |
| `src/core/inference/runtime_profile.py` | Clean — straightforward merge logic, single responsibility |
| `src/core/inference/workflow_selector.py` | Clean — concise, readable binary selection logic |
| `src/core/inference/adapters/mock_adapter.py` | Clean — deterministic, single-purpose, no dead code |
| `src/core/errors.py` | Clean aside from L-1 (the `_LABELS` dict placement); the error taxonomy itself is solid |
| `src/core/utils/retry.py` | Clean — well-structured, single responsibility, good use of `functools.wraps` |

---

## Finding Count by Severity

| Severity | Count |
|----------|-------|
| High | 2 |
| Medium | 11 |
| Low | 9 |
| **Total** | **22** |

---

## Priority Fix Order

1. **H-1** — Fix the unreachable duplicate `ModelTier.MEDIUM` branch immediately; it silently produces wrong model parameters for all MEDIUM-tier fallback lookups.
2. **M-6** — Extract the shared `_write_prefs()` helper in `credentials.py` to eliminate the duplicated atomic-write block before the next credentials-related change.
3. **M-7** — Deduplicate `ThinkingMode` enum — the risk of `isinstance` mismatches will grow as more code imports from both modules.
4. **H-2** — Move the telemetry import out of the hot-path `_publish_telemetry()` method.
5. **L-1** — Trivial one-line fix: hoist `_LABELS` to module scope in `errors.py`.
6. **L-6** — Either wire `_check_cache_size()` or delete it.
