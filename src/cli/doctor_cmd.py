"""``codingagent doctor`` — diagnose local model providers and validate tool schemas.

Provides a comprehensive pre-flight health check for modern local models (Ollama,
LM Studio, vLLM, llama.cpp) and frontier providers:
- Endpoint reachability, protocol version, and network latency.
- Hardware capability profiling (RAM, VRAM, GPU acceleration, and context token capacity).
- Model architecture classification & ModelRuntimePolicy tier resolution (nano, compact, standard, frontier).
- Flat toolset schema validation and server-side native tool acceptance testing.
- Actionable remediation advice for local inference setup.
"""

from __future__ import annotations

import json
import logging
import platform
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.cli._helpers import (
    cli_version,
    git_branch,
    print_json,
    python_version,
    resolve_workdir,
    runtime_os,
)
from src.core.inference.hardware_capability_profile import detect_hardware
from src.core.orchestration.graph.runtime_policy import (
    ModelRuntimePolicy,
    resolve_runtime_policy,
)
from src.config.toolsets.loader import get_tools_for_toolset
from src.tools._registry import build_registry
from src.tools.sandbox import (
    get_sandbox_level,
    sandbox_available,
    sandbox_exec_enforced,
)

logger = logging.getLogger(__name__)

# ANSI color codes for pretty terminal output
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_CYAN = "\033[36m"
_C_BLUE = "\033[34m"
_C_MAGENTA = "\033[35m"


def check_endpoint_reachability(url: str, timeout: float = 2.0) -> Tuple[bool, Optional[float], Optional[str]]:
    """Test HTTP reachability to an endpoint URL and measure round-trip latency in ms."""
    clean_url = (url or "").rstrip("/")
    if not clean_url:
        return False, None, "Empty endpoint URL"

    t0 = time.perf_counter()
    try:
        # Try a quick HEAD or GET to the base URL or root path
        _ = requests.get(clean_url, timeout=timeout, allow_redirects=True)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        # Any status code indicates network reachability
        return True, round(latency_ms, 1), None
    except requests.exceptions.ConnectionError:
        return False, None, "Connection refused (server offline or port closed)"
    except requests.exceptions.Timeout:
        return False, None, f"Connection timed out after {timeout}s"
    except Exception as exc:
        return False, None, str(exc)


def get_tool_schemas_for_tier(toolset_name: str) -> List[Dict[str, Any]]:
    """Build and return OpenAI tool function schemas for a given flat toolset."""
    try:
        reg = build_registry()
        tool_names = get_tools_for_toolset(toolset_name) or []
        sub_reg = reg.filter_by_names(tool_names)
        return sub_reg.get_openai_functions()
    except Exception as exc:
        logger.debug("Failed to build tool schemas for %s: %s", toolset_name, exc)
        return []


