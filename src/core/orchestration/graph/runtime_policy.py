"""Model-aware runtime policy resolution and flat toolset schema filtering.

Maps 2026 model capabilities across four operational profiles while sharing a
single unified LangGraph topology:
- ``nano``     (<8.5B local, e.g. Gemma 4 4B, Qwen 2.5 7B): 12 tools, 16 turns, verification optional
- ``compact``  (8.5B–14.9B local, e.g. Qwen3.5-9B, Qwen3-14B, Gemma 4 12B, Phi-4): 12 tools, 24 turns, targeted verification
- ``standard`` (15B–70B dense & MoE, e.g. Gemma 4 26B-A4B, Qwen3.5-27B/35B-A3B): 18 tools, 36 turns, targeted verification
- ``frontier`` (>70B & 2026 cloud frontier, e.g. Claude 4.5/4.6, GPT-5/o3/o4, Gemini 2.5/3, DeepSeek R1/V3): 25 tools, 60 turns, full verification
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

from src.core.inference.model_tiers import ModelTier, classify_model, is_nano_model
from src.tools._tool import PermissionKind

logger = logging.getLogger(__name__)

PolicyTier = Literal["nano", "compact", "standard", "frontier"]
TargetTier = Literal["medium", "frontier"]
VerificationMode = Literal["targeted", "full"]

_STRICT_READONLY_ROLES: frozenset[str] = frozenset(
    {"analyst", "scout", "explore", "strategic", "planner", "researcher"}
)
_STRICT_READONLY_BLOCKED_NAMES: frozenset[str] = frozenset(
    {
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "write_file",
        "multiedit",
        "apply_patch",
        "delete_file",
        "create_directory",
        "rename_file",
        "copy_file",
        "ast_rename",
        "bash",
        "run_tests",
        "run_linter",
        "delegate_task",
        "git_commit",
        "manage_todo",
    }
)
_REVIEWER_BLOCKED_NAMES: frozenset[str] = frozenset(
    {
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "write_file",
        "multiedit",
        "apply_patch",
        "delete_file",
        "create_directory",
        "rename_file",
        "copy_file",
        "ast_rename",
        "bash",
        "delegate_task",
        "git_commit",
    }
)


@dataclass(frozen=True)
class ModelRuntimePolicy:
    """Immutable runtime execution profile derived from model + workflow config."""

    policy_tier: PolicyTier
    target_tier: TargetTier
    toolset_name: str
    tool_limit: int
    turn_limit: int
    recovery_limit: int
    compaction_limit: int
    verification_mode: VerificationMode
    use_verification: bool
    readonly_parallelism: bool
    max_parallel_reads: int
    thinking_enabled: bool
    compaction_trigger_ratio: float
    compaction_target_ratio: float
    context_window: int
    tool_output_max_chars: int = 24_000

    def to_policy_dict(self) -> Dict[str, Any]:
        """Serialize into the ``state['runtime']['policy']`` contract dictionary."""
        return {
            "policy_tier": self.policy_tier,
            "toolset_name": self.toolset_name,
            "turn_limit": self.turn_limit,
            "tool_limit": self.tool_limit,
            "recovery_limit": self.recovery_limit,
            "compaction_limit": self.compaction_limit,
            "verification_mode": self.verification_mode,
            "use_verification": self.use_verification,
            "readonly_parallelism": self.readonly_parallelism,
            "max_parallel_reads": self.max_parallel_reads,
            "mutations_serialized": True,
            "preflight_required": True,
            "thinking_enabled": self.thinking_enabled,
            "compaction_trigger_ratio": self.compaction_trigger_ratio,
            "compaction_target_ratio": self.compaction_target_ratio,
            "context_window": self.context_window,
            "tool_output_max_chars": self.tool_output_max_chars,
        }


NANO_RUNTIME_POLICY = ModelRuntimePolicy(
    policy_tier="nano",
    target_tier="medium",
    toolset_name="compact-flat",
    tool_limit=10,
    turn_limit=16,
    recovery_limit=1,
    compaction_limit=1,
    verification_mode="targeted",
    use_verification=False,
    readonly_parallelism=False,
    max_parallel_reads=1,
    thinking_enabled=False,
    compaction_trigger_ratio=0.70,
    compaction_target_ratio=0.45,
    context_window=16384,
    tool_output_max_chars=6000,
)

COMPACT_RUNTIME_POLICY = ModelRuntimePolicy(
    policy_tier="compact",
    target_tier="medium",
    toolset_name="compact-flat",
    tool_limit=10,
    turn_limit=24,
    recovery_limit=2,
    compaction_limit=2,
    verification_mode="targeted",
    use_verification=True,
    readonly_parallelism=True,
    max_parallel_reads=2,
    thinking_enabled=True,
    compaction_trigger_ratio=0.72,
    compaction_target_ratio=0.45,
    context_window=32768,
    tool_output_max_chars=12000,
)

STANDARD_RUNTIME_POLICY = ModelRuntimePolicy(
    policy_tier="standard",
    target_tier="medium",
    toolset_name="standard-flat",
    tool_limit=14,
    turn_limit=36,
    recovery_limit=2,
    compaction_limit=2,
    verification_mode="targeted",
    use_verification=True,
    readonly_parallelism=True,
    max_parallel_reads=3,
    thinking_enabled=True,
    compaction_trigger_ratio=0.78,
    compaction_target_ratio=0.50,
    context_window=65536,
    tool_output_max_chars=24000,
)

FRONTIER_RUNTIME_POLICY = ModelRuntimePolicy(
    policy_tier="frontier",
    target_tier="frontier",
    toolset_name="frontier-flat",
    tool_limit=18,
    turn_limit=60,
    recovery_limit=3,
    compaction_limit=3,
    verification_mode="full",
    use_verification=True,
    readonly_parallelism=True,
    max_parallel_reads=5,
    thinking_enabled=True,
    compaction_trigger_ratio=0.80,
    compaction_target_ratio=0.50,
    context_window=200000,
    tool_output_max_chars=48000,
)

TIER_TOOL_OUTPUT_MAX_CHARS: Dict[PolicyTier, int] = {
    "nano": 6_000,
    "compact": 12_000,
    "standard": 24_000,
    "frontier": 48_000,
}

_POLICY_BY_TIER: Dict[PolicyTier, ModelRuntimePolicy] = {
    "nano": NANO_RUNTIME_POLICY,
    "compact": COMPACT_RUNTIME_POLICY,
    "standard": STANDARD_RUNTIME_POLICY,
    "frontier": FRONTIER_RUNTIME_POLICY,
}


def get_runtime_policy_for_tier(policy_tier: PolicyTier) -> ModelRuntimePolicy:
    """Return the baseline ``ModelRuntimePolicy`` for *policy_tier*."""
    return _POLICY_BY_TIER.get(policy_tier, STANDARD_RUNTIME_POLICY)


def resolve_policy_tier(model_name: str, context_window: int = 0) -> PolicyTier:
    """Resolve ``nano`` | ``compact`` | ``standard`` | ``frontier`` for *model_name*."""
    clean_name = (model_name or "").strip()
    if not clean_name or clean_name == "default":
        return "standard"

    tier_enum = classify_model(clean_name, context_window)
    if tier_enum == ModelTier.SMALL:
        return "nano" if is_nano_model(clean_name) else "compact"
    if tier_enum == ModelTier.MEDIUM:
        return "standard"
    return "frontier"


_VALID_POLICY_TIERS: frozenset[str] = frozenset({"nano", "compact", "standard", "frontier"})


def _resolve_provider_policy_tier_override(
    orchestrator: Any,
    provider: Optional[str],
    model: str,
) -> Optional[PolicyTier]:
    """Return an explicit ``policy_tier`` override configured for a provider or model.

    Lookup order:
      1. ``adapter.provider`` dict (``model_policy_tiers[model]`` then ``policy_tier``)
      2. ``UserPrefs`` ``providers.<provider>`` (``model_policy_tiers[model]`` then ``policy_tier``)
      3. Bundled ``src/config/providers.json`` matching *provider*
    """
    adapter = (
        (getattr(orchestrator, "_adapter", None) or getattr(orchestrator, "adapter", None))
        if orchestrator is not None
        else None
    )
    prov_name = (
        provider
        or (getattr(orchestrator, "_provider_name", None) if orchestrator is not None else None)
        or (getattr(adapter, "name", None) if adapter is not None else None)
        or ""
    )
    prov_key = str(prov_name).strip().lower().replace("-", "_").replace(" ", "_")
    model_lower = (model or "").strip().lower()

    def _extract_from_cfg(cfg: Any) -> Optional[PolicyTier]:
        if not isinstance(cfg, dict):
            return None
        mpt = cfg.get("model_policy_tiers")
        if isinstance(mpt, dict) and model_lower:
            for mk, mv in mpt.items():
                if str(mk).strip().lower() == model_lower:
                    cand = str(mv).strip().lower()
                    if cand in _VALID_POLICY_TIERS:
                        return cand  # type: ignore[return-value]
        pt = cfg.get("policy_tier")
        if isinstance(pt, str):
            cand = pt.strip().lower()
            if cand in _VALID_POLICY_TIERS:
                return cand  # type: ignore[return-value]
        return None

    # 1. Adapter provider dict
    if adapter is not None:
        hit = _extract_from_cfg(getattr(adapter, "provider", None))
        if hit:
            return hit

    if not prov_key:
        return None

    # 2. UserPrefs provider override
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
        provs = prefs.data.get("providers")
        if isinstance(provs, dict):
            hit = _extract_from_cfg(provs.get(prov_key))
            if hit:
                return hit
    except Exception:
        pass

    # 3. Bundled providers.json entry
    try:
        import json
        from pathlib import Path

        cfg_path = Path(__file__).parents[3] / "config" / "providers.json"
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                ek = str(entry.get("type") or entry.get("name") or "").strip().lower().replace("-", "_").replace(" ", "_")
                if ek == prov_key:
                    hit = _extract_from_cfg(entry)
                    if hit:
                        return hit
    except Exception:
        pass

    return None


def resolve_runtime_policy(
    orchestrator: Any = None,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    context_window: int = 0,
) -> ModelRuntimePolicy:
    """Resolve the authoritative ``ModelRuntimePolicy`` for an orchestrator or model.

    Honors explicit ``orchestrator.workflow_config`` overrides and per-provider /
    per-model ``policy_tier`` settings when present.
    """
    resolved_model = model or ""
    resolved_ctx = int(context_window or 0)

    if orchestrator is not None:
        if not resolved_model:
            try:
                from src.core.inference.provider_utils import resolve_provider_and_model

                _, _prov_model = resolve_provider_and_model(orchestrator)
                resolved_model = _prov_model or ""
            except Exception:
                resolved_model = getattr(orchestrator, "_model", "") or ""
        if resolved_ctx <= 0:
            adapter = getattr(orchestrator, "_adapter", None) or getattr(
                orchestrator, "adapter", None
            )
            try:
                resolved_ctx = int(getattr(adapter, "context_window", 0) or 0)
            except Exception:
                resolved_ctx = 0

    override_tier = _resolve_provider_policy_tier_override(
        orchestrator, provider, resolved_model
    )
    p_tier = override_tier or resolve_policy_tier(resolved_model, resolved_ctx)
    base = get_runtime_policy_for_tier(p_tier)

    if resolved_ctx > 0:
        base = replace(base, context_window=resolved_ctx)

    # Honor explicit WorkflowConfig attached to orchestrator (Finding 48)
    if orchestrator is not None:
        try:
            from src.core.inference.workflow_selector import WorkflowConfig, WorkflowType

            explicit_wf = getattr(orchestrator, "workflow_config", None)
            if isinstance(explicit_wf, WorkflowConfig):
                is_single = explicit_wf.workflow_type == WorkflowType.SINGLE_LOOP
                wf_thinking = (
                    str(
                        getattr(
                            explicit_wf.thinking_mode,
                            "value",
                            explicit_wf.thinking_mode,
                        )
                    ).lower()
                    != "off"
                )
                base = replace(
                    base,
                    policy_tier="nano" if is_single else base.policy_tier,
                    toolset_name="compact-flat" if is_single else base.toolset_name,
                    tool_limit=int(explicit_wf.tool_limit),
                    turn_limit=int(explicit_wf.max_turns),
                    context_window=int(explicit_wf.context_limit),
                    use_verification=bool(explicit_wf.use_verification),
                    readonly_parallelism=False if is_single else base.readonly_parallelism,
                    max_parallel_reads=1 if is_single else base.max_parallel_reads,
                    thinking_enabled=wf_thinking,
                )
        except Exception as exc:
            logger.debug("resolve_runtime_policy: workflow_config check skipped: %s", exc)

    return base


def _is_tool_permitted_for_context(
    tool_name: str,
    reg: Any,
    orchestrator: Any,
    active_role: str,
) -> bool:
    """Enforce read-only, role, explore_mode, and active_agent restrictions."""
    from src.core.orchestration.role_config import (
        is_tool_allowed_for_role,
        normalize_role,
    )

    norm_role = normalize_role(active_role or "operational")
    explore_mode = bool(getattr(orchestrator, "explore_mode", False))

    perm_kind = PermissionKind.NONE
    if hasattr(reg, "get_permission_kind") and callable(reg.get_permission_kind):
        try:
            perm_kind = reg.get_permission_kind(tool_name)
        except Exception:
            perm_kind = PermissionKind.NONE

    if explore_mode or norm_role in _STRICT_READONLY_ROLES:
        if tool_name in _STRICT_READONLY_BLOCKED_NAMES:
            return False
        if perm_kind in (
            PermissionKind.WRITE_FILE,
            PermissionKind.EXECUTE_BASH,
            PermissionKind.GIT_WRITE,
            PermissionKind.LSP_WRITE,
            PermissionKind.MEMORY,
            PermissionKind.DELEGATE,
        ):
            return False

    if norm_role == "reviewer":
        if tool_name in _REVIEWER_BLOCKED_NAMES:
            return False
        if perm_kind in (
            PermissionKind.WRITE_FILE,
            PermissionKind.GIT_WRITE,
            PermissionKind.LSP_WRITE,
            PermissionKind.DELEGATE,
        ):
            return False

    # Enforce role_config explicit allowed/denied lists for specialized roles
    if norm_role not in ("operational", "general"):
        if not is_tool_allowed_for_role(tool_name, norm_role):
            # Allow flat read primitives (glob, grep, git_read, lsp) on read/review roles
            if not (
                norm_role in ("reviewer", "strategic")
                and tool_name in {"glob", "grep", "git_read", "git_status", "git_diff", "git_log", "lsp", "find_symbol", "read_tool_output"}
            ):
                return False

    active_agent = getattr(orchestrator, "active_agent", None)
    if active_agent is not None and hasattr(active_agent, "is_tool_permitted"):
        try:
            if not active_agent.is_tool_permitted(tool_name):
                return False
        except Exception:
            pass

    return True


def _is_git_workspace(workdir: Any) -> bool:
    """Stat-only check for a ``.git`` entry in *workdir* or any ancestor directory."""
    if not workdir:
        return True
    try:
        cursor = Path(str(workdir)).resolve()
        for parent in (cursor, *cursor.parents):
            if (parent / ".git").exists():
                return True
        return False
    except OSError:
        return True


def _is_lsp_available(workdir: Any = None) -> bool:
    """Return True when at least one configured LSP server binary is available on PATH."""
    try:
        from src.core.indexing.lsp_manager import _find_cmd, _load_server_config

        cfg = _load_server_config()
        if not isinstance(cfg, Mapping):
            return False
        return any(
            isinstance(entry, Mapping) and _find_cmd(dict(entry)) is not None
            for entry in cfg.values()
        )
    except Exception:
        return False


def _has_active_background_tasks(orchestrator: Any = None) -> bool:
    """Return True when *orchestrator* has spawned a background bash job in this session."""
    return bool(getattr(orchestrator, "_has_background_tasks", False)) if orchestrator is not None else False


def _has_evicted_tool_outputs(orchestrator: Any = None) -> bool:
    """Return True when *orchestrator*'s ``ToolOutputStore`` has at least one evicted handle.

    On lightweight test mocks without ``execute_tool`` or ``_tool_output_store``,
    returns True so unit-test schema assertions stay deterministic.
    """
    if orchestrator is None:
        return True
    store = getattr(orchestrator, "_tool_output_store", None)
    if store is not None and hasattr(store, "stats"):
        try:
            return int(store.stats().get("entries", 0)) > 0
        except Exception:
            return True
    if hasattr(orchestrator, "execute_tool"):
        return False
    return True


def _is_conditionally_active_tool(tool_name: str, orchestrator: Any) -> bool:
    """Gate situational tools (`git_read`, `lsp`, `read_tool_output`) on live session/workspace state."""
    if tool_name == "git_read":
        return _is_git_workspace(getattr(orchestrator, "working_dir", None))
    if tool_name == "lsp":
        return _is_lsp_available(getattr(orchestrator, "working_dir", None))
    if tool_name == "read_tool_output":
        return _has_evicted_tool_outputs(orchestrator)
    return True


def resolve_effective_tool_schemas(
    orchestrator: Any,
    runtime_policy: Optional[ModelRuntimePolicy | Mapping[str, Any]] = None,
    active_role: str = "operational",
) -> List[Dict[str, Any]]:
    """Build the exact OpenAI tool schema list for one LLM turn.

    Loads the tier's flat primitive toolset YAML (``compact-flat``,
    ``standard-flat``, or ``frontier-flat``), intersects with role/explore/agent
    permissions, appends permitted MCP tools within ``tool_limit``, and degrades
    gracefully on lightweight mock registries used in unit tests.
    """
    if orchestrator is None:
        return []
    reg = getattr(orchestrator, "tool_registry", None)
    if reg is None:
        return []

    if isinstance(runtime_policy, ModelRuntimePolicy):
        policy_tier_str = str(runtime_policy.policy_tier)
        toolset_name = runtime_policy.toolset_name
        tool_limit = runtime_policy.tool_limit
    elif isinstance(runtime_policy, Mapping):
        policy_tier_str = str(runtime_policy.get("policy_tier") or "standard")
        toolset_name = str(runtime_policy.get("toolset_name") or "standard-flat")
        tool_limit = int(runtime_policy.get("tool_limit") or 18)
    else:
        resolved = resolve_runtime_policy(orchestrator)
        policy_tier_str = str(resolved.policy_tier)
        toolset_name = resolved.toolset_name
        tool_limit = resolved.tool_limit
    use_compact_schemas = policy_tier_str in ("nano", "compact")

    # Real ToolRegistry path: load curated flat toolset YAML and filter by permissions
    from src.tools._registry import ToolRegistry as _ToolsRegistry

    if isinstance(reg, _ToolsRegistry):
        try:
            from src.config.toolsets.loader import get_tools_for_toolset

            registered = set(reg.list())
            yaml_names = get_tools_for_toolset(toolset_name) or []
            selected_names: List[str] = [
                n
                for n in yaml_names
                if n in registered
                and _is_tool_permitted_for_context(n, reg, orchestrator, active_role)
                and _is_conditionally_active_tool(n, orchestrator)
            ]

            dynamic_extras: List[str] = []
            # Dynamically expose check_background_task only after a background bash job starts
            if (
                "check_background_task" in registered
                and "check_background_task" not in selected_names
                and _has_active_background_tasks(orchestrator)
                and _is_tool_permitted_for_context(
                    "check_background_task", reg, orchestrator, active_role
                )
            ):
                selected_names.append("check_background_task")
                dynamic_extras.append("check_background_task")

            # Dynamically expose specialist tools from on-demand load_tool_group() calls
            loaded_groups = getattr(orchestrator, "_loaded_tool_groups", None)
            if isinstance(loaded_groups, (set, list, tuple)) and loaded_groups:
                from src.core.context.tool_prompt_sections import is_private_mode_enabled
                from src.tools.tool_group_tools import get_tools_for_group

                for grp in sorted(str(g) for g in loaded_groups):
                    if grp == "memory" and is_private_mode_enabled():
                        continue
                    for g_tool in get_tools_for_group(grp):
                        if (
                            g_tool in registered
                            and g_tool not in selected_names
                            and _is_tool_permitted_for_context(g_tool, reg, orchestrator, active_role)
                        ):
                            selected_names.append(g_tool)
                            dynamic_extras.append(g_tool)

            # Include permitted MCP tools if registered dynamically
            if hasattr(reg, "get") and callable(reg.get):
                for n in registered:
                    if n in selected_names:
                        continue
                    entry = reg.get(n) or {}
                    if entry.get("origin") == "mcp" and _is_tool_permitted_for_context(
                        n, reg, orchestrator, active_role
                    ):
                        selected_names.append(n)

            if selected_names:
                eff_limit = max(1, tool_limit)
                bounded_names = selected_names[:eff_limit]
                for extra in dynamic_extras:
                    if extra not in bounded_names:
                        bounded_names.append(extra)
                from src.core.orchestration.pipeline.permissions import assert_lethal_trifecta_isolated

                assert_lethal_trifecta_isolated(
                    bounded_names,
                    role=active_role,
                    explore_mode=bool(getattr(orchestrator, "explore_mode", False)),
                )
                return reg.filter_by_names(bounded_names).get_openai_functions(
                    compact=use_compact_schemas
                )
            if getattr(orchestrator, "explore_mode", False) or (active_role or "operational") not in ("operational", "general"):
                return []
        except Exception as exc:
            logger.warning("resolve_effective_tool_schemas: flat toolset resolution error: %s", exc)
            if getattr(orchestrator, "explore_mode", False) or (active_role or "operational") not in ("operational", "general"):
                # Fail closed on restricted roles / explore_mode
                return []

    # Fallback path (e.g. SimpleNamespace mock registries in unit tests)
    if hasattr(reg, "get_openai_functions") and callable(reg.get_openai_functions):
        try:
            schemas = list(reg.get_openai_functions() or [])
            return schemas[: max(1, tool_limit)] if schemas else []
        except Exception:
            return []

    return []
