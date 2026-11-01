"""Comprehensive regression tests for the 2026 LangGraph & Model-Fit Architecture upgrades."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.inference.adapters.ollama_adapter import OllamaAdapter
from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.inference.model_tiers import (
    ModelTier,
    classify_model,
    extract_model_params,
    is_nano_model,
)
from src.core.inference.streaming import (
    StreamedTurnAccumulator,
    decode_sse_line,
    extract_stream_deltas,
    parse_sse_chunk,
)
from src.core.inference.workflow_selector import ThinkingMode, WorkflowConfig, WorkflowType
from src.core.orchestration.graph.builder import compile_canonical_graph
from src.core.orchestration.graph.nodes.compact_node import _compact_node_impl
from src.core.orchestration.graph.nodes.finalize_node import _finalize_node_impl
from src.core.orchestration.graph.nodes.frontier_dispatch import (
    capture_workspace_file_state,
    detect_workspace_mutations,
    partition_tool_call_waves,
)
from src.core.orchestration.graph.nodes.frontier_loop_node import (
    _prepare_turn_messages,
    frontier_loop_node,
)
from src.core.orchestration.graph.nodes.in_loop_verification import compute_file_sha256
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.runtime_policy import (
    resolve_effective_tool_schemas,
    resolve_runtime_policy,
)
from src.core.orchestration.rollback_manager import RollbackManager
from src.tools._registry import build_registry


class TestModelTiersAndRuntimePolicy2026:
    def test_2026_model_parameter_and_moe_extraction(self):
        # MoE total parameter count AND active parameter count extracted accurately
        assert extract_model_params("gemma-4-26B-A4B") == (26.0, 4.0)
        assert extract_model_params("qwen3.5-35b-a3b") == (35.0, 3.0)
        assert extract_model_params("qwen3.5-9b-instruct") == (9.0, 9.0)
        assert extract_model_params("llama-4-scout") == (109.0, 17.0)
        assert extract_model_params("llama-4-maverick") == (400.0, 60.0)
        assert is_nano_model("qwen3-4b") is True
        assert is_nano_model("qwen3.5-9b") is False
        assert is_nano_model("llama-4-8b") is True
        assert is_nano_model("llama-4-14b") is False

    def test_2026_frontier_and_moe_model_classification(self):
        # 2026 OpenAI generation
        assert classify_model("gpt-6") == ModelTier.FRONTIER
        assert classify_model("gpt-6.1") == ModelTier.FRONTIER
        assert classify_model("gpt-6-astra") == ModelTier.FRONTIER
        assert classify_model("gpt-5.6") == ModelTier.FRONTIER
        assert resolve_runtime_policy(model="gpt-6").policy_tier == "frontier"

        # 2026 Anthropic Claude generation
        assert classify_model("claude-sonnet-4.6") == ModelTier.FRONTIER
        assert classify_model("claude-opus-4.6") == ModelTier.FRONTIER
        assert classify_model("claude-5") == ModelTier.FRONTIER
        assert resolve_runtime_policy(model="claude-sonnet-4.6").policy_tier == "frontier"

        # 2026 Gemini & DeepSeek
        assert classify_model("gemini-3.5") == ModelTier.FRONTIER
        assert classify_model("deepseek-v3.5") == ModelTier.FRONTIER
        assert classify_model("deepseek-r2") == ModelTier.FRONTIER

        # 2026 Llama 4 Scout (17B active MoE) vs Maverick (400B MoE)
        assert classify_model("llama-4-scout") == ModelTier.MEDIUM
        assert resolve_runtime_policy(model="llama-4-scout").policy_tier == "standard"
        assert classify_model("llama-4-maverick") == ModelTier.FRONTIER
        assert resolve_runtime_policy(model="llama-4-maverick").policy_tier == "frontier"

        # Llama 4 parameterized
        assert classify_model("llama-4-8b") == ModelTier.SMALL
        assert resolve_runtime_policy(model="llama-4-8b").policy_tier == "nano"
        assert classify_model("llama-4-14b") == ModelTier.SMALL
        assert resolve_runtime_policy(model="llama-4-14b").policy_tier == "compact"

    def test_local_distill_not_misclassified_as_cloud_frontier(self):
        # Local 27B distill mentioning claude-4.6-opus must classify by 27B size, not cloud FRONTIER
        tier = classify_model(
            "qwen3.5-27b-claude-4.6-opus-reasoning-distilled", context_window=32768
        )
        assert tier != ModelTier.FRONTIER

    def test_four_profile_runtime_policy_resolution(self):
        nano = resolve_runtime_policy(
            model="qwen3-4b", provider="ollama", context_window=16384
        )
        assert nano.policy_tier == "nano"
        assert nano.toolset_name == "compact-flat"
        assert nano.tool_limit == 12
        assert nano.turn_limit == 16
        assert nano.use_verification is False

        compact = resolve_runtime_policy(
            model="qwen3.5-9b", provider="ollama", context_window=32768
        )
        assert compact.policy_tier == "compact"
        assert compact.toolset_name == "compact-flat"
        assert compact.tool_limit == 12
        assert compact.turn_limit == 24
        assert compact.max_parallel_reads == 2
        assert compact.verification_mode == "targeted"

        standard = resolve_runtime_policy(
            model="gemma-4-26B-A4B", provider="lm_studio", context_window=65536
        )
        assert standard.policy_tier == "standard"
        assert standard.toolset_name == "standard-flat"
        assert standard.tool_limit == 18
        assert standard.turn_limit == 36
        assert standard.max_parallel_reads == 3

        frontier = resolve_runtime_policy(
            model="claude-sonnet-4-6", provider="anthropic", context_window=200000
        )
        assert frontier.policy_tier == "frontier"
        assert frontier.toolset_name == "frontier-flat"
        assert frontier.tool_limit == 25
        assert frontier.turn_limit == 60
        assert frontier.max_parallel_reads == 5
        assert frontier.verification_mode == "full"

    def test_explicit_workflow_config_overrides_honored(self):
        wf = WorkflowConfig(
            workflow_type=WorkflowType.CAPABLE_LOOP,
            context_limit=24000,
            tool_limit=15,
            max_turns=11,
            thinking_mode=ThinkingMode.OFF,
            use_verification=False,
            use_replan=True,
            use_vector_memory=False,
            graph_type="canonical_6node",
        )
        orch = SimpleNamespace(workflow_config=wf, deterministic=False)
        pol = resolve_runtime_policy(
            orch,
            model="qwen3:14b",
            provider="ollama",
            context_window=32768,
        )
        assert pol.turn_limit == 11
        assert pol.tool_limit == 15
        assert pol.use_verification is False
        assert pol.thinking_enabled is False


class TestFlatToolsetsAndRoleFiltering:
    def test_flat_toolset_counts_and_alias_invisibility(self):
        reg = build_registry()
        schemas = reg.get_openai_functions()
        schema_names = {s["function"]["name"] for s in schemas}

        # Alias-only capability wrappers are hidden from model-facing schemas
        for alias in ("files_read", "files_write", "files_edit", "shell", "verify"):
            assert alias not in schema_names
            # Yet remain registered as executable aliases
            assert reg.get(alias) is not None

        orch = SimpleNamespace(tool_registry=reg, explore_mode=False)

        p_compact = resolve_runtime_policy(
            model="qwen3.5-9b", provider="ollama", context_window=32768
        )
        p_standard = resolve_runtime_policy(
            model="gemma-4-26B-A4B", provider="ollama", context_window=65536
        )
        p_frontier = resolve_runtime_policy(
            model="gpt-5", provider="openai", context_window=200000
        )

        t_compact = resolve_effective_tool_schemas(
            orch, p_compact, active_role="operational"
        )
        t_standard = resolve_effective_tool_schemas(
            orch, p_standard, active_role="operational"
        )
        t_frontier = resolve_effective_tool_schemas(
            orch, p_frontier, active_role="operational"
        )

        assert len(t_compact) == 12
        assert len(t_standard) == 18
        assert len(t_frontier) == 25

    def test_readonly_roles_and_explore_mode_strip_mutating_tools(self):
        reg = build_registry()
        orch = SimpleNamespace(tool_registry=reg, explore_mode=False)
        p_frontier = resolve_runtime_policy(
            model="claude-opus-4-6", provider="anthropic", context_window=200000
        )

        mutating = {
            "write_file",
            "edit_file_atomic",
            "edit_file",
            "edit_by_line_range",
            "delete_file",
            "apply_patch",
            "bash",
        }
        for ro_role in ("analyst", "reviewer", "scout"):
            ro_schemas = resolve_effective_tool_schemas(
                orch, p_frontier, active_role=ro_role
            )
            ro_names = {s["function"]["name"] for s in ro_schemas}
            assert ro_names.isdisjoint(mutating), (
                f"role {ro_role} leaked mutating tools: {ro_names & mutating}"
            )

        orch_explore = SimpleNamespace(tool_registry=reg, explore_mode=True)
        ex_schemas = resolve_effective_tool_schemas(
            orch_explore, p_frontier, active_role="operational"
        )
        ex_names = {s["function"]["name"] for s in ex_schemas}
        assert ex_names.isdisjoint(mutating)


class TestStreamingAndAdapterStructuredMessages:
    def test_streamed_turn_accumulator_openai_and_anthropic_deltas(self):
        acc = StreamedTurnAccumulator()
        raw_lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Step 1"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig_abc"}}',
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Reading file."}}',
            'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file"}}',
            'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\": "}}',
            'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\\"a.py\\"}"}}',
        ]
        visible_parts = []
        for line in raw_lines:
            payload = decode_sse_line(line)
            chunk = parse_sse_chunk(payload) if payload else None
            if chunk:
                acc.feed_chunk(chunk)
                deltas = extract_stream_deltas(chunk)
                if deltas:
                    _, r_delta, c_delta = deltas
                    if r_delta:
                        acc.record_reasoning(r_delta)
                    if c_delta:
                        visible_parts.append(c_delta)

        assert "".join(visible_parts) == "Reading file."
        tcs = acc.build_tool_calls()
        assert len(tcs) == 1
        assert tcs[0]["id"] == "toolu_1"
        assert tcs[0]["function"]["name"] == "read_file"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"path": "a.py"}

        tbs = acc.build_thinking_blocks()
        assert len(tbs) == 1
        assert tbs[0]["thinking"] == "Step 1"
        assert tbs[0]["signature"] == "sig_abc"

    def test_ollama_and_openai_compat_structured_tool_projections(self):
        history = [
            {"role": "user", "content": "check a.py"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Let me inspect a.py",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "a.py"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": '{"content": "x=1"}',
            },
        ]

        # Ollama requires dict arguments and tool_name on role="tool"
        ollama_msgs = OllamaAdapter._format_outbound_messages(history)
        assert isinstance(
            ollama_msgs[1]["tool_calls"][0]["function"]["arguments"], dict
        )
        assert ollama_msgs[2]["tool_name"] == "read_file"

        # OpenAI-compat strips reasoning_content by default (400-safe) and preserves it when requested
        oai_default = OpenAICompatibleAdapter._preprocess_messages(history)
        assert "reasoning_content" not in oai_default[1]
        assert oai_default[1]["tool_calls"][0]["id"] == "call_1"
        assert oai_default[2]["tool_call_id"] == "call_1"

        oai_keep = OpenAICompatibleAdapter._preprocess_messages(
            history, preserve_reasoning_content=True
        )
        assert oai_keep[1]["reasoning_content"] == "Let me inspect a.py"
        assert "<think>" not in (oai_keep[1].get("content") or "")


class TestKVCacheAndNativeFrontierLoop:
    @pytest.mark.asyncio
    async def test_local_provider_system_prompt_prefix_not_mutated(self, tmp_path):
        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            tool_registry=build_registry(),
            _provider_name="ollama",
            deterministic=False,
        )
        pol = resolve_runtime_policy(
            model="qwen3.5-9b", provider="ollama", context_window=32768
        )
        with patch(
            "src.core.inference.provider_utils.resolve_provider_capabilities",
            return_value={
                "provider_name": "ollama",
                "model": "qwen3.5-9b",
                "supports_native_tools": True,
            },
        ):
            msgs = await _prepare_turn_messages(
                orchestrator=orch,
                task="Explain main.py",
                history=[],
                model_tier="medium",
                turns_taken=1,
                role="operational",
                runtime_policy=pol.to_policy_dict(),
            )
        assert msgs[0]["role"] == "system"
        assert "Available tools (native function calling" not in msgs[0]["content"]

    def test_partition_tool_call_waves_groups_readonly_and_isolates_writes(self):
        reg = build_registry()
        orch = SimpleNamespace(tool_registry=reg)
        calls = [
            ("c1", "read_file", {"path": "a.py"}, {"id": "c1"}),
            ("c2", "grep", {"pattern": "foo"}, {"id": "c2"}),
            ("c3", "edit_file_atomic", {"path": "a.py"}, {"id": "c3"}),
            ("c4", "read_file", {"path": "b.py"}, {"id": "c4"}),
        ]
        waves = partition_tool_call_waves(calls, orch, max_parallel_reads=3)
        assert [(is_par, [c[0] for c in batch]) for is_par, batch in waves] == [
            (True, ["c1", "c2"]),
            (False, ["c3"]),
            (False, ["c4"]),
        ]

    @pytest.mark.asyncio
    async def test_parallel_readonly_wave_and_1x_native_history(self, tmp_path):
        start_barrier = threading.Barrier(2, timeout=3.0)
        overlap_detected = False

        def _exec_tool(call):
            nonlocal overlap_detected
            if call["name"] == "read_file":
                try:
                    start_barrier.wait()
                    overlap_detected = True
                except threading.BrokenBarrierError:
                    pass
                time.sleep(0.02)
                return {
                    "ok": True,
                    "result": {
                        "status": "ok",
                        "content": f"data:{call['arguments']['path']}",
                    },
                }
            return {"ok": True, "result": {"status": "ok"}}

        reg = build_registry()
        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            tool_registry=reg,
            execute_tool=_exec_tool,
            event_bus=None,
            _session_modified_files=set(),
            _session_read_files=set(),
            plan_mode=None,
        )

        turn1_resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Read both files in parallel.",
                        "tool_calls": [
                            {
                                "id": "tc_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"a.py"}',
                                },
                            },
                            {
                                "id": "tc_2",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"b.py"}',
                                },
                            },
                        ],
                    }
                }
            ]
        }
        turn2_resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "a.py and b.py both contain valid definitions.",
                    }
                }
            ]
        }

        state = {
            "task": "Inspect a.py and b.py and summarize them",
            "history": [],
            "model_tier": "frontier",
            "working_dir": str(tmp_path),
            "runtime": {
                "policy": resolve_runtime_policy(
                    model="claude-sonnet-4-6",
                    provider="anthropic",
                    context_window=200000,
                ).to_policy_dict()
            },
        }

        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.inference.provider_utils.resolve_provider_capabilities",
                return_value={
                    "provider_name": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "supports_native_tools": True,
                },
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(side_effect=[turn1_resp, turn2_resp]),
            ),
        ):
            res = await frontier_loop_node(state, config={})

        assert overlap_detected is True
        hist = res["history"]
        assert [m["role"] for m in hist] == ["assistant", "tool", "tool", "assistant"]
        assert "```yaml" not in (hist[0].get("content") or "")
        assert hist[0].get("reasoning_content") == "Read both files in parallel."
        assert {hist[1]["tool_call_id"], hist[2]["tool_call_id"]} == {"tc_1", "tc_2"}
        assert res["run"]["outcome"] == "complete"

    def test_bash_workspace_mutation_detection(self, tmp_path):
        f1 = tmp_path / "existing.py"
        f1.write_text("x = 1\n", encoding="utf-8")
        before = capture_workspace_file_state(str(tmp_path))

        # Simulate bash modifying existing.py and creating generated.py
        f1.write_text("x = 222\n", encoding="utf-8")
        f2 = tmp_path / "generated.py"
        f2.write_text("y = 3\n", encoding="utf-8")

        mutated = detect_workspace_mutations(str(tmp_path), before)
        assert str(f1.resolve()) in mutated
        assert str(f2.resolve()) in mutated


class TestDeferredRollbackVerificationAndSettlementGuard:
    def test_rollback_manager_path_normalization_preserves_baseline_v0(self, tmp_path):
        target = tmp_path / "src" / "calc.py"
        target.parent.mkdir(parents=True)
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files(["src/calc.py"])

        # Turn 1 writes broken V1
        target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        # Turn 2 snapshots same file using "./src/calc.py" and absolute path before writing V2
        rm.append_to_snapshot(snap_id, "./src/calc.py")
        rm.append_to_snapshot(snap_id, str(target.resolve()))
        target.write_text(
            "def add(a, b):\n    raise RuntimeError()\n", encoding="utf-8"
        )

        # Rolling back must restore baseline V0 ("a + b"), NOT intermediate V1 ("a - b")
        rb = rm.rollback(snap_id)
        assert rb["ok"] is True
        assert "return a + b" in target.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_deferred_rollback_keeps_edits_on_retry_and_rolls_back_on_exhaustion(
        self, tmp_path
    ):
        mod = tmp_path / "app.py"
        mod.write_text("def val():\n    return 1\n", encoding="utf-8")

        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files(["app.py"])

        # Agent writes broken syntax on disk
        mod.write_text("def val(:\n    return 2\n", encoding="utf-8")

        class _OrchStub:
            def __init__(self):
                self.working_dir = str(tmp_path)
                self._session_modified_files = {str(mod)}
                self._step_snapshot_id = snap_id

            def rollback_step_transaction(self):
                sid = self._step_snapshot_id
                self._step_snapshot_id = None
                return rm.rollback(sid)

            def commit_step_transaction(self):
                self._step_snapshot_id = None

        orch = _OrchStub()
        base_state = {
            "working_dir": str(tmp_path),
            "modified_files": [str(mod)],
            "last_tool_name": "edit_file_atomic",
            "last_result": {"status": "ok", "path": str(mod)},
            "history": [{"role": "user", "content": "update app.py"}],
            "runtime": {
                "policy": {"verification_mode": "targeted", "recovery_limit": 1}
            },
            "run": {"recoveries": 0},
        }

        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=orch,
        ):
            res1 = await verification_node(base_state, config={})

        assert res1["verification_passed"] is False
        assert res1["verification_result"]["step_rollback"]["deferred"] is True
        assert res1["verification_result"]["step_rollback"]["triggered"] is False
        assert orch._step_snapshot_id == snap_id
        assert "def val(:" in mod.read_text(encoding="utf-8")
        assert res1["run"]["recoveries"] == 1
        assert res1["run"]["outcome"] == "continue"
        assert 'disk_state="EDITS_PRESERVED"' in res1["history"][-1]["content"]

        # Attempt 2 still fails and recovery_limit (1) is exhausted -> IMMEDIATE ROLLBACK to V0!
        state_attempt_2 = {
            **base_state,
            "run": res1["run"],
            "history": res1["history"],
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=orch,
        ):
            res2 = await verification_node(state_attempt_2, config={})

        assert res2["verification_passed"] is False
        assert res2["verification_result"]["step_rollback"]["triggered"] is True
        assert orch._step_snapshot_id is None
        assert "def val():\n    return 1" in mod.read_text(encoding="utf-8")
        assert res2["run"]["outcome"] == "failed"

    @pytest.mark.asyncio
    async def test_finalize_transaction_settlement_guard_rolls_back_unverified_snapshot(
        self, tmp_path
    ):
        mod = tmp_path / "service.py"
        mod.write_text("VERSION = 1\n", encoding="utf-8")
        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files(["service.py"])
        mod.write_text("VERSION = 999  # unverified mutation\n", encoding="utf-8")

        class _OrchStub:
            working_dir = str(tmp_path)
            _step_snapshot_id = snap_id
            cancel_event = None

            def rollback_step_transaction(self):
                sid = self._step_snapshot_id
                self._step_snapshot_id = None
                return rm.rollback(sid)

            def commit_step_transaction(self):
                self._step_snapshot_id = None

        orch = _OrchStub()
        state = {
            "task": "Update service",
            "working_dir": str(tmp_path),
            "history": [],
            "verification_passed": False,
            "run": {"status": "failed", "outcome": "failed"},
        }
        with patch(
            "src.core.orchestration.graph.nodes.finalize_node._resolve_orchestrator",
            return_value=orch,
        ):
            res = await _finalize_node_impl(state, config={})

        assert orch._step_snapshot_id is None
        assert mod.read_text(encoding="utf-8") == "VERSION = 1\n"
        assert res["run"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_sha256_verified_hash_deduplication_and_tampered_verify_yaml_rejection(
        self, tmp_path
    ):
        mod = tmp_path / "clean.py"
        mod.write_text("x = 42\n", encoding="utf-8")
        digest = compute_file_sha256(mod)

        orch = SimpleNamespace(
            working_dir=str(tmp_path), _session_modified_files={str(mod)}
        )
        state = {
            "working_dir": str(tmp_path),
            "modified_files": [str(mod)],
            "last_tool_name": "edit_file_atomic",
            "last_result": {"status": "ok", "path": str(mod)},
            "_verified_file_hashes": {str(mod.resolve()): digest},
            "runtime": {
                "policy": {"verification_mode": "targeted", "recovery_limit": 2}
            },
            "run": {"recoveries": 0},
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=orch,
        ):
            res = await verification_node(state, config={})

        assert res["verification_passed"] is True
        assert res["verification_result"]["syntax"]["checked_files"] == 0
        assert res["verification_result"]["syntax"]["skipped_cached"] == 1

        # Verify fail-closed rejection when agent modifies .codingagent/verify.yaml during session
        cfg_dir = tmp_path / ".codingagent"
        cfg_dir.mkdir()
        v_yaml = cfg_dir / "verify.yaml"
        v_yaml.write_text(
            "commands:\n  - ['python3', '-c', 'print(1)']\n", encoding="utf-8"
        )

        orch_tampered = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files={str(mod), str(v_yaml)},
        )
        state_tampered = {
            **state,
            "modified_files": [str(mod), str(v_yaml)],
            "runtime": {
                "policy": {"verification_mode": "targeted", "recovery_limit": 0}
            },
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=orch_tampered,
        ):
            res_tampered = await verification_node(state_tampered, config={})

        assert res_tampered["verification_passed"] is False
        assert (
            "modified during the same agent session"
            in res_tampered["verification_result"]["custom_verify"]["error"]
        )


class TestTurnGroupAtomicCompaction:
    @pytest.mark.asyncio
    async def test_compaction_never_splits_assistant_tool_calls_from_tool_results(self):
        history = [{"role": "user", "content": "Refactor 20 modules"}]
        for i in range(16):
            cid = f"call_{i}"
            history.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": f"m{i}.py"}),
                            },
                        }
                    ],
                }
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": "read_file",
                    "content": "X" * 800,
                }
            )

        assert len(history) == 33
        res = await _compact_node_impl(
            {
                "task": "Refactor 20 modules",
                "history": history,
                "modified_files": ["m0.py"],
                "run": {"compactions": 0},
                "runtime": {"policy": {"compaction_limit": 2}},
            },
            config=MagicMock(),
        )
        compacted = res["history"]
        assert len(compacted) < len(history)
        assert any(
            "<context_checkpoint>" in str(m.get("content", "")) for m in compacted
        )

        idx = 0
        while idx < len(compacted):
            msg = compacted[idx]
            assert msg["role"] != "tool", f"Orphan tool message at index {idx}: {msg}"
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                expected_ids = [tc["id"] for tc in msg["tool_calls"]]
                actual_ids = []
                idx += 1
                while idx < len(compacted) and compacted[idx]["role"] == "tool":
                    actual_ids.append(compacted[idx]["tool_call_id"])
                    idx += 1
                assert actual_ids == expected_ids
            else:
                idx += 1

    def test_six_node_canonical_graph_topology_has_no_recover_node(self):
        graph = compile_canonical_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert node_names == {
            "__start__",
            "initialize",
            "agent_loop",
            "approval",
            "verify",
            "compact",
            "finalize",
            "__end__",
        }
