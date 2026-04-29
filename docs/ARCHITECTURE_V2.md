# Local-First CodingAgent — Target Architecture (v2)

**Date:** 2026-04-26  
**Status:** PROPOSED — Migration Target  
**Priority:** P0 (Architectural Direction)

---

## 0. Core Philosophy (Non-Negotiable)

- Single execution loop (not graph explosion)
- Two-axis adaptation (model × hardware)
- Context is a budget, not a buffer
- Tools > reasoning (for small models)
- Deterministic degradation (never OOM, never stall)

---

## 1. High-Level Architecture

```
                ┌──────────────────────────┐
                │   CLI / TUI Interface    │
                └────────────┬─────────────┘
                             │
                ┌────────────▼─────────────┐
                │   Session Orchestrator   │
                │  (single loop runtime)   │
                └────────────┬─────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌────────▼────────┐
│ WorkflowSelector│  │ Context Manager │  │ Tool Runtime    │
│ (model+hw)      │  │ (budget engine) │  │ (IO + sandbox)  │
└───────┬────────┘  └────────┬────────┘  └────────┬────────┘
        │                     │                    │
        ▼                     ▼                    ▼
┌───────────────┐   ┌────────────────┐   ┌──────────────────┐
│ Model Adapter │   │ Memory System  │   │ External Systems │
│ (local/cloud) │   │ (short+vector) │   │ (LSP, FS, Git)   │
└───────────────┘   └────────────────┘   └──────────────────┘
```

---

## 2. The Big Simplification (Critical Change)

### Remove:
- 16-node graph
- Fragmented node system
- Tier-based branching inside graph

### Replace with:
**Single Loop Runtime (ReAct++)**

```python
while not done:
    perception = build_context(state)
    response = model.generate(perception)
    
    if response.tool_call:
        result = execute_tool(response.tool_call)
        state.add_tool_result(result)
    else:
        state.add_message(response)
        done = check_completion(response)
```

### Why This Wins (Especially Locally)

| Old System | Problem |
|------------|---------|
| Graph nodes | Token + latency overhead |
| Tier branching | Hard to reason about |
| Multi-phase pipeline | Breaks small models |

| New System | Benefit |
|------------|---------|
| Single loop | Predictable |
| Fewer prompts | Better small-model accuracy |
| Unified logic | Easier to debug |

---

## 3. Two-Axis Intelligence

### A. Model Capability Profile

```python
ModelProfile:
    name: str
    architecture: "dense" | "moe"
    params_total: int
    params_active: int
    quantization: str
    
    reasoning_score: float
    tool_call_reliability: float
    
    max_context: int
    kv_per_token: float
```

### B. Hardware Capability Profile

```python
HardwareProfile:
    vram_gb: float
    ram_gb: float
    cpu_cores: int
    
    max_kv_cache_gb: float
    safe_context_tokens: int
    
    supports_gpu_offload: bool
```

### C. Runtime Decision (THE KEY)

```python
RuntimeProfile = merge(model, hardware)
```

This drives: context size, tool count, loop depth, thinking mode, compaction strategy.

---

## 4. Workflow Selector (Binary, Not Tier Explosion)

Replace 5 tiers with 2 modes:

### 🟢 SMALL MODE (Qwen3.5 9B, Gemma4 4B active)

- max_turns: 20-30
- tools: 8-15
- context: 8K-16K
- verification: minimal
- thinking: OFF by default
- graph: single-loop
- llm_call_cap: 6

### 🔵 MEDIUM+ MODE (Gemma4 27B A4B, cloud models)

- max_turns: 40+
- tools: 20-40
- context: 32K+
- verification: ON
- thinking: AUTO
- llm_call_cap: 15 (Standard) / 40 (Full)

> That's it. No NANO/SMALL/MEDIUM/LARGE explosion.

### AgentMode Enum

```python
class AgentMode(Enum):
    LITE = "lite"           # ≤14B models → SMALL mode
    STANDARD = "standard"   # 14-70B models → MEDIUM+ mode  
    FULL = "full"           # Cloud frontier → MEDIUM+ mode

def select_agent_mode(params_b: float, is_local: bool) -> AgentMode:
    if not is_local:
        return AgentMode.FULL
    if params_b <= 14:
        return AgentMode.LITE
    return AgentMode.STANDARD
```

---

## 5. Context System (Most Important Subsystem)

### Replace:
- Passive token counting
- Percentage thresholds

### With: Active Budget Manager

```python
ContextBudget:
    max_tokens
    reserved_output
    reserved_tools
    
    def admit(messages):
        if overflow:
            return compact(messages)
```

### Context Layers (STRICT ORDER)

1. SYSTEM (SOUL.md)
2. TASK (current user goal)
3. WORKING MEMORY (recent steps)
4. TOOL RESULTS (compressed)
5. LONG-TERM MEMORY (top-K)

### Hard Rules
- Tool output > summaries
- Recent > old
- Code > prose
- Drop > overflow (never OOM)

