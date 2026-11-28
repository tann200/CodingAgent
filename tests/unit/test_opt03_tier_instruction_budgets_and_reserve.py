"""Unit tests for OPT-03: tier-scaled instruction budgets and single-source response reserve."""

from __future__ import annotations

from pathlib import Path

from src.cli.context_report_cmd import build_context_report
from src.core.context.context_builder import ContextBuilder
from src.core.context.instruction_files import (
    MAX_TOTAL_INSTRUCTION_CHARS,
    discover_instruction_files,
    get_instruction_budget_for_tier,
    render_instruction_files,
)
from src.core.inference.provider_context import (
    RESPONSE_RESERVE_TOKENS,
    clamp_response_tokens,
    response_reserve,
)


class TestTierInstructionBudgets:
    def test_budget_lookup_scales_by_tier(self) -> None:
        nano_cap, nano_arch = get_instruction_budget_for_tier("nano")
        small_cap, small_arch = get_instruction_budget_for_tier("small")
        compact_cap, compact_arch = get_instruction_budget_for_tier("compact")
        std_cap, std_arch = get_instruction_budget_for_tier("medium")
        front_cap, front_arch = get_instruction_budget_for_tier("frontier")
        default_cap, default_arch = get_instruction_budget_for_tier(None)

        assert nano_cap < small_cap < std_cap < front_cap == MAX_TOTAL_INSTRUCTION_CHARS
        assert small_cap == compact_cap
        assert nano_arch is False
        assert small_arch is False
        assert compact_arch is False
        assert std_arch is True
        assert front_arch is True
        assert default_cap == MAX_TOTAL_INSTRUCTION_CHARS and default_arch is True

    def test_discover_omits_architecture_for_compact_tiers(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Rules\n" + ("rule line\n" * 400), encoding="utf-8")
        (tmp_path / "ARCHITECTURE.md").write_text(
            "# Architecture\n" + ("arch detail\n" * 200), encoding="utf-8"
        )

        small_files = discover_instruction_files(tmp_path, model_tier="small")
        assert [f.path.name for f in small_files] == ["AGENTS.md"]

        frontier_files = discover_instruction_files(tmp_path, model_tier="frontier")
        assert [f.path.name for f in frontier_files] == ["AGENTS.md", "ARCHITECTURE.md"]

        # Explicit override still wins
        explicit_files = discover_instruction_files(
            tmp_path, model_tier="small", include_architecture=True
        )
        assert [f.path.name for f in explicit_files] == ["AGENTS.md", "ARCHITECTURE.md"]

    def test_render_respects_tier_total_budget(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Agents\n" + ("A" * 5000), encoding="utf-8")
        files = discover_instruction_files(tmp_path, model_tier="nano")
        rendered_nano = render_instruction_files(files, model_tier="nano")
        rendered_frontier = render_instruction_files(files, model_tier="frontier")

        assert len(rendered_nano) < len(rendered_frontier)
        assert "[truncated]" in rendered_nano

    def test_context_builder_and_report_apply_compact_budget(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Agents\n" + ("B" * 4500), encoding="utf-8")
        (tmp_path / "ARCHITECTURE.md").write_text("# Arch\n" + ("C" * 3500), encoding="utf-8")

        ContextBuilder.clear_cache()
        builder = ContextBuilder(working_dir=str(tmp_path))
        small_prefix = builder._build_static_system_prefix(
            role_name="operational",
            active_skills=[],
            tools=[],
            model_tier="small",
            provider_capabilities={},
            use_native_tools=True,
            is_simple_mode=False,
        )
        frontier_prefix = builder._build_static_system_prefix(
            role_name="operational",
            active_skills=[],
            tools=[],
            model_tier="frontier",
            provider_capabilities={},
            use_native_tools=True,
            is_simple_mode=False,
        )
        assert "ARCHITECTURE.md" not in small_prefix
        assert "ARCHITECTURE.md" in frontier_prefix

        compact_report = build_context_report(workdir=str(tmp_path), tier="compact-flat")
        frontier_report = build_context_report(workdir=str(tmp_path), tier="frontier-flat")
        assert compact_report["sections"]["instruction_files"]["include_architecture"] is False
        assert frontier_report["sections"]["instruction_files"]["include_architecture"] is True
        assert (
            compact_report["sections"]["instruction_files"]["tokens"]
            < frontier_report["sections"]["instruction_files"]["tokens"]
        )
        assert (
            compact_report["sections"]["skills_catalog"]["catalog_tokens"]
            <= frontier_report["sections"]["skills_catalog"]["catalog_tokens"]
        )


class TestResponseReserveAndClamp:
    def test_response_reserve_is_monotonic_and_preserves_prompt_floor(self) -> None:
        assert response_reserve(0) == 0
        assert response_reserve(-1024) == 0

        windows = [2048, 4096, 8192, 12288, 16384, 32768, 131072]
        reserves = [response_reserve(w) for w in windows]
        usables = [w - r for w, r in zip(windows, reserves)]

        assert reserves == sorted(reserves)
        assert usables == sorted(usables)
        assert all(u >= 1024 for u in usables)
        assert response_reserve(16384) == RESPONSE_RESERVE_TOKENS
        assert response_reserve(32768, max_response_tokens=8192) == 8192
        # Never consumes more than half the window
        assert response_reserve(32768, max_response_tokens=30000) == 16384

    def test_clamp_response_tokens(self) -> None:
        assert clamp_response_tokens(8192, None) is None
        assert clamp_response_tokens(0, 4096) == 4096
        assert clamp_response_tokens(4096, 4096) == 1024
        assert clamp_response_tokens(8192, 4096) == 2048
        assert clamp_response_tokens(32768, 4096) == 4096
        assert clamp_response_tokens(32768, 8192) == 8192
        assert clamp_response_tokens(32768, 25000) == 16384
