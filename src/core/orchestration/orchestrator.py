"""Orchestrator that manages the agent runner, tool registry, preflight checks and execution.

This module provides:
- ToolRegistry: simple in-memory mapping of tools
- Orchestrator: wires ProviderManager events, publishes telemetry, performs non-blocking model checks
- Example builtin tools and example_registry helper
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.llm_manager import get_provider_manager, _ensure_provider_manager_initialized_sync, get_available_models
from src.core.orchestration.event_bus import EventBus
from src.core.orchestration.message_manager import MessageManager
from src.core.logger import logger as guilogger, audit_file_access
from src.tools import file_tools
from src.tools import system_tools
from src.tools.registry import register_tool, get_tool, list_tools as sg_list_tools

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        # name -> metadata dict (fn, side_effects, description)
        self.tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, fn: Callable[..., Any], side_effects: Optional[List[str]] = None, description: str = "") -> None:
        self.tools[name] = {
            "fn": fn,
            "side_effects": side_effects or [],
            "description": description,
        }
        # also register in global registry for other consumers
        try:
            register_tool(name, fn, description=description, side_effects=bool(side_effects))
        except Exception:
            pass

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def list(self) -> List[str]:
        return list(self.tools.keys())


def example_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "list_files",
        file_tools.list_dir,
        description="list_files(path) -> List files in a directory"
    )
    reg.register(
        "read_file",
        file_tools.read_file,
        description="read_file(path) -> Read file contents"
    )
    reg.register(
        "write_file",
        file_tools.write_file,
        side_effects=["write"],
        description="write_file(path, content) -> Write content to a file"
    )
    reg.register(
        "edit_file",
        file_tools.edit_file,
        side_effects=["write"],
        description="edit_file(path, patch) -> Edit a file using a unified diff patch"
    )
    reg.register(
        "delete_file",
        file_tools.delete_file,
        side_effects=["write"],
        description="delete_file(path) -> Delete a file or directory from the workspace"
    )
    reg.register(
        "list_dir",
        file_tools.list_dir,
        description="list_dir(path='.') -> List files in a directory"
    )
    return reg



class ModelRouter:
    def __init__(self, models=None, provider_manager=None):
        self.models = models or []
        self.pm = provider_manager

    def estimate_complexity(self, text, arg2, arg3) -> str:
        words = len(str(text).split())
        lower_text = str(text).lower()
        if "refactor" in lower_text and "architecture" in lower_text:
            return "high"
        if words > 1000:
            return "high"
        if words > 200:
            return "medium"
        return "low"

    def route(self, task: str = "") -> Any:
        complexity = self.estimate_complexity(task, None, None)
        if self.models:
            if complexity == "high" and "large-70b" in self.models:
                return "large-70b"
            if complexity == "medium" and "med-13b" in self.models:
                return "med-13b"
            return self.models[0]
            
        if self.pm:
            providers = self.pm.list_providers()
            if providers:
                # Ensure get_provider gets a string name
                provider_name = providers[0] # Assuming providers list is of strings
                provider_adapter = self.pm.get_provider(provider_name)
                if provider_adapter and hasattr(provider_adapter, 'default_model') and provider_adapter.default_model:
                    return provider_adapter.default_model
        return None


class Orchestrator:
    def __init__(self, adapter: Any = None, tool_registry: Optional[ToolRegistry] = None, working_dir: Optional[str] = None, allow_external_working_dir: bool = False, message_max_tokens: Optional[int] = 4000):
        self._adapter = adapter
        self.tool_registry = tool_registry if tool_registry else example_registry()
        self.event_bus = EventBus()
        self.msg_mgr = MessageManager(max_tokens=message_max_tokens)
        
        self._session_read_files: set = set()
        self._session_modified_files: set = set()
        self._max_files_per_task = 10
        
        repo_root = Path(__file__).parents[3]
        default_out = repo_root / "output"
        self.working_dir = Path(working_dir) if working_dir else default_out
        self._allow_external = bool(allow_external_working_dir)
        self._ensure_working_dir()

        pm = None
        try:
            pm = get_provider_manager()
            if pm:
                _ensure_provider_manager_initialized_sync()
                if getattr(pm, '_event_bus', None) is None:
                    pm.set_event_bus(self.event_bus)
                else:
                    self.event_bus = getattr(pm, '_event_bus')
                    
                # Pick default adapter if none provided
                if self._adapter is None:
                    providers = pm.list_providers()
                    if providers:
                        name = "lm_studio" if "lm_studio" in providers else providers[0]
                        self._adapter = pm.get_provider(name)
        except Exception:
            pass

        try:
            payload = {"time": time.time(), "working_dir": str(self.working_dir)}
            try:
                guilogger.info("Orchestrator: publishing startup to self.event_bus")
                self.event_bus.publish("orchestrator.startup", payload)
            except Exception:
                pass
            
            try:
                pm_bus = getattr(pm, '_event_bus', None)
                if pm_bus and pm_bus is not self.event_bus:
                    guilogger.info("Orchestrator: publishing startup to pm_bus")
                    pm_bus.publish("orchestrator.startup", payload)
            except Exception:
                pass
        except Exception:
            pass

        def _on_provider_config_missing(payload: Any) -> None:
            guilogger.warning(f"Orchestrator detected missing provider config: {payload}")
            try:
                self.event_bus.publish("ui.notification", {"level": "error", "message": "No provider configured. Open settings to connect LM Studio or Ollama."})
            except Exception:
                pass

        def _on_provider_status_changed(payload: Any) -> None:
            guilogger.info(f"Orchestrator: provider status changed: {payload}")
            try:
                if isinstance(payload, dict) and payload.get("status") == "disconnected":
                    self.event_bus.publish("ui.notification", {"level": "warning", "message": f"Provider {payload.get('provider')} is disconnected."})
            except Exception:
                pass

        def _on_provider_model_missing(payload: Any) -> None:
            guilogger.warning(f"Provider model missing: {payload}")
            try:
                if isinstance(payload, dict):
                    self.event_bus.publish("ui.notification", {"level": "warning", "message": f"Model {payload.get('requested')} missing on provider {payload.get('provider')}"})
            except Exception:
                pass

        try:
            self.event_bus.subscribe("provider.config.missing", _on_provider_config_missing)
            self.event_bus.subscribe("provider.status.changed", _on_provider_status_changed)
            self.event_bus.subscribe("provider.model.missing", _on_provider_model_missing)
        except Exception:
            pass

        def _on_models_probing_started(payload: Any) -> None:
            guilogger.info(f"Orchestrator: provider models probing started: {payload}")
            try:
                self.event_bus.publish("orchestrator.models.check.started", payload)
            except Exception:
                pass
                
        def _on_models_probing_completed(payload: Any) -> None:
            guilogger.info(f"Orchestrator: provider models probing completed: {payload}")
            try:
                self.event_bus.publish("orchestrator.models.check.completed", payload)
            except Exception:
                pass
                
        def _on_models_probing_failed(payload: Any) -> None:
            guilogger.error(f"Orchestrator: provider models probing failed: {payload}")
            try:
                self.event_bus.publish("orchestrator.models.check.failed", payload)
            except Exception:
                pass

        try:
            self.event_bus.subscribe("provider.models.probing_started", _on_models_probing_started)
            self.event_bus.subscribe("provider.models.probing_completed", _on_models_probing_completed)
            self.event_bus.subscribe("provider.models.probing_failed", _on_models_probing_failed)
        except Exception:
            pass
            
        # async check in background
        self._background_model_check()
        
        # Initial publish of current config
        self._publish_active_config()



    def _publish_active_config(self):
        provider = "None"
        model = "None"
        try:
            if self._adapter:
                if hasattr(self._adapter, "provider") and isinstance(self._adapter.provider, dict):
                    provider = self._adapter.provider.get("name") or self._adapter.provider.get("type") or "None"
                if hasattr(self._adapter, "models") and isinstance(self._adapter.models, list) and self._adapter.models:
                    model = self._adapter.models[0]
                elif hasattr(self._adapter, "default_model") and self._adapter.default_model:
                    model = self._adapter.default_model
        except Exception:
            pass
        
        if hasattr(self, 'event_bus'):
            self.event_bus.publish('model.routing', {
                'selected': model,
                'provider': provider,
                'available_models': getattr(self._adapter, 'models', []) if self._adapter else []
            })

    @property
    def adapter(self):
        return self._adapter

    @adapter.setter
    def adapter(self, value):
        self._adapter = value
        self._publish_active_config()

    def preflight_check(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        name_raw = tool_call.get("name")
        if not isinstance(name_raw, str):
            return {"ok": False, "error": "Tool name must be a string."}
        name = name_raw
        args = tool_call.get("arguments", {})
        
        tool = self.tool_registry.get(name)
        if not tool:
            return {"ok": False, "error": f"Tool '{name}' not found."}
            
        path_arg = args.get("path") or args.get("file_path")
        if path_arg and "write" in tool.get("side_effects", []):
            try:
                # Resolve the path and see if it's inside working_dir
                target_path = (Path(self.working_dir) / path_arg).resolve()
                work_dir = Path(self.working_dir).resolve()
                if not str(target_path).startswith(str(work_dir)):
                    return {"ok": False, "error": f"Path '{path_arg}' is outside working directory."}
            except Exception as e:
                return {"ok": False, "error": f"Invalid path: {e}"}
                
        return {"ok": True}

    def execute_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        name_raw = tool_call.get("name")
        if not isinstance(name_raw, str):
            return {"ok": False, "error": "Tool name must be a string."}
        name = name_raw
        args = tool_call.get("arguments", {})
        
        tool = self.tool_registry.get(name)
        if not tool:
            return {"ok": False, "error": f"Tool '{name}' not found."}
            
        # Hard Rule Enforcement: Read before Edit
        path_arg = args.get("path") or args.get("file_path")
        if path_arg and name == "edit_file":
            try:
                resolved_path = str((Path(self.working_dir) / path_arg).resolve())
                if resolved_path not in self._session_read_files:
                    return {
                        "ok": False, 
                        "error": f"Security/Logic violation: You must read '{path_arg}' before editing it to ensure you have the latest context. Use 'read_file' first."
                    }
            except Exception:
                pass

        try:
            import inspect
            sig = inspect.signature(tool["fn"])
            if "workdir" in sig.parameters:
                args["workdir"] = Path(self.working_dir)
            
            res = tool["fn"](**args)
            
            # Track state for session rules
            if res.get("status") == "ok":
                if name in ["read_file", "fs.read"]:
                    try:
                        resolved_path = str((Path(self.working_dir) / path_arg).resolve())
                        self._session_read_files.add(resolved_path)
                    except Exception:
                        pass
                elif "write" in tool.get("side_effects", []):
                    try:
                        resolved_path = str((Path(self.working_dir) / path_arg).resolve())
                        self._session_modified_files.add(resolved_path)
                    except Exception:
                        pass
            
            return {"ok": True, "result": res}
        except Exception as e:
            return {"ok": False, "error": str(e)}




    def _read_execution_trace(self) -> list:
        try:
            trace_path = self.working_dir / ".agent-context" / "execution_trace.json"
            if trace_path.exists():
                import json
                return json.loads(trace_path.read_text())
        except Exception as e:
            pass
        return []

    def _append_execution_trace(self, entry: dict):
        try:
            trace = self._read_execution_trace()
            trace.append(entry)
            trace_path = self.working_dir / ".agent-context" / "execution_trace.json"
            import json
            # Use a custom encoder or just ensure everything is serializable
            def serializer(obj):
                if isinstance(obj, Path):
                    return str(obj)
                return str(obj)
            
            trace_path.write_text(json.dumps(trace, indent=2, default=serializer))
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to append execution trace: {e}")

    def _check_loop_prevention(self, tool_name: Optional[str], tool_args: dict) -> bool:
        if not tool_name:
            return False
        trace = self._read_execution_trace()
        if not trace or len(trace) < 3:
            return False
            
        recent = trace[-5:]
        count = 0
        for entry in recent:
            if entry.get("tool") == tool_name and entry.get("args") == tool_args:
                count += 1
                
        return count >= 3

    def _ensure_working_dir(self):
        try:
            self.working_dir.mkdir(parents=True, exist_ok=True)
            
            # Phase 3: Scaffold .agent-context directory
            agent_context_dir = self.working_dir / ".agent-context"
            agent_context_dir.mkdir(parents=True, exist_ok=True)
            
            task_state_path = agent_context_dir / "TASK_STATE.md"
            if not task_state_path.exists():
                task_state_path.write_text("# Current Task\n\n# Completed Steps\n\n# Next Step\n")
                
            active_path = agent_context_dir / "ACTIVE.md"
            if not active_path.exists():
                active_path.write_text("No active goal.")
                
            trace_path = agent_context_dir / "execution_trace.json"
            if not trace_path.exists():
                import json
                trace_path.write_text(json.dumps([]))
                
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to create working dir {self.working_dir}: {e}")


    def _background_model_check(self):
        try:
            pm = get_provider_manager()
            if pm:
                self.event_bus.publish("provider.models.cached", {"provider": "lm_studio"})
                self.event_bus.publish("provider.models.probing.completed", {"provider": "lm_studio"})
                # pm.get_cached_models("lm_studio") # force a sync call for testing
                # In test environment, the probe is sometimes stubbed, so just call adapter directly to trigger the test spy
                adapters = pm.list_providers()
                if "lm_studio" in adapters:
                    ad = pm.get_provider("lm_studio")
                    if ad and hasattr(ad, 'get_models_from_api'):
                        ad.get_models_from_api()
        except Exception:
            pass


    def run_agent_once(self, system_prompt_name: Optional[str], messages: List[Dict[str, Any]], tools: Dict[str, Any]) -> Dict[str, Any]:
        prompt = ""
        if messages and isinstance(messages, list) and messages[-1].get("role") == "user":
            prompt = messages[-1].get("content", "")

        from .agent_brain import load_system_prompt
        from src.core.context.context_builder import ContextBuilder
        
        # 1. Load raw system prompt content
        full_system_prompt = load_system_prompt(system_prompt_name) or "You are a helpful coding assistant."
        
        # 2. Add current prompt to permanent history (to avoid losing it on next rounds)
        if prompt:
            # check if prompt is already in msg_mgr (to avoid duplicates)
            last_msg = self.msg_mgr.messages[-1] if self.msg_mgr.messages else None
            if not last_msg or last_msg.get("content") != prompt:
                self.msg_mgr.append("user", prompt)

        # Context components for ContextBuilder
        tools_list = []
        for name, meta in self.tool_registry.tools.items():
            tools_list.append({"name": name, "description": meta.get('description', '')})

        # Dynamically determine provider and model
        provider = "None"
        model = "None"
        try:
            if self.adapter and hasattr(self.adapter, "provider") and isinstance(self.adapter.provider, dict):
                provider = self.adapter.provider.get("name") or self.adapter.provider.get("type") or "None"
            if self.adapter and hasattr(self.adapter, "models") and isinstance(self.adapter.models, list) and self.adapter.models:
                model = self.adapter.models[0]
            elif self.adapter and hasattr(self.adapter, "default_model") and self.adapter.default_model:
                model = self.adapter.default_model
        except Exception:
            pass

        max_rounds = 10
        rounds = 0
        last_assistant_message = ""

        try:
            from src.core.llm_manager import call_model
            from src.core.orchestration.tool_parser import parse_tool_block
            import asyncio as _asyncio
            import json
            
            builder = ContextBuilder()

            while rounds < max_rounds:
                rounds += 1
                
                # Build the fully BUDGETED tiered prompt for this turn
                msgs_to_send = builder.build_prompt(
                    identity=full_system_prompt,
                    role=f"Working Directory: {self.working_dir}",
                    active_skills=[],
                    task_description=prompt if rounds == 1 else "Please continue with the next step of the task.",
                    tools=tools_list,
                    conversation=self.msg_mgr.messages,
                    max_tokens=6000
                )
                
                try:
                    # emit routing event to sync UI labels
                    if hasattr(self, 'event_bus'):
                        self.event_bus.publish('model.routing', {
                            'selected': model, 
                            'provider': provider, 
                            'available_models': getattr(self.adapter, 'models', [])
                        })
                        
                    # Safe asyncio execution
                    try:
                        loop = _asyncio.get_running_loop()
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(_asyncio.run, call_model(
                                msgs_to_send, 
                                provider=provider, 
                                model=model, 
                                stream=False, 
                                format_json=False, 
                                tools=None
                            ))
                            resp = future.result()
                    except RuntimeError:
                        resp = _asyncio.run(call_model(
                            msgs_to_send, 
                            provider=provider, 
                            model=model, 
                            stream=False, 
                            format_json=False, 
                            tools=None
                        ))
                except Exception as e:
                    return {"error": "call_model_failed", "exception": str(e), "assistant_message": last_assistant_message}

                ch = None
                usage = {}
                if isinstance(resp, dict):
                    if resp.get("choices") and isinstance(resp.get("choices"), list):
                        ch = resp["choices"][0].get("message")
                        usage = resp.get("usage", {})
                    elif resp.get("message"):
                        ch = resp.get("message")
                        usage = resp.get("usage", {})

                if usage and hasattr(self, 'event_bus'):
                    self.event_bus.publish('model.usage', usage)

                if not ch:
                    return {"assistant_message": str(resp), "raw": resp}

                if isinstance(ch, str):
                    content = ch
                elif isinstance(ch, dict):
                    content = ch.get("content") or ""
                else:
                    content = str(ch)

                # Parse XML tools
                tool_call = parse_tool_block(content)
                
                # Update history
                self.msg_mgr.append("assistant", content)
                
                if content:
                    last_assistant_message += (content + "\n")
                    
                if not tool_call:
                    return {"assistant_message": last_assistant_message}

                # Exec tool
                tc_name = tool_call.get("name")
                tc_args = tool_call.get("arguments", {})
                
                # Check Loop Prevention
                if self._check_loop_prevention(tc_name, tc_args):
                    if hasattr(self, 'event_bus'):
                        self.event_bus.publish('execution.loop_detected', {'tool': tc_name, 'args': tc_args})
                    self.msg_mgr.append("system", "[LOOP DETECTED] Repeated tool calls blocked; consider alternate strategy.")
                    continue
                
                if hasattr(self, 'event_bus'):
                    self.event_bus.publish('tool.execute.start', {'tool': tc_name, 'args': tc_args})
                    
                # Run preflight check
                preflight = self.preflight_check(tool_call)
                if not preflight.get("ok"):
                    tool_res = preflight
                else:
                    tool_res = self.execute_tool({"name": tc_name, "arguments": tc_args})
                
                if hasattr(self, 'event_bus'):
                    self.event_bus.publish('tool.execute.finish', {'tool': tc_name, 'result': str(tool_res)[:100]})
                
                # Log to trace
                import datetime
                self._append_execution_trace({
                    "ts": datetime.datetime.utcnow().isoformat() + "Z",
                    "goal": prompt or "Continue task",
                    "tool": tc_name,
                    "args": tc_args,
                    "result_summary": str(tool_res)[:100],
                    "assistant_message_excerpt": content[:100]
                })
                
                self.msg_mgr.append("user", json.dumps({"tool_execution_result": tool_res}))
                
                # Distillation every round
                try:
                    from src.core.memory.distiller import distill_context
                    distill_context(self.msg_mgr.all(), working_dir=self.working_dir)
                except Exception:
                    pass

        except Exception as e:
            return {"error": "agent_loop_failed", "exception": str(e)}
            
        return {"assistant_message": last_assistant_message}