def validate_tool_schemas(schemas: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Validate JSON Schema correctness for tool definitions."""
    errors: List[str] = []
    if not schemas:
        return False, ["No tool schemas found for toolset"]

    for idx, s in enumerate(schemas):
        if not isinstance(s, dict):
            errors.append(f"Schema #{idx} is not a dictionary")
            continue
        if s.get("type") != "function":
            errors.append(f"Schema #{idx} type is '{s.get('type')}', expected 'function'")

        fn = s.get("function")
        if not isinstance(fn, dict):
            errors.append(f"Schema #{idx} missing 'function' object")
            continue

        name = fn.get("name")
        if not name or not isinstance(name, str):
            errors.append(f"Schema #{idx} missing or non-string 'name'")

        desc = fn.get("description")
        if not desc or not isinstance(desc, str) or len(desc.strip()) == 0:
            errors.append(f"Tool '{name}' missing descriptive documentation")

        params = fn.get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            errors.append(f"Tool '{name}' parameters must be an object schema")
            continue

        props = params.get("properties")
        if not isinstance(props, dict):
            errors.append(f"Tool '{name}' parameters.properties must be a dictionary")
            continue

        # Check parameter property types
        for p_name, p_def in props.items():
            if not isinstance(p_def, dict):
                errors.append(f"Tool '{name}' property '{p_name}' definition is not an object")
                continue
            has_type = bool(p_def.get("type") or p_def.get("anyOf") or p_def.get("oneOf"))
            if not has_type:
                errors.append(f"Tool '{name}' property '{p_name}' lacks a declared type")

        # Check required fields are in properties
        required = params.get("required", [])
        if isinstance(required, list):
            for r in required:
                if r not in props:
                    errors.append(f"Tool '{name}' required field '{r}' not in properties")

    return len(errors) == 0, errors


def test_server_native_tool_acceptance(
    url: str,
    provider_type: str,
    model: str,
    schemas: List[Dict[str, Any]],
    timeout: float = 8.0,
) -> Tuple[bool, Optional[str]]:
    """Send a minimal test payload with tool schemas to verify native tool compatibility."""
    clean_url = (url or "").rstrip("/")
    p_type = (provider_type or "").lower().replace("-", "_")

    try:
        if p_type == "ollama":
            endpoint = f"{clean_url}/api/chat"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Respond with 'READY'"}],
                "tools": schemas,
                "stream": False,
            }
            resp = requests.post(endpoint, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True, None
            try:
                err_data = resp.json()
                err_msg = err_data.get("error") or resp.text
            except Exception:
                err_msg = resp.text
            return False, f"HTTP {resp.status_code}: {err_msg[:200]}"

        # OpenAI compatible (LM Studio, vLLM, LocalAI)
        endpoint = clean_url if clean_url.endswith("/chat/completions") else f"{clean_url}/chat/completions"
        if not clean_url.endswith("/v1") and "/v1" not in clean_url and not clean_url.endswith("/chat/completions"):
            endpoint = f"{clean_url}/v1/chat/completions"

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Respond with 'READY'"}],
            "tools": schemas,
            "max_tokens": 5,
            "stream": False,
        }
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        if resp.status_code == 200:
            return True, None
        try:
            err_data = resp.json()
            err_msg = err_data.get("error", {}).get("message") or resp.text
        except Exception:
            err_msg = resp.text
        return False, f"HTTP {resp.status_code}: {err_msg[:200]}"

    except requests.exceptions.Timeout:
        return False, f"Server timed out after {timeout}s while validating tools"
    except Exception as exc:
        return False, str(exc)


def diagnose_hardware_profile() -> Dict[str, Any]:
    """Diagnose local system hardware and compute context capacity."""
    hw = detect_hardware()
    ram_gb = getattr(hw, "ram_gb", 0.0) or 0.0
    vram_gb = getattr(hw, "vram_gb", 0.0) or 0.0
    cores = getattr(hw, "cpu_cores", 4)
    threads = getattr(hw, "cpu_threads", cores * 2)
    os_name = getattr(hw, "os", platform.system().lower())

    # Assess recommended max context limits for coding workloads
    effective_vram = max(vram_gb, ram_gb if os_name == "darwin" else 0.0)
    if effective_vram >= 32.0:
        safe_ctx_compact = 65536
        safe_ctx_standard = 32768
    elif effective_vram >= 16.0:
        safe_ctx_compact = 32768
        safe_ctx_standard = 24576
    elif effective_vram >= 8.0:
        safe_ctx_compact = 16384
        safe_ctx_standard = 8192
    else:
        safe_ctx_compact = 8192
        safe_ctx_standard = 4096

    return {
        "os": os_name,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ram_gb": round(ram_gb, 1),
        "vram_gb": round(vram_gb, 1),
        "effective_accelerator_ram_gb": round(effective_vram, 1),
        "cpu_cores": cores,
        "cpu_threads": threads,
        "gpu_offload_supported": getattr(hw, "supports_gpu_offload", True),
        "recommended_context": {
            "compact_9b_14b": safe_ctx_compact,
            "standard_15b_70b": safe_ctx_standard,
            "frontier": 200000,
        },
    }


def diagnose_sandbox_status() -> Dict[str, Any]:
    """Audit the active shell sandbox configuration."""
    level = get_sandbox_level()
    available = sandbox_available()
    enforced = sandbox_exec_enforced()
    return {
        "level": level,
        "available": available,
        "enforced": enforced,
        "healthy": level != "off" and (available or enforced),
    }


def diagnose_ollama_provider(
    provider_cfg: Dict[str, Any],
    target_model: Optional[str] = None,
    validate_tools: bool = False,
    endpoint_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Diagnose an Ollama endpoint."""
    base_url = endpoint_override or provider_cfg.get("base_url") or "http://localhost:11434"
    clean_url = base_url.rstrip("/")

    diag: Dict[str, Any] = {
        "provider": "ollama",
        "type": "ollama",
        "endpoint": clean_url,
        "online": False,
        "latency_ms": None,
        "models_discovered": [],
        "target_model": None,
        "model_architecture": None,
        "policy": None,
        "tool_validation": None,
        "context_audit": None,
        "status": "offline",
        "recommendations": [],
    }

    # Connectivity test
    online, latency_ms, err = check_endpoint_reachability(f"{clean_url}/api/tags")
    if not online:
        # Fallback to root
        online, latency_ms, err = check_endpoint_reachability(clean_url)

    if not online:
        diag["status"] = "offline"
        diag["error"] = err
        diag["recommendations"].append(
            "Ollama server is not running. Start it with: `ollama serve`"
        )
        diag["recommendations"].append(
            "To pull a recommended 2026 compact model: `ollama pull qwen3.5:9b` or `ollama pull devstral`"
        )
        return diag

    diag["online"] = True
    diag["latency_ms"] = latency_ms

    # Query tags / available models
    models_found: List[str] = []
    try:
        resp = requests.get(f"{clean_url}/api/tags", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                name = m.get("name") or m.get("model")
                if name:
                    models_found.append(name)
    except Exception as exc:
        logger.debug("Failed to query Ollama tags: %s", exc)

    diag["models_discovered"] = models_found

    # Resolve target model
    cfg_models = provider_cfg.get("models") or []
    default_cfg_model = cfg_models[0] if cfg_models else None
    if isinstance(default_cfg_model, dict):
        default_cfg_model = default_cfg_model.get("id") or default_cfg_model.get("name")

    chosen_model = target_model or default_cfg_model or (models_found[0] if models_found else "qwen3.5:9b")
    diag["target_model"] = chosen_model

    # Check model architecture via /api/show
    if chosen_model in models_found or target_model:
        try:
            show_resp = requests.post(f"{clean_url}/api/show", json={"name": chosen_model}, timeout=3.0)
            if show_resp.status_code == 200:
                show_data = show_resp.json()
                details = show_data.get("details", {})
                param_size = details.get("parameter_size") or ""
                family = details.get("family") or ""
                quant = details.get("quantization_level") or ""
                diag["model_architecture"] = {
                    "parameter_size": param_size,
                    "family": family,
                    "quantization": quant,
                    "format": details.get("format", "gguf"),
                }
        except Exception:
            pass

    # Resolve ModelRuntimePolicy
    policy: ModelRuntimePolicy = resolve_runtime_policy(model=chosen_model, provider="ollama")
    diag["policy"] = {
        "tier": policy.policy_tier,
        "toolset": policy.toolset_name,
        "tool_limit": policy.tool_limit,
        "turn_limit": policy.turn_limit,
        "recovery_limit": policy.recovery_limit,
        "verification_mode": policy.verification_mode,
        "parallel_reads": policy.max_parallel_reads,
        "context_window": policy.context_window,
        "thinking_enabled": policy.thinking_enabled,
    }

    # Validate tool schemas
    schemas = get_tool_schemas_for_tier(policy.toolset_name)
    schemas_valid, schema_errors = validate_tool_schemas(schemas)
    diag["tool_validation"] = {
        "schemas_count": len(schemas),
        "schemas_valid": schemas_valid,
        "schema_errors": schema_errors,
        "server_acceptance_tested": False,
        "server_acceptance_ok": None,
    }

    if validate_tools and schemas_valid:
        acc_ok, acc_err = test_server_native_tool_acceptance(
            url=clean_url,
            provider_type="ollama",
            model=chosen_model,
            schemas=schemas,
        )
        diag["tool_validation"]["server_acceptance_tested"] = True
        diag["tool_validation"]["server_acceptance_ok"] = acc_ok
        diag["tool_validation"]["server_acceptance_error"] = acc_err
        if not acc_ok:
            diag["recommendations"].append(
                f"Ollama rejected native tools for '{chosen_model}'. Ensure Ollama >= 0.3.0 is installed."
            )

    # Context window audit
    ctx = policy.context_window
    if ctx >= 32768:
        ctx_verdict = "healthy"
    elif ctx >= 16384:
        ctx_verdict = "adequate"
    else:
        ctx_verdict = "warning"
        diag["recommendations"].append(
            f"Context window ({ctx} tokens) is constrained for complex edits. Increase OLLAMA_NUM_CTX or set in Modelfile."
        )

    diag["context_audit"] = {
        "context_tokens": ctx,
        "verdict": ctx_verdict,
    }

    # Final status determination
    if not schemas_valid or (diag["tool_validation"]["server_acceptance_ok"] is False):
        diag["status"] = "error"
    elif ctx_verdict == "warning" or not models_found:
        diag["status"] = "warning"
        if not models_found:
            diag["recommendations"].append(
                f"No models discovered on Ollama. Run: `ollama pull {chosen_model}`"
            )
    else:
        diag["status"] = "healthy"

    return diag


def diagnose_openai_compat_provider(
    provider_name: str,
    provider_cfg: Dict[str, Any],
    target_model: Optional[str] = None,
    validate_tools: bool = False,
    endpoint_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Diagnose an OpenAI-compatible endpoint (LM Studio, vLLM, LocalAI)."""
    base_url = endpoint_override or provider_cfg.get("base_url") or "http://localhost:1234/v1"
    clean_url = base_url.rstrip("/")

    diag: Dict[str, Any] = {
        "provider": provider_name,
        "type": "openai_compat",
        "endpoint": clean_url,
        "online": False,
        "latency_ms": None,
        "models_discovered": [],
        "target_model": None,
        "model_architecture": None,
        "policy": None,
        "tool_validation": None,
        "context_audit": None,
        "status": "offline",
        "recommendations": [],
    }

    # Try models endpoint
    models_url = f"{clean_url}/models" if not clean_url.endswith("/models") else clean_url
    online, latency_ms, err = check_endpoint_reachability(models_url)
    if not online and clean_url.endswith("/v1"):
        # Try parent URL
        online, latency_ms, err = check_endpoint_reachability(clean_url[:-3])

    if not online:
        diag["status"] = "offline"
        diag["error"] = err
        if provider_name == "lm_studio":
            diag["recommendations"].append(
                "LM Studio local server is offline. Open LM Studio -> Developer Tab -> Start Server on port 1234."
            )
            diag["recommendations"].append(
                "Ensure a 9B–14B model (e.g. Qwen 3.5 9B, Devstral) is loaded in LM Studio with 32K context."
            )
        elif "vllm" in provider_name.lower():
            diag["recommendations"].append(
                "vLLM server is offline. Start with: `vllm serve <model> --port 8000 --enable-auto-tool-choice`"
            )
        else:
            diag["recommendations"].append(
                f"Local server at {clean_url} is not reachable. Verify the server is running."
            )
        return diag

    diag["online"] = True
    diag["latency_ms"] = latency_ms

    # Query available models
    models_found: List[str] = []
    try:
        resp = requests.get(models_url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            model_items = data.get("data") if isinstance(data, dict) else data
            if isinstance(model_items, list):
                for item in model_items:
                    m_id = item.get("id") if isinstance(item, dict) else item
                    if m_id:
                        models_found.append(str(m_id))
    except Exception as exc:
        logger.debug("Failed to query models from %s: %s", models_url, exc)

    diag["models_discovered"] = models_found

    # Select target model
    cfg_models = provider_cfg.get("models") or []
    default_cfg_model = cfg_models[0] if cfg_models else None
    if isinstance(default_cfg_model, dict):
        default_cfg_model = default_cfg_model.get("id") or default_cfg_model.get("name")

    chosen_model = target_model or default_cfg_model or (models_found[0] if models_found else "qwen/qwen3.5-9b")
    diag["target_model"] = chosen_model

    # Resolve ModelRuntimePolicy
    policy: ModelRuntimePolicy = resolve_runtime_policy(model=chosen_model, provider=provider_name)
    diag["policy"] = {
        "tier": policy.policy_tier,
        "toolset": policy.toolset_name,
        "tool_limit": policy.tool_limit,
        "turn_limit": policy.turn_limit,
        "recovery_limit": policy.recovery_limit,
        "verification_mode": policy.verification_mode,
        "parallel_reads": policy.max_parallel_reads,
        "context_window": policy.context_window,
        "thinking_enabled": policy.thinking_enabled,
    }

    # Validate tool schemas
    schemas = get_tool_schemas_for_tier(policy.toolset_name)
    schemas_valid, schema_errors = validate_tool_schemas(schemas)
    diag["tool_validation"] = {
        "schemas_count": len(schemas),
        "schemas_valid": schemas_valid,
        "schema_errors": schema_errors,
        "server_acceptance_tested": False,
        "server_acceptance_ok": None,
    }

    if validate_tools and schemas_valid:
        acc_ok, acc_err = test_server_native_tool_acceptance(
            url=clean_url,
            provider_type=provider_name,
            model=chosen_model,
            schemas=schemas,
        )
        diag["tool_validation"]["server_acceptance_tested"] = True
        diag["tool_validation"]["server_acceptance_ok"] = acc_ok
        diag["tool_validation"]["server_acceptance_error"] = acc_err
        if not acc_ok:
            diag["recommendations"].append(
                f"Server rejected native tools for '{chosen_model}'. Reason: {acc_err}"
            )

    # Context window audit
    ctx = policy.context_window
    if ctx >= 32768:
        ctx_verdict = "healthy"
    elif ctx >= 16384:
        ctx_verdict = "adequate"
    else:
        ctx_verdict = "warning"
        diag["recommendations"].append(
            f"Context window ({ctx} tokens) is constrained. For LM Studio, set 'Context Overflow' to at least 32768."
        )

    diag["context_audit"] = {
        "context_tokens": ctx,
        "verdict": ctx_verdict,
    }

    if not schemas_valid or (diag["tool_validation"]["server_acceptance_ok"] is False):
        diag["status"] = "error"
    elif ctx_verdict == "warning" or not models_found:
        diag["status"] = "warning"
        if not models_found:
            diag["recommendations"].append(
                "No loaded model found in local server. Load a model before running agent tasks."
            )
    else:
        diag["status"] = "healthy"

    return diag


def diagnose_cloud_provider(
    provider_name: str,
    provider_cfg: Dict[str, Any],
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Diagnose configuration and policy for cloud/frontier providers."""
    cfg_models = provider_cfg.get("models") or []
    first_model = cfg_models[0] if cfg_models else "claude-sonnet-4.6"
    if isinstance(first_model, dict):
        first_model = first_model.get("id") or first_model.get("name")
    chosen_model = target_model or first_model

    policy: ModelRuntimePolicy = resolve_runtime_policy(model=chosen_model, provider=provider_name)
    schemas = get_tool_schemas_for_tier(policy.toolset_name)
    schemas_valid, schema_errors = validate_tool_schemas(schemas)

    return {
        "provider": provider_name,
        "type": "cloud",
        "target_model": chosen_model,
        "policy": {
            "tier": policy.policy_tier,
            "toolset": policy.toolset_name,
            "tool_limit": policy.tool_limit,
            "turn_limit": policy.turn_limit,
            "verification_mode": policy.verification_mode,
            "parallel_reads": policy.max_parallel_reads,
            "context_window": policy.context_window,
        },
        "tool_validation": {
            "schemas_count": len(schemas),
            "schemas_valid": schemas_valid,
            "schema_errors": schema_errors,
        },
        "status": "configured" if schemas_valid else "error",
        "recommendations": [],
    }


def load_providers_config() -> List[Dict[str, Any]]:
    """Load the declared provider configurations."""
    try:
        import src.core.inference.provider_config as _pc

        cfg_path = _pc.resolve_providers_config_path(None, _pc.__file__)
        if cfg_path and cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                return [raw]
    except Exception as exc:
        logger.debug("Failed to load providers config: %s", exc)
    return []


def run_doctor(args: Any) -> int:
    """Execute the full doctor diagnostic audit and output findings."""
    workdir = resolve_workdir(getattr(args, "workdir", None))
    provider_filter = getattr(args, "provider", None)
    model_filter = getattr(args, "model", None)
    endpoint_override = getattr(args, "endpoint", None)
    validate_tools = getattr(args, "validate_tools", False)
    as_json = getattr(args, "json", False)

    # 1. System & Hardware Diagnosis
    sys_diag = {
        "version": cli_version(),
        "python": python_version(),
        "os": runtime_os(),
        "workdir": workdir,
        "git_branch": git_branch(workdir),
    }
    hw_diag = diagnose_hardware_profile()
    sb_diag = diagnose_sandbox_status()

    # 2. Providers Diagnosis
    all_configs = load_providers_config()
    target_configs: List[Dict[str, Any]] = []

    if provider_filter:
        p_filt_norm = provider_filter.lower().replace("-", "_")
        for p in all_configs:
            name = str(p.get("name") or p.get("type") or "").lower().replace("-", "_")
            if name == p_filt_norm or p_filt_norm in name:
                target_configs.append(p)
        if not target_configs:
            # Synthetic provider config if user provided custom flags
            target_configs.append({
                "name": provider_filter,
                "type": provider_filter,
                "base_url": endpoint_override,
            })
    else:
        # Default: check all local providers first, plus active provider
        target_configs = all_configs

    provider_results: List[Dict[str, Any]] = []
    all_recommendations: List[str] = []

    for p_cfg in target_configs:
        name = str(p_cfg.get("name") or p_cfg.get("type") or "unknown").lower()
        p_type = str(p_cfg.get("type") or name).lower()

        if p_type == "ollama" or "ollama" in name:
            res = diagnose_ollama_provider(
                provider_cfg=p_cfg,
                target_model=model_filter,
                validate_tools=validate_tools,
                endpoint_override=endpoint_override,
            )
        elif p_type in ("lm_studio", "openai_compat", "vllm", "local") or "lm_studio" in name:
            res = diagnose_openai_compat_provider(
                provider_name=name,
                provider_cfg=p_cfg,
                target_model=model_filter,
                validate_tools=validate_tools,
                endpoint_override=endpoint_override,
            )
        else:
            res = diagnose_cloud_provider(
                provider_name=name,
                provider_cfg=p_cfg,
                target_model=model_filter,
            )

        provider_results.append(res)
        if res.get("recommendations"):
            all_recommendations.extend(res["recommendations"])

    # Compute overall health score
    online_count = sum(1 for p in provider_results if p.get("online") or p.get("status") == "configured")
    error_count = sum(1 for p in provider_results if p.get("status") == "error")

    overall_status = "healthy"
    if error_count > 0:
        overall_status = "error"
    elif online_count == 0 and len(provider_results) > 0:
        overall_status = "offline"
    elif any(p.get("status") == "warning" for p in provider_results):
        overall_status = "warning"

    payload: Dict[str, Any] = {
        "status": overall_status,
        "system": sys_diag,
        "hardware": hw_diag,
        "sandbox": sb_diag,
        "providers": provider_results,
        "recommendations": all_recommendations,
    }

    if as_json:
        print_json(payload)
        return 0 if overall_status in ("healthy", "warning") else 1

    # Pretty Terminal Report
    print(f"\n{_C_BOLD}{_C_CYAN}CodingAgent Doctor — Local Model & Capability Health Audit{_C_RESET}")
    print(f"{_C_DIM}Version {sys_diag['version']} | Python {sys_diag['python']} | {sys_diag['os']} ({hw_diag['machine']}){_C_RESET}\n")

    # Section 1: Hardware & Context Capacity
    print(f"{_C_BOLD}[1] System Hardware & Memory Capacity{_C_RESET}")
    mem_info = f"RAM: {hw_diag['ram_gb']} GB"
    if hw_diag["vram_gb"] > 0:
        mem_info += f" | VRAM: {hw_diag['vram_gb']} GB"
    print(f"  • Hardware:     {mem_info} | {hw_diag['cpu_cores']} cores ({hw_diag['cpu_threads']} threads)")
    rec = hw_diag["recommended_context"]
    print(f"  • Context Cap:  Compact 9–14B: {_C_GREEN}{rec['compact_9b_14b']:,} tokens{_C_RESET} | Standard 15–70B: {rec['standard_15b_70b']:,} tokens")

    # Section 2: Sandbox Security
    print(f"\n{_C_BOLD}[2] Execution Sandbox Security{_C_RESET}")
    sb_badge = f"{_C_GREEN}✓ Enforced{_C_RESET}" if sb_diag["enforced"] else (
        f"{_C_GREEN}✓ Available{_C_RESET}" if sb_diag["available"] else f"{_C_YELLOW}! Unenforced{_C_RESET}"
    )
    print(f"  • Strictness:   Level: '{sb_diag['level']}' ({sb_badge})")
    print(f"  • Workdir:      {sys_diag['workdir']} (Git: {sys_diag['git_branch'] or 'none'})")

    # Section 3: Model Providers & Flat Toolsets
    print(f"\n{_C_BOLD}[3] Model Providers & 2026 Flat Toolsets{_C_RESET}")
    for p in provider_results:
        p_name = p.get("provider", "unknown")
        p_stat = p.get("status", "unknown")

        if p_stat in ("healthy", "configured"):
            status_tag = f"{_C_GREEN}[ONLINE]{_C_RESET}"
        elif p_stat == "warning":
            status_tag = f"{_C_YELLOW}[WARNING]{_C_RESET}"
        elif p_stat == "offline":
            status_tag = f"{_C_YELLOW}[OFFLINE]{_C_RESET}"
        else:
            status_tag = f"{_C_RED}[ERROR]{_C_RESET}"

        endpoint_str = f" @ {p.get('endpoint')}" if p.get("endpoint") else ""
        lat_str = f" ({p.get('latency_ms')} ms)" if p.get("latency_ms") is not None else ""
        print(f"  • {_C_BOLD}{p_name.upper()}{_C_RESET} {status_tag}{endpoint_str}{lat_str}")

        if p.get("target_model"):
            pol = p.get("policy") or {}
            tier = pol.get("tier", "unknown")
            toolset = pol.get("toolset", "unknown")
            t_count = pol.get("tool_limit", 0)
            verify_m = pol.get("verification_mode", "targeted")
            p_reads = pol.get("parallel_reads", 2)
            ctx = pol.get("context_window", 32768)

            print(f"    - Target Model:    {_C_CYAN}{p['target_model']}{_C_RESET}")
            print(f"    - Runtime Policy:  Tier: {_C_BOLD}{tier}{_C_RESET} | Flat Toolset: {_C_BOLD}{toolset}{_C_RESET} ({t_count} tools)")
            print(f"    - Execution Guard: Verify Mode: '{verify_m}' (SHA-256 deduplicated) | Parallel Reads: {p_reads} waves")
            print(f"    - Context Budget:  {ctx:,} tokens | Thinking: {'Enabled' if pol.get('thinking_enabled') else 'Standard'}")

            # Tool schema validation status
            t_val = p.get("tool_validation") or {}
            if t_val.get("schemas_valid"):
                tool_badge = f"{_C_GREEN}✓ All {t_val.get('schemas_count', 0)} tool schemas valid{_C_RESET}"
            else:
                tool_badge = f"{_C_RED}✗ Schema errors: {t_val.get('schema_errors')}{_C_RESET}"
            print(f"    - Schema Surface:  {tool_badge}")

            # Acceptance testing if executed
            if t_val.get("server_acceptance_tested"):
                if t_val.get("server_acceptance_ok"):
                    print(f"    - Tool Probe:      {_C_GREEN}✓ Server accepted native tool schema ping{_C_RESET}")
                else:
                    print(f"    - Tool Probe:      {_C_RED}✗ Server rejected tools: {t_val.get('server_acceptance_error')}{_C_RESET}")

        if p.get("error"):
            print(f"    - Probe Note:      {_C_DIM}{p['error']}{_C_RESET}")

    # Section 4: Recommendations
    if all_recommendations:
        print(f"\n{_C_BOLD}[4] Actionable Recommendations{_C_RESET}")
        for rec_item in set(all_recommendations):
            print(f"  {_C_YELLOW}→{_C_RESET} {rec_item}")
    else:
        print(f"\n{_C_BOLD}[4] System Verdict{_C_RESET}")
        print(f"  {_C_GREEN}✓ All configured providers and tool schemas are ready for local coding agent tasks.{_C_RESET}")

    print()
    # Exit with code 1 if explicitly filtered provider was offline, otherwise 0
    if provider_filter and online_count == 0:
        return 1
    return 0 if overall_status in ("healthy", "warning") else 1
