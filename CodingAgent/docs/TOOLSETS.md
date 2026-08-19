**Toolset Loader**

Summary
The toolset loader supports both YAML (.yaml / .yml) and JSON (.json) toolset files and provides a model-aware loading helper that prefers a format depending on the target LLM. There are two loader modules in the repo: the legacy/primary YAML-first loader (src/tools/toolsets/loader.py) and the config-oriented loader with model-aware features (src/config/toolsets/loader.py).

**Supported Formats**
- YAML: .yaml and .yml
- JSON: .json

Toolset filenames are discovered by stem (e.g. coding.yaml, coding.json) and listed by list_available_toolsets().

**Model Heuristic**
The loader contains a conservative heuristic to decide whether to prefer YAML (smaller/lighter models) or JSON (bigger/frontier models). Key points:
- Big-model tokens (take precedence): "gpt-4", "gpt4", "4o", "gpt-4o". If any of these appear in the model string the loader treats the model as "big".
- Small-model tokens: "mini", "small", "lite", "3.5", "gpt-3", "gpt3", "ada", "babbage", "curie", "turbo".
- Default: when model is None or no markers are found, the loader defaults to YAML (safe, backward-compatible behaviour).

Examples:
- "gpt-4o-mini" is treated as big because of the "4o" token (big tokens have precedence).
- "gpt-3.5-mini" is treated as small and will prefer YAML.

**APIs**
- load_toolset(name): classic loader — loads and caches the toolset by name. Returns the parsed dict or None.
- load_toolset_for_model(name, model): model-aware loader — prefers YAML for small models and JSON for big models. Consults a format-aware cache and falls back to available files when the preferred format is missing. Note: callers that need model-aware behaviour should call this helper directly.
- list_available_toolsets(): lists available toolset stems across both YAML and JSON files.
- clear_cache(): invalidates both the name-keyed cache and the format-aware cache.

**Cache Semantics**
- _cache (name-keyed): used by load_toolset(name) and keyed only by toolset name.
- _format_cache (format-aware): keyed by (toolset_name, format, dir_path) to avoid collisions when the toolsets directory changes (tests often monkeypatch _DIR). This is used by load_toolset_for_model and is populated by both load_toolset and load_toolset_for_model.
- Behavioural note: load_toolset(name) populates both the name-keyed cache and the format-aware cache for the format it loaded. load_toolset_for_model populates only the format-aware cache. To obtain model-aware results, call load_toolset_for_model(name, model). If you rely on load_toolset(name) but also call load_toolset_for_model elsewhere, be aware that load_toolset(name) will continue to return whatever was cached under the plain name-keyed cache unless you clear_cache().

**Orchestrator Integration**
- The orchestrator helper that selects tools for a role (get_tools_for_role_impl in src/core/orchestration/task_lifecycle.py) will use a model-aware loader when available: it dynamically imports the toolset loader module and, if that module exposes load_toolset_for_model, calls it with orch._model as the model hint. If the loader module does not expose the model-aware helper, the orchestrator falls back to the classic get_tools_for_toolset(name) behaviour.
- Practical implication: for model-format selection to be used automatically by the orchestrator, the loader module referenced at runtime must expose load_toolset_for_model (the config-oriented loader in src/config/toolsets/loader.py does; the legacy src/tools/toolsets/loader.py currently does not). Callers that know the model should call load_toolset_for_model explicitly to avoid depending on which loader module is imported at runtime.

**Recommendations & Troubleshooting**
- To ensure consistent model-aware behaviour in your runtime or tests, call load_toolset_for_model(name, model) and avoid relying on the plain load_toolset(name) when model-specific format selection matters.
- After editing toolset files on disk (YAML/JSON), call clear_cache() so subsequent loads read the updated files.
- In tests that monkeypatch loader._DIR, either monkeypatch the caches (loader._cache / loader._format_cache) or call clear_cache() to avoid stale entries.

**Files of interest**
- Model-aware loader: src/config/toolsets/loader.py
- Legacy/primary loader: src/tools/toolsets/loader.py
- Orchestrator integration: src/core/orchestration/task_lifecycle.py (get_tools_for_role_impl)