---

## 6. KV Cache Governor (Non-Optional)

Small models fail from KV blowups, not weights.

```python
if kv_usage > safe_limit:
    trigger_compaction()
```

| Model | Real Bottleneck |
|-------|-----------------|
| Qwen 9B | KV cache |
| Gemma MoE | Memory fragmentation |
| Cloud | Latency |

---

## 7. Tool System

Simplify tool interface:

```json
{
  "name": "read_file",
  "args": {"path": "file.py"}
}
```

Add:
- **Tool Reliability Layer** — if model is small: enforce_strict_schema(), retry_on_invalid()
- **Qwen-specific** — Native XML parser (keep), but normalize internally → JSON

---

## 8. Memory Architecture (Simplified)

### Remove:
- Complex multi-memory abstractions

### Use:
1. **Short-Term (in-context)** — last ~10 steps
2. **Long-Term (vector or hash)** — top-K retrieval only

> Rule: If it's not retrieved, it doesn't exist.

---

## 9. Thinking Mode

Replace heuristic triggers with:

```python
thinking_mode:
    off   → local small models default
    auto  → enabled for multi-step tasks
    on    → forced (debug)
```

**Critical:** Small models should not think by default.

---

## 10. Model Adapter Layer

```python
class ModelAdapter:
    def generate(messages, tools, config):
        ...
```

Supports: Ollama (local), vLLM, OpenAI, Anthropic, Gemini

Must normalize: tool format, token counting, streaming, errors

---

## 11. File System & Tool Runtime

Keep simple and fast:

```python
ToolRuntime:
    - file ops (atomic)
    - git ops
    - shell (sandboxed)
    - LSP (limited concurrency)
```

Add: strict timeouts, output truncation (50KB cap), deterministic errors

---

## 12. What You Should REMOVE or MERGE

### 🔥 Strong Recommendation: Merge

```
TokenBudgetMonitor
auto_compactor
distiller
```

Into ONE: `context_manager.py`

### Delete:
- Tier-specific prompt branches
- Graph node explosion
- Redundant "verification nodes"

---

## 13. What Actually Differentiates Local vs Cloud

| Feature | Local | Cloud |
|---------|-------|-------|
| Context | constrained | large |
| Latency | low | high |
| Cost | free | expensive |
| Reliability | lower | higher |

| Decision | Local | Cloud |
|----------|-------|-------|
| Loop | shallow | deeper |
| Tools | fewer | more |
| Thinking | off | auto |
| Memory | tight | expanded |

---

## 14. Minimal File Structure (Target)

```
core/
  inference/
    model_profile.py       # ModelProfile dataclass
    hardware_profile.py   # HardwareProfile dataclass
    runtime_profile.py    # RuntimeProfile = merge(model, hardware)

  orchestration/
    orchestrator.py       # SINGLE LOOP (replaces graph/)
    workflow_selector.py  # Binary: SMALL vs MEDIUM+
    context_manager.py    # MERGED: TokenBudgetMonitor + auto_compactor + distiller

  tools/
    runtime.py            # Tool execution
    parser.py             # Tool parsing (Qwen XML → JSON)

  memory/
    short_term.py         # In-context working memory
    long_term.py          # Vector or hash-based retrieval

  adapters/
    ollama.py
    openai.py
    anthropic.py
    gemini.py
```

### Current → Target Mapping

| Current | Target |
|---------|--------|
| `src/core/orchestration/graph/` | `orchestrator.py` (single loop) |
| `src/core/orchestration/token_budget.py` | `context_manager.py` |
| `src/core/memory/auto_compactor.py` | `context_manager.py` |
| `src/core/memory/distiller.py` | `context_manager.py` |
| `src/core/orchestration/graph/builder.py` | `workflow_selector.py` |
| `src/core/inference/model_tiers.py` | `model_profile.py` |

---

## 15. Migration Strategy

**DO NOT rewrite everything.**

1. **Introduce runtime_profile** — keep existing system
2. **Add new single_loop_orchestrator** — alongside existing
3. **Route SMALL models → new loop** — gradual migration
4. **Gradually kill old graph** — once validated

---

## 16. The One-Liner Architecture

> A single-loop agent driven by a runtime profile (model × hardware), with strict context budgeting and tool-first execution.

---

## 17. Immediate Wins

### 🚀 Immediate Improvements
- 2-4× lower latency
- Far fewer OOMs
- Better small-model reliability

### 🧠 Structural Improvements
- Easier debugging
- Fewer edge cases
- No tier explosion

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| model_profile.py | TODO | New file |
| hardware_profile.py | TODO | Partial existing |
| runtime_profile.py | TODO | New file |
| orchestrator.py (single loop) | TODO | Major rewrite |
| workflow_selector.py | TODO | Simplify from builder.py |
| context_manager.py | TODO | Merge existing |

---

*Created 2026-04-26*
*This is the target architecture v2 — guides all implementation decisions*