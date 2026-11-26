"""Offline prompt & tool-schema token budget report (`codingagent context-report`).

TASK-28: Prints estimated token breakdown for static system prompt, project
instruction files (``AGENTS.md`` / ``CLAUDE.md``), bundled/workspace skills,
and OpenAI tool JSON schemas per tier without contacting any LLM provider.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_VALID_TIERS: Tuple[str, ...] = ("compact-flat", "standard-flat", "frontier-flat")
_TIER_TO_MODEL_TIER: Dict[str, str] = {
    "compact-flat": "small",
    "standard-flat": "medium",
    "frontier-flat": "frontier",
}


def estimate_tokens(text: str) -> int:
    """Fast deterministic token estimate (~4 chars/token, matching ContextBuilder)."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def build_context_report(
    *,
    workdir: Optional[str] = None,
    tier: str = "compact-flat",
) -> Dict[str, Any]:
    """Compute an offline token breakdown for *tier* in *workdir*."""
    from src.config.toolsets.loader import get_tools_for_toolset
    from src.core.context.context_builder import ContextBuilder
    from src.core.context.instruction_files import (
        discover_instruction_files,
        render_instruction_files,
    )
    from src.core.orchestration.registry_builder import example_registry

    if tier not in _VALID_TIERS:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {', '.join(_VALID_TIERS)}")

    wd = Path(workdir).resolve() if workdir else Path.cwd().resolve()

    # 1. Base static system prompt (identity + operational role)
    builder = ContextBuilder(working_dir=str(wd))
    base_identity = getattr(builder, "soul", "") or ""
    roles_map: Dict[str, str] = getattr(builder, "roles", None) or {}
    role_prompt = roles_map.get("operational") or roles_map.get("coder") or ""
    base_prompt_text = "\n\n".join(p for p in (base_identity, role_prompt) if p.strip())
    base_chars = len(base_prompt_text)
    base_tokens = estimate_tokens(base_prompt_text)

    # 2. Project instruction files (AGENTS.md / CLAUDE.md hierarchy)
    inst_files = discover_instruction_files(wd)
    inst_rendered = render_instruction_files(inst_files) if inst_files else ""
    inst_chars = len(inst_rendered)
    inst_tokens = estimate_tokens(inst_rendered)

    # 3. Skills inventory (full bodies vs compact metadata index)
    skills_map: Dict[str, str] = dict(getattr(builder, "skills", None) or {})
    skills_full_chars = sum(len(v or "") for v in skills_map.values())
    skills_full_tokens = estimate_tokens("".join(skills_map.values()))
    skills_catalog_text = (
        builder.get_skill_catalog_text()
        if hasattr(builder, "get_skill_catalog_text")
        else "\n".join(
            f"- {name}: {(body.strip().splitlines()[0] if body and body.strip() else name)[:100]}"
            for name, body in sorted(skills_map.items())
        )
    )
    skills_catalog_tokens = estimate_tokens(skills_catalog_text)

    # 4. Tool JSON schemas for selected flat tier
    tool_names = set(get_tools_for_toolset(tier))
    registry = example_registry()
    all_schemas = registry.get_openai_functions(include_aliases=False)
    tier_schemas: List[Dict[str, Any]] = []
    per_tool: List[Dict[str, Any]] = []
    for schema in all_schemas:
        fn_block = schema.get("function") if isinstance(schema, dict) else None
        name = str((fn_block or {}).get("name") or "")
        if name in tool_names:
            tier_schemas.append(schema)
            serialized = json.dumps(schema, ensure_ascii=False)
            per_tool.append(
                {
                    "name": name,
                    "chars": len(serialized),
                    "tokens": estimate_tokens(serialized),
                }
            )
    per_tool.sort(key=lambda item: (-int(item["tokens"]), str(item["name"])))
    tools_serialized = json.dumps(tier_schemas, ensure_ascii=False)
    tools_chars = len(tools_serialized)
    tools_tokens = estimate_tokens(tools_serialized)

    total_upfront_tokens = base_tokens + inst_tokens + skills_catalog_tokens + tools_tokens
    total_upfront_chars = base_chars + inst_chars + len(skills_catalog_text) + tools_chars

    return {
        "workdir": str(wd),
        "tier": tier,
        "model_tier": _TIER_TO_MODEL_TIER[tier],
        "sections": {
            "base_prompt": {"chars": base_chars, "tokens": base_tokens},
            "instruction_files": {
                "count": len(inst_files),
                "paths": [str(f.path) for f in inst_files],
                "chars": inst_chars,
                "tokens": inst_tokens,
            },
            "skills_catalog": {
                "count": len(skills_map),
                "catalog_chars": len(skills_catalog_text),
                "catalog_tokens": skills_catalog_tokens,
                "full_bodies_chars": skills_full_chars,
                "full_bodies_tokens": skills_full_tokens,
            },
            "tool_schemas": {
                "count": len(tier_schemas),
                "chars": tools_chars,
                "tokens": tools_tokens,
                "top_tools": per_tool[:5],
            },
        },
        "totals": {
            "upfront_chars": total_upfront_chars,
            "upfront_tokens": total_upfront_tokens,
            "pct_of_16k": round((total_upfront_tokens / 16_384) * 100.0, 2),
            "pct_of_32k": round((total_upfront_tokens / 32_768) * 100.0, 2),
        },
    }


def _print_context_report(report: Dict[str, Any]) -> None:
    sec = report["sections"]
    tot = report["totals"]
    print(f"\nCodingAgent Context Report — tier: {report['tier']} ({report['workdir']})")
    print("-" * 72)
    print(
        f"  Base prompt (identity+role) : {sec['base_prompt']['tokens']:6,d} tok "
        f"({sec['base_prompt']['chars']:,d} chars)"
    )
    print(
        f"  Instruction files ({sec['instruction_files']['count']:2d})      : "
        f"{sec['instruction_files']['tokens']:6,d} tok "
        f"({sec['instruction_files']['chars']:,d} chars)"
    )
    print(
        f"  Skills catalog ({sec['skills_catalog']['count']:2d} skills) : "
        f"{sec['skills_catalog']['catalog_tokens']:6,d} tok "
        f"(full bodies on disk: {sec['skills_catalog']['full_bodies_tokens']:,d} tok)"
    )
    print(
        f"  Tool schemas ({sec['tool_schemas']['count']:2d} tools)     : "
        f"{sec['tool_schemas']['tokens']:6,d} tok "
        f"({sec['tool_schemas']['chars']:,d} chars)"
    )
    print("-" * 72)
    print(
        f"  Total upfront static budget : {tot['upfront_tokens']:6,d} tok "
        f"({tot['pct_of_16k']:.1f}% of 16K | {tot['pct_of_32k']:.1f}% of 32K)"
    )
    top_tools = sec["tool_schemas"]["top_tools"]
    if top_tools:
        print("\n  Heaviest tool schemas:")
        for item in top_tools:
            print(f"    - {item['name']:<24s} {item['tokens']:5,d} tok ({item['chars']:,d} chars)")
    print()


def run_context_report(args: Any) -> int:
    """CLI entry point for ``codingagent context-report``."""
    tier = getattr(args, "tier", None) or "compact-flat"
    workdir = getattr(args, "workdir", None)
    as_json = bool(getattr(args, "json", False))
    try:
        report = build_context_report(workdir=workdir, tier=tier)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_context_report(report)
    return 0
