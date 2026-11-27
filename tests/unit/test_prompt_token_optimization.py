"""Unit tests for lossless prompt and tool-schema token optimizations."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.core.context.context_builder import ContextBuilder
from src.core.context.static_prompt_parts import (
    build_output_format_block,
    build_static_system_parts,
    build_thinking_mode_block,
)
from src.core.orchestration.graph.runtime_policy import (
    COMPACT_RUNTIME_POLICY,
    resolve_runtime_policy,
)
from src.tools._registry import build_registry


def test_static_system_parts_skips_duplicate_available_tools_when_native_tools_enabled():
    result = build_static_system_parts(
        soul="Soul text",
        role_content="Role text",
        active_skills=[],
        get_skill=lambda _name: "",
        sanitize_text=lambda text: text,
        system_prompt_dynamic_boundary="__BOUNDARY__",
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="medium",
        provider_capabilities={"supports_native_tools": True},
        model_name="qwen/qwen3.8-27b",
        use_native_tools=True,
        is_simple_mode=False,
        build_model_constraints_block_fn=lambda _mt, _tools: "",
        build_instruction_files_block_fn=lambda: "",
        build_project_instructions_block_fn=lambda: "",
        render_tools_for_tier_fn=lambda _tools, _mt: "name: read_file\ndescription: Read a file.\n",
        build_thinking_guidance_block_fn=lambda _mt, _pc, _mn: "",
        is_reasoning_model_fn=lambda _model: False,
        build_thinking_mode_block_fn=lambda _tier, _is_rm: "",
        build_output_format_block_fn=lambda unt, ism, tier: build_output_format_block(
            use_native_tools=unt, is_simple_mode=ism, tier_str=tier
        ),
    )

    assert "<available_tools>" not in result
    assert "native JSON function calling API" in result
    assert "<think>" not in result


def test_thinking_mode_omitted_for_all_tiers_and_models():
    assert build_thinking_mode_block(tier_str="medium", is_reasoning_model=True) == ""
    assert build_thinking_mode_block(tier_str="frontier", is_reasoning_model=True) == ""
    assert build_thinking_mode_block(tier_str="frontier", is_reasoning_model=False) == ""


def test_select_role_for_compact_and_nano_uses_small_role():
    cb = ContextBuilder()
    small = cb._select_role_for_tier("operational", "small")
    compact = cb._select_role_for_tier("operational", "compact")
    nano = cb._select_role_for_tier("operational", "nano")
    assert compact == small
    assert nano == small
    assert "Small Model" in compact


def test_compact_openai_schemas_reduce_size_without_dropping_parameters(tmp_path):
    reg = build_registry(working_dir=str(tmp_path))
    full = reg.get_openai_functions(compact=False)
    compact = reg.get_openai_functions(compact=True)

    assert len(full) == len(compact)
    full_bytes = len(json.dumps(full))
    compact_bytes = len(json.dumps(compact))
    assert compact_bytes < full_bytes

    by_name_full = {t["function"]["name"]: t["function"] for t in full}
    by_name_compact = {t["function"]["name"]: t["function"] for t in compact}
    for name, fn_full in by_name_full.items():
        fn_comp = by_name_compact[name]
        p_full = fn_full.get("parameters") or {}
        p_comp = fn_comp.get("parameters") or {}
        assert set((p_full.get("properties") or {}).keys()) == set(
            (p_comp.get("properties") or {}).keys()
        )
        assert p_full.get("required") == p_comp.get("required")
        for pname, pval in (p_full.get("properties") or {}).items():
            cval = (p_comp.get("properties") or {})[pname]
            assert pval.get("type") == cval.get("type")
            if "enum" in pval:
                assert pval["enum"] == cval.get("enum")


def test_resolve_runtime_policy_honors_provider_and_model_policy_tier_override():
    adapter = SimpleNamespace(
        name="groq",
        default_model="qwen/qwen3.8-27b",
        provider={"name": "Groq", "type": "groq", "policy_tier": "compact"},
    )
    orch = SimpleNamespace(_adapter=adapter, _provider_name="groq", model="qwen/qwen3.8-27b")
    pol = resolve_runtime_policy(orch, model="qwen/qwen3.8-27b", provider="groq")
    assert pol.policy_tier == "compact"
    assert pol.toolset_name == COMPACT_RUNTIME_POLICY.toolset_name
    assert pol.tool_limit == COMPACT_RUNTIME_POLICY.tool_limit

    # Per-model override takes precedence over provider default
    adapter2 = SimpleNamespace(
        name="custom",
        default_model="my-27b-model",
        provider={
            "name": "custom",
            "policy_tier": "standard",
            "model_policy_tiers": {"my-27b-model": "compact"},
        },
    )
    orch2 = SimpleNamespace(_adapter=adapter2, _provider_name="custom", model="my-27b-model")
    pol2 = resolve_runtime_policy(orch2, model="my-27b-model", provider="custom")
    assert pol2.policy_tier == "compact"


def test_scenario_evaluator_debug_logging(tmp_path, capsys):
    from src.core.evaluation.scenario_evaluator import Scenario, ScenarioEvaluator

    class _FakeBus:
        def __init__(self):
            self._subs = {}

        def subscribe(self, topic, fn):
            self._subs.setdefault(topic, []).append(fn)

        def unsubscribe(self, topic, fn):
            if topic in self._subs and fn in self._subs[topic]:
                self._subs[topic].remove(fn)

        def publish(self, topic, payload):
            for fn in list(self._subs.get(topic, [])):
                fn(payload)

    bus = _FakeBus()

    class _FakeAgent:
        event_bus = bus
        working_dir = None
        _adapter = SimpleNamespace(name="gemini", default_model="gemini-3.5-flash-lite")

        def run_agent_once(self, *args, **kwargs):
            bus.publish("model.response", {"prompt_tokens": 500, "completion_tokens": 40, "latency": 0.42, "model": "gemini-3.5-flash-lite"})
            bus.publish("tool.execute.start", {"tool": "write_file", "args": {"path": "hello.py"}})
            bus.publish("tool.execute.finish", {"tool": "write_file", "ok": True})
            (Path(self.working_dir) / "hello.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
            return {"assistant_message": "done"}

    from pathlib import Path

    ev = ScenarioEvaluator(workdir=str(tmp_path), debug=True)
    sc = Scenario(
        name="dbg_demo",
        description="demo",
        task="Create hello.py",
        expected_files={"hello.py": "def hello"},
    )
    res = ev.run_scenario(sc, lambda: _FakeAgent())
    out = capsys.readouterr().out
    assert res.status == "pass"
    assert ">>> dbg_demo | provider=gemini model=gemini-3.5-flash-lite" in out
    assert "LLM  call #1: 0.42s | tok=(500p/40c)" in out
    assert "TOOL start #1: write_file path='hello.py'" in out
    assert "TOOL done  write_file ok=True" in out
    assert "<<< dbg_demo: PASS" in out


def test_grep_excludes_internal_codingagent_dir_and_binary_files(tmp_path):
    from src.tools.system_tools import grep

    (tmp_path / "app.py").write_text("def compute_total(x):\n    return x\n", encoding="utf-8")
    internal = tmp_path / ".codingAgent" / "snapshots"
    internal.mkdir(parents=True)
    (internal / "backup.py").write_text("def compute_total(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01compute_total\x00\x02")

    res = grep("compute_total", path=".", workdir=tmp_path)
    assert res.get("ok") is True
    payload = res.get("output") or res.get("result") or {}
    matches = payload.get("matches") or []
    paths = [m.get("file_path", "") for m in matches]
    assert any(p.endswith("app.py") for p in paths)
    assert not any(".codingAgent" in p for p in paths)
    assert not any(p.endswith("blob.bin") for p in paths)


def test_openai_compat_build_payload_strips_internal_orchestration_kwargs():
    from src.core.inference.adapters.gemini_adapter import GeminiAdapter

    a = GeminiAdapter(api_key="test_key")
    payload, _ = a._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model_name="gemini-3.5-flash-lite",
        kwargs={
            "temperature": 0.2,
            "verification_retries": 1,
            "policy_tier": "compact",
            "reasoning_effort": "low",
            "correlation_id": "cid-123",
        },
    )
    assert payload["temperature"] == 0.2
    for leaked in ("verification_retries", "policy_tier", "reasoning_effort", "correlation_id"):
        assert leaked not in payload


def test_validation_debt_ignores_rc5_rc127_skipped_and_accepts_python_test_runners():
    from src.core.orchestration.graph.validation_authority import ValidationDebtTracker

    t = ValidationDebtTracker()
    # Non-failure exits must never create debt
    t.record_command("pytest", passed=False, exit_code=5)
    t.record_command("pytest", passed=False, exit_code=127)
    t.record_command("pytest", passed=False, skipped=True)
    assert t.unpaid_debt() == []

    # Real failure (rc=1) creates debt; post-mutation unittest/pytest pays it down
    t.record_command("pytest", passed=False, exit_code=1)
    assert t.unpaid_debt() == ["pytest"]
    t.record_mutation()
    t.record_command("python3 -m unittest test_merge.py", passed=True, exit_code=0)
    assert t.unpaid_debt() == []


def test_run_tests_falls_back_to_sys_executable_when_pytest_not_on_path(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from src.tools import verification_tools

    (tmp_path / "test_ok.py").write_text("def test_one():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    monkeypatch.setattr(verification_tools.shutil, "which", lambda _name: None)
    captured_cmd = []

    def _fake_run(cmd, **kwargs):
        captured_cmd.append(list(cmd))
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "1 passed in 0.01s\n"
        return proc

    monkeypatch.setattr(verification_tools, "_run_subprocess", _fake_run)
    res = verification_tools.run_tests(str(tmp_path))
    assert res["status"] == "ok"
    assert captured_cmd and captured_cmd[0][:3] == [verification_tools.sys.executable, "-m", "pytest"]


def test_select_prompt_partial_routes_gemini_flash_lite_to_small():
    from src.core.context.static_prompt_parts import select_prompt_partial

    assert (
        select_prompt_partial(
            model_tier="compact",
            provider_capabilities={"model": "gemini-3.5-flash-lite"},
            is_reasoning=False,
            load_partial=lambda name: name,
        )
        == "gemini-small.md"
    )
    assert (
        select_prompt_partial(
            model_tier="frontier",
            provider_capabilities={"model": "gemini-3.1-pro-preview"},
            is_reasoning=False,
            load_partial=lambda name: name,
        )
        == "gemini-frontier.md"
    )


def test_core_system_prompt_token_ceilings_after_deduplication(tmp_path):
    import re
    from src.core.inference.tokenizer import count_tokens

    ContextBuilder.clear_cache()
    cb = ContextBuilder(working_dir=str(tmp_path))
    tools = [
        {"name": "read_file", "description": "Read a file."},
        {"name": "edit_file_atomic", "description": "Edit a file."},
        {"name": "write_file", "description": "Write a file."},
        {"name": "bash", "description": "Run a shell command."},
    ]

    for tier, model, ceiling in (
        ("compact", "gemini-3.5-flash-lite", 950),
        ("frontier", "claude-sonnet-4-6", 1450),
        ("frontier", "o3", 1450),
    ):
        msgs = cb.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="Fix the bug in app.py",
            tools=tools,
            conversation=[],
            model_tier=tier,
            model_name=model,
            provider_capabilities={"model": model, "supports_native_tools": True},
            include_prior_context=False,
        )
        core_sys = re.sub(
            r"<project_instructions[\s\S]*?</project_instructions[^>]*>",
            "",
            msgs[0]["content"],
        )
        toks = count_tokens(core_sys)
        assert toks <= ceiling, f"{tier}/{model} core system prompt regressed to {toks} tok (ceiling {ceiling})"
        assert "<thinking_mode>" not in core_sys
        assert "Execute the next action using JSON function calling format." not in msgs[-1]["content"]


def test_compact_bundled_skill_catalog_preserves_project_skills_and_collapses_bundled(tmp_path):
    from pathlib import Path
    from src.core.context.agent_brain_loading import SkillMeta, render_skill_catalog

    skills = {
        "custom-deploy": SkillMeta(
            name="custom-deploy",
            description="Project-specific deploy checklist",
            path=Path(tmp_path / "custom.md"),
            provenance="project",
            source_scope=".codingagent",
        ),
        "dry": SkillMeta(
            name="dry",
            description="Avoid code duplication across modules",
            path=Path(tmp_path / "dry.md"),
            provenance="bundled",
            source_scope="bundled",
        ),
        "refactor": SkillMeta(
            name="refactor",
            description="Refactor safely in small steps",
            path=Path(tmp_path / "refactor.md"),
            provenance="bundled",
            source_scope="bundled",
        ),
    }

    full = render_skill_catalog(skills, compact_bundled=False)
    compact = render_skill_catalog(skills, compact_bundled=True)
    assert "- custom-deploy: Project-specific deploy checklist" in compact
    assert "Bundled: dry, refactor" in compact
    assert "Avoid code duplication" not in compact
    assert len(compact) < len(full)


def test_compact_dedup_hit_and_truncated_tool_call_recovery():
    from src.core.orchestration.loop_guards import (
        build_incomplete_turn_nudge,
        compact_dedup_hit,
        extract_finish_reason,
    )

    large_body = "x = 1\n" * 200
    compacted = compact_dedup_hit(
        "read_file",
        {"path": "app.py"},
        {"ok": True, "status": "ok", "path": "app.py", "content": large_body},
    )
    assert compacted["dedup_cache_hit"] is True
    assert compacted["ok"] is True
    assert "Already read unchanged" in compacted["content"]
    assert len(compacted["content"]) < len(large_body)

    # Small results stay intact
    small = compact_dedup_hit("read_file", {"path": "tiny.py"}, {"ok": True, "content": "a = 1"})
    assert small["content"] == "a = 1"

    # finish_reason == "length" produces chunking recovery guidance
    resp_len = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    assert extract_finish_reason(resp_len) == "length"
    nudge = build_incomplete_turn_nudge("empty_response", "empty", response=resp_len)
    assert "truncated at the output token limit" in nudge
    assert "Split large file edits into smaller chunks" in nudge


def test_subagent_budget_capsule_and_targeted_pytest_fallback(tmp_path):
    from src.tools.subagent_payloads import build_subagent_budget_capsule
    from src.core.orchestration.graph.nodes.verification_helpers import discover_targeted_pytest_files

    scout_capsule = build_subagent_budget_capsule(
        role="scout", mode="scout", max_steps=15, tools_required=True
    )
    assert 'max_steps="15"' in scout_capsule
    assert "uncovered gaps" in scout_capsule
    assert "Tool evidence is mandatory" in scout_capsule

    wt_capsule = build_subagent_budget_capsule(
        role="operational", mode="worktree", max_steps=20, tools_required=False
    )
    assert "Tool evidence is mandatory" not in wt_capsule

    # Targeted pytest discovery picks up non-stem-matching test file importing modified module
    mod = tmp_path / "math_ops.py"
    mod.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    suite = tests_dir / "test_all.py"
    suite.write_text("from math_ops import add\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")

    found = discover_targeted_pytest_files([str(mod)], tmp_path)
    assert str(suite.resolve()) in found
