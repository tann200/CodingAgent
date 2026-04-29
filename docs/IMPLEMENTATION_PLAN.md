# Small Model Optimization: Implementation Plan

**Date:** 2026-04-26
**Focus:** Simplify for small models (Gemma4 27B A4B / Qwen3.5 9B) - "make it smaller, not smarter"
**Inputs:**
- `build-your-own-openclaw` tutorial analysis
- Hardware-model optimization suggestions
- GPT analysis: simplification recommendations
- Current CodingAgent architecture

---

## Executive Summary

**Key Insight:** For small local models, the goal should be "make the system smaller" not "make the system smarter."

### Core Principle: Lite vs Full Agent

| Mode | Model | Philosophy |
|------|-------|-------------|
| **LITE** | Qwen3.5 9B (≤14B) | Fast, minimal, heuristic-driven, 3-5 tools max |
| **STANDARD** | Gemma4 27B A4B (≤70B) | Balanced |
| **FULL** | GPT-4o/Claude | Maximum capability |

---

## Part 1: Core Simplification (NEW)

### 1.1 AgentMode System (P0)

```python
class AgentMode(Enum):
    LITE = "lite"        # local models ≤14B
    STANDARD = "standard"  # local models 14-70B
    FULL = "full"          # cloud frontier

def select_agent_mode(model_params_b: float) -> AgentMode:
    if model_params_b <= 14:
        return AgentMode.LITE
    if model_params_b <= 70:
        return AgentMode.STANDARD
    return AgentMode.FULL
```

### 1.2 Lite Mode Limits (P0)

| Setting | Lite | Standard | Full |
|---------|------|----------|------|
| Max LLM calls/task | **6** | 15 | 40 |
| Nodes | **3-5** | 7-10 | 16 |
| Tools/turn | **3-5** | 8-10 | Unlimited |
| Evaluation node | ❌ OFF | Partial | ✅ ON |
| Replan node | ❌ OFF | Partial | ✅ ON |
| Vector memory | ❌ OFF | Partial | ✅ ON |
| Reasoning budget | low | medium | high |
| Approval gate | minimal | partial | full |

### 1.3 Remove From Plan (Complexity Not Worth It)

These add complexity without benefit for small models:
- ~~Multi-layer prompts (SOUL.md)~~ → REMOVE
- ~~Specialized agent dispatch~~ → REMOVE  
- ~~Cron/scheduled tasks~~ → REMOVE
- ~~Full dual-memory system~~ → REMOVE

---

## Part 2: Gap Analysis

### 1.1 Already Implemented ✅

| Feature | Current State | Notes |
|---------|--------------|-------|
| Tiered model system | `model_tiers.py` with NANO/SMALL/MEDIUM/LARGE/FRONTIER | 187 references in codebase |
| Tier-aware routing | `builder.py` (NANO/SMALL → simplified graph) | ✅ |
| GAP-SMALL-* optimizations | perception_node, execution_node, verification_node | ✅ |
| Tool limits | 8 (NANO), 20 (SMALL), 35 (MEDIUM) | ✅ |
| Fast-mode (reduced tool descriptions) | `context_builder.py` | ✅ |
| Clarification guard | GAP-SMALL-4 for ambiguous tasks | ✅ |
| Simplified output format | GAP-SMALL-1 | ✅ |
| frontier_loop_node | Multi-turn ReAct loop for LARGE/FRONTIER | ✅ Partial |
| Thinking mode | `disable_thinking` flag exists | ✅ Partial |
| Context compaction | `auto_compactor.py`, `distiller.py` | ✅ |
| TokenBudgetMonitor | Percentage thresholds in `token_budget.py` | ✅ Partial |
| tiktoken fallback | `tokenizer.py` with char heuristic | ✅ |

### 1.2 Missing / Gaps 🚫

#### P0 - Critical (SIMPLIFICATION)

| Gap | Description | Priority |
|-----|-------------|----------|
| AgentMode system | Lite/Standard/Full enum + selection logic | P0 |
| Phase-based graph builder | Composable phases instead of fixed graphs | P0 |
| LLM call caps | Lite:6, Standard:15, Full:40 per task | P0 |
| Tool routing by task | Task → 3-5 tools (not 20→15 pruning) | P0 |
| Disable evaluation for Lite | Use tool result heuristics instead | P0 |
| Disable replan for Lite | Replace with retry+hint | P0 |
| Hardware detection | VRAM/CPU auto-detection | P0 |

#### P1 - Quality

| Gap | Description | Priority |
|-----|-------------|----------|
| Reasoning budget | Replace thinking on/off with budget levels | P1 |
| Lite memory collapse | Disable vector memory, max 2 memories | P1 |
| Simplify safety for Lite | Keep only path containment + basic bash | P1 |
| Tool parser (Qwen3) | XML format support | P1 |
| Latency budget | Max 3s Lite, 8s Standard | P1 |
| Slash commands (/compact, /context) | build-your-own-openclaw | P2 |
| Specialized agent dispatch | build-your-own-openclaw | P2 |
| Cron/scheduled tasks | build-your-own-openclaw | P3 |

### 1.3 Counterarguments & Refinements

| Suggestion | Counterargument | Refined Approach |
|------------|-----------------|------------------|
| Replace tiktoken with HuggingFace entirely | Heavy dependency, slow startup | Detect Qwen3 → use HF; keep tiktoken for OpenAI |
| Separate 5-node graph class | Refactoring complexity | Extend `frontier_loop_node` to LOCAL tier |
| Full hardware_profile.py | Overkill for MVP | Start with VRAM detection, CPU later |

---

## Part 2: Model-Specific Configurations (PRIMARY TARGETS)

### Gemma4 27B A4B (PRIMARY - MoE)

| Setting | Value | Reason |
|---------|-------|--------|
| Parameters | 27B total / 4B active | MoE architecture |
| Weights | ~13GB (Q4) | Fits on 16GB VRAM |
| Max tools | 25 | MEDIUM tier |
| Max turns | 40 | Token budget |
| Tool format | JSON | Full model capability |
| Verification | Full | MEDIUM tier |
| Memory context | 10 items | Larger context |
| safe_context_tokens | 32768 | ~13GB weights + 3.2GB KV |
| Architecture | moe | Affects KV calculation |
| native_tool_format | gemma4 | |

### Qwen3.5 9B (PRIMARY - GDN)

| Setting | Value | Reason |
|---------|-------|--------|
| Parameters | 9B | Dense |
| Weights | ~5.5GB (Q4) | Fits easily on 16GB |
| Max tools | 15 | SMALL tier |
| Max turns | 25 | Token budget |
| Tool format | YAML/JSON | Local capability |
| Verification | Writes only | Read-only skip |
| Memory context | 5 items | Context limit |
| safe_context_tokens | 16384 | ~5.5GB weights + 1.6GB KV |
| Architecture | gdn | Gradient Deferred Notification |
| native_tool_format | qwen3 | |

### Qwen3-14B-Q4 (Hybrid MEDIUM/SMALL)

| Setting | Value | Reason |
|---------|-------|--------|
| Weights | 8.2GB | Q4_K_M quantization |
| KV cache | ~100MB/1K tokens | |
| safe_context_tokens | 32768 | 8.2 + 3.2 + 1.2 = 12.6GB |
| max_context_tokens | 65536 | Emergency only |
| Capability tier | MEDIUM | ~32B reasoning quality |
| Inference tier | SMALL | 35 tok/sec local |
| Graph type | collapsed_5node | Not 16-node |

---

## Part 3: Implementation Roadmap

### Phase 1: Critical (Week 1) — Prevent OOM & Fix Latency

| # | Task | File(s) | Priority | Source |
|---|------|---------|----------|--------|
| 1.1 | Create hardware_profile.py | NEW | P0 | HW-Profile |
| 1.2 | Enable frontier_loop_node for LOCAL | `graph/builder.py` | P0 | HW-Profile |
| 1.3 | VRAM-aware context negotiation | `token_budget.py` | P0 | HW-Profile |
| 1.4 | Add --thinking CLI flag | `main.py`, adapters | P1 | Both |

### Phase 2: Quality (Week 2) — Tool Calling & Memory

| # | Task | File(s) | Priority | Source |
|---|------|---------|----------|--------|
| 2.1 | KV cache governor | NEW | P1 | HW-Profile |
| 2.2 | Qwen3 native tool parser | `tool_parser.py` | P1 | HW-Profile |
| 2.3 | SKILL.md loader | NEW | P1 | build-your-own |
| 2.4 | Tier-aware memory limiting | `memory_tools.py` | P1 | build-your-own |

### Phase 3: Optimization (Week 3) — Hardware Utilization

| # | Task | File(s) | Priority | Source |
|---|------|---------|----------|--------|
| 3.1 | CPU-aware LSP governor | `lsp_manager.py` | P2 | HW-Profile |
| 3.2 | HuggingFace tokenizer | `tokenizer.py` | P2 | HW-Profile |
| 3.3 | Embedding cache for 64GB RAM | `vector_store.py` | P2 | HW-Profile |

**Note:** Vector store is already dependency-free (SHA256-based stub embeddings). "RAM optimization" means caching computed embeddings in memory rather than recomputing on each query.
| 3.4 | CPU offload strategy | `orchestrator_bootstrap.py` | P2 | HW-Profile |

### Phase 4: Polish (REMOVED for small models ❌)

> **Decision:** For small local models, these add complexity without benefit. Removed from plan.

| # | Task | File(s) | Reason Removed |
|---|------|---------|----------------|
| 4.1 | ~~Slash commands~~ | NEW | ❌ Not needed for CLI-first |
| 4.2 | ~~Multi-layer prompts (SOUL.md)~~ | NEW | ❌ Overhead for 9B models |
| 4.3 | ~~Specialized agent dispatch~~ | `subagent_tools.py` | ❌ Too complex, single agent |
| 4.4 | ~~Qwen3/Gemma4 tests~~ | tests/ | ✅ Keep but defer |
| 4.5 | ~~LOCAL_SETUP.md documentation~~ | docs/ | ✅ Keep for setup docs |

### Phase 4: Polish (RETAINED for small models ✅)

| # | Task | File(s) | Priority |
|---|------|---------|----------|
| 4.1 | E2E tests (basic) | tests/e2e/test_small_model_pipeline.py | P2 |
| 4.2 | LOCAL_SETUP.md | docs/LOCAL_SETUP.md | P2 |

---

## Part 4: Detailed Implementation Plans

### 4.1 Hardware Profile (P0)

```python
# src/core/inference/hardware_profile.py
HARDWARE_PROFILES = {
    "auto": {
        "gpu_vram_gb": None,  # Detected via nvidia-smi
        "system_ram_gb": None,  # Detected via psutil
        "cpu_cores": None,
        "models": {}
    },
    "workstation-5070ti-16g-64g": {
        "gpu_vram_gb": 16,
        "gpu_bandwidth_gbps": 896,
        "system_ram_gb": 64,
        "cpu_cores": 6,  # 5600X
        "cpu_threads": 12,
        "models": {
            "qwen3-14b-q4": {
                "weights_gb": 8.2,
                "kv_per_1k_tokens_mb": 100,
                "safe_context_tokens": 32768,
                "max_context_tokens": 65536,
                "throughput_tok_sec": 35,
                "native_tool_format": "qwen3",
            },
            "qwen3-14b-q6": {
                "weights_gb": 11.5,
                "safe_context_tokens": 8192,
                "max_context_tokens": 16384,
                "throughput_tok_sec": 22,
            },
            "gemma4-27b": {
                "weights_gb": 14,
                "safe_context_tokens": 32768,
                "max_context_tokens": 131072,
                "throughput_tok_sec": 15,
            }
        }
    }
}

def detect_hardware() -> dict:
    """Auto-detect hardware profile via nvidia-smi + CPU info."""
    ...

def get_model_profile(model_id: str) -> dict:
    """Get model-specific profile from hardware config."""
    ...
```

### 4.2 Token Budget VRM-Aware (P0)

```python
# Modify TokenBudgetMonitor
class TokenBudgetMonitor:
    def __init__(self, hardware_profile=None):
        self.profile = hardware_profile or {}
        
    def set_context_limit(self, requested_tokens: int) -> int:
        if not self.profile:
            return requested_tokens
        max_safe = self.profile.get("safe_context_tokens", 128000)
        if requested_tokens > max_safe:
            logger.warning(f"Context capped to {max_safe}")
            return max_safe
        return requested_tokens
    
    def reserve_tool_output_budget(self) -> int:
        safe = self.profile.get("safe_context_tokens", 128000)
        return min(4096, safe // 4)
```

### 4.3 Enable frontier_loop_node for LOCAL (P0)

```python
# graph/builder.py
def _is_local_model(state: Mapping[str, Any]) -> bool:
    """Check if model should use collapsed graph."""
    model = (state.get("model") or "").lower()
    if any(m in model for m in ["qwen3", "llama", "gemma"]):
        if "api" not in model:
            return True
    return False

def _should_use_collapsed_graph(state):
    return _is_local_model(state) or _is_large_or_frontier(state)
```

### 4.4 Qwen3 Native Tool Parser (P1)

```python
# tool_parser.py
class Qwen3ToolParser(ToolParser):
    def parse(self, raw_output: str) -> list[ToolCall]:
        if "<tool_call>" in raw_output or "<tool_calls>" in raw_output:
            return self._parse_xml_tool_calls(raw_output)
        return super().parse(raw_output)
    
    def _parse_xml_tool_calls(self, raw: str) -> list[ToolCall]:
        # Parse XML tool calls from Qwen3 native format
        ...
```

### 4.5 Thinking Mode CLI (P1)

```python
# main.py
parser.add_argument("--thinking", choices=["auto", "on", "off"], default="auto")

# In adapter - auto detection
def should_think(self, messages) -> bool:
    if self.thinking_mode == "on":
        return True
    if self.thinking_mode == "off":
        return False
    # Auto: enable for planning/debugging
    last_user = get_last_user_msg(messages)
    triggers = ["refactor", "debug", "architecture", "design", "plan"]
    return any(t in last_user.lower() for t in triggers)
```

### 4.6 SKILL.md Loader (P1)

```
src/core/skills/
├── skill_loader.py      # Load SKILL.md files
├── skill_registry.py     # Track available skills
└── skills/              # User-defined skills
```

### 4.7 Slash Commands (P2)

| Command | Purpose | Priority |
|---------|---------|----------|
| `/compact` | Force context compaction | HIGH |
| `/context` | Show token usage | HIGH |
| `/agents` | List available agents | MEDIUM |
| `/skills` | List available skills | MEDIUM |
| `/reset` | Reset session | MEDIUM |

### 4.8 KV Cache Governor (P1)

```python
# src/core/inference/kv_cache_governor.py
class KVCacheGovernor:
    def __init__(self, max_vram_gb: float, model_weights_gb: float):
        self.max_kv_gb = max_vram_gb - model_weights_gb - 1.5
    
    def on_context_growth(self, current_tokens: int, incoming: int) -> Action:
        projected = self.estimate_kv_gb(current_tokens + incoming)
        if projected > self.max_kv_gb:
            return Action.COMPACT
        return Action.CONTINUE
```

---

## Part 5: Files to Create/Modify

### Create (NEW)

| File | Priority | Description |
|------|----------|-------------|
| `src/core/inference/hardware_profile.py` | P0 | VRAM/CPU detection |
| `src/core/inference/kv_cache_governor.py` | P1 | VRAM monitoring |
| `src/core/skills/skill_loader.py` | P1 | SKILL.md loading |
| `src/core/skills/skill_registry.py` | P1 | Skill registry |
| `src/tools/slash_commands.py` | P2 | Slash commands |
| `src/core/prompts/prompt_builder.py` | P2 | Multi-layer prompts |

### Modify (Existing)

| File | Changes | Priority |
|------|---------|----------|
| `src/core/orchestration/graph/builder.py` | Enable collapsed graph for local | P0 |
| `src/core/orchestration/token_budget.py` | VRAM-aware negotiation | P0 |
| `src/core/inference/adapters/ollama_adapter.py` | Thinking mode toggle | P1 |
| `src/main.py` | --thinking flag | P1 |
| `src/core/orchestration/tool_parser.py` | Qwen3 native parser | P1 |
| `src/tools/memory_tools.py` | Tier-aware memory limiting | P1 |
| `src/core/indexing/lsp_manager.py` | CPU-aware LSP governor | P2 |
| `src/core/orchestration/message_manager.py` | HF tokenizer for Qwen3 | P2 |
| `src/core/memory/vector_store.py` | RAM-optimized storage | P2 |

---

## Part 6: Backward Compatibility

All changes must:
- ✅ Not break existing FRONTIER/LARGE model workflows
- ✅ Not change tool API surface
- ✅ Pass existing test suite
- ✅ Maintain optional dependencies (tiktoken still optional for OpenAI)

---

## Part 7: Success Metrics

| Model | Task | Target | Metric |
|-------|------|--------|--------|
| Qwen3-14B-Q4 | Simple file edit | >85% | Task completion |
| Qwen3-14B-Q4 | Multi-step coding | <60s | Turn latency |
| Qwen3-14B-Q4 | 32K context | No OOM | Stability |
| Gemma4 4B | Simple task | >80% | Task completion |
| Gemma4 4B | 15 turns | <5s | Turn latency |

---

## Part 8: Testing Plan

### Unit Tests
- `test_hardware_profile.py` - VRAM detection
- `test_token_budget.py` - VRAM-aware limits
- `test_qwen3_tool_parser.py` - XML parsing
- `test_skill_loader.py` - SKILL.md parsing
- `test_model_tiers.py` - Tier classification

### Integration Tests
- `test_frontier_loop_local.py` - Collapsed graph for local models
- `test_thinking_mode.py` - Auto/on/off modes
- `test_skill_integration.py` - Skill loading

### E2E Tests
- `test_qwen3_coding_task.py` - Full pipeline with Qwen3
- `test_gemma4_simple_task.py` - Full pipeline with Gemma4

---

## Part 9: Task Template & Guidelines

### Standard Task Template

Each task should include:

```markdown
### Task: [Name]

**File(s):** `src/path/to/file.py`
**Priority:** P0/P1/P2
**Effort:** 1h/2h/4h

**Description:**
- What the task does

**Implementation:**
```python
# Pseudocode or key implementation details
```

**Acceptance Criteria:**
- [ ] Code compiles: `python -m py_compile src/path/to/file.py`
- [ ] Unit tests pass: `pytest tests/unit/test_xxx.py`
- [ ] Integration tests pass (if applicable)
- [ ] Updated `docs/codingagent-architecture.md` if API changed
- [ ] Added/updated test coverage for new functionality

**Documentation Updates Required:**
- [ ] Update architecture doc if new files/modules added
- [ ] Update IMPLEMENTATION_PLAN.md checklist
- [ ] Add to CHANGELOG if user-facing
```

---

### Post-Implementation Checklist

After completing ANY code change:

- [ ] Run `python -m py_compile <changed_file.py>` — must pass
- [ ] Run relevant unit tests — must pass
- [ ] Run `grep -R "exc_info=True"` — ensure no bad patterns
- [ ] Run `grep -R "write_text("` — ensure atomic patterns used
- [ ] Update `docs/codingagent-architecture.md` if new modules/APIs added
- [ ] Update this IMPLEMENTATION_PLAN.md checklist
- [ ] Run `pytest --collect-only` to verify test count

---

## Part 10: Checklist

### Phase 0: Architecture V2 Spec ✅
- [x] **ARCHITECTURE_V2.md** — Single-loop architecture spec
  - File: `docs/ARCHITECTURE_V2.md`
  - Status: CREATED 2026-04-26
  - Key changes: single loop, binary workflow (SMALL/MEDIUM+), merge context managers

### Phase 0: Two-Axis Profiling ✅
- [x] **model_capability_profile.py** — ModelProfile dataclass + AgentMode
  - File: `src/core/inference/model_capability_profile.py`
  - Test: `tests/unit/test_model_capability_profile.py` (TODO)
  - Primary profiles: gemma-4-27b-a4b, qwen3.5-9b, qwen3-14b, gemma-4-4b
  - Status: CREATED 2026-04-26
- [x] **hardware_capability_profile.py** — HardwareProfile + VRAM detection
  - File: `src/core/inference/hardware_capability_profile.py`
  - Test: `tests/unit/test_hardware_capability_profile.py` (TODO)
  - Detection: nvidia-smi (Linux), sysctl (Mac), kernel32 (Windows)
  - Profiles: rtx5070ti-16g, rtx4080-16g, m4-mac-*, cloud
  - Status: CREATED 2026-04-26
- [x] **runtime_profile.py** — Merge model + hardware into RuntimeProfile
  - File: `src/core/inference/runtime_profile.py`
  - Test: `tests/unit/test_inference_v2.py` (22 tests, PASSING)
  - Drives: context size, tool count, loop depth, thinking mode
  - Status: CREATED 2026-04-26
- [x] **workflow_selector.py** — Binary workflow selection (SMALL/MEDIUM+)
  - File: `src/core/inference/workflow_selector.py`
  - Test: `tests/unit/test_inference_v2.py` (included)
  - Workflow types: SINGLE_LOOP (Lite), FRONTIER_LOOP (Standard/Full)
  - Status: CREATED 2026-04-26
- [x] **context_manager.py** — Merge token_budget + auto_compactor
  - File: `src/core/orchestration/context_manager.py`
  - Features: budget tracking, deterministic compaction, admit_messages()
  - Status: CREATED 2026-04-26
- [x] **Tokenizer router** — HF for Qwen3/Gemma, tiktoken for OpenAI
  - File: `src/core/inference/tokenizer.py` (✅ IMPLEMENTED)
- [x] **--thinking CLI flag** — `--thinking auto|on|off`
  - File: `src/main.py`, `src/core/inference/thinking_utils.py` (✅ IMPLEMENTED)

### Hardware Profile Analysis (Current Implementation Fit)

| Profile | Current Support | Required Changes |
|---------|-----------------|------------------|
| **rtx5070ti-16g** (PRIMARY) | ⚠️ No detection | Add nvidia-smi detection |
| **gemma-4-27b-a4b** (local target) | ⚠️ Partial | Add profile, optimize for MoE |
| **qwen3.5-9b** (local target) | ⚠️ Partial | Add profile, optimize for GDN |
| **cloud-frontier** | ✅ Existing | Works with GPT-4o, Claude, Gemini |

#### Primary Local Targets

| Model | Parameters | Quantization | VRAM Usage | Context | Tier |
|-------|------------|--------------|-------------|---------|------|
| **gemma-4-27b-a4b** | 27B (4B active) | Q4 | ~13GB | 32K | MEDIUM |
| **qwen3.5-9b** | 9B | Q4 | ~5.5GB | 16K | SMALL |

**Note:** Gemma 4 27B A4B is a MoE (Mixture of Experts) model with 4B active parameters - classifies as MEDIUM tier despite large total parameters.

#### Graph Type Support (Existing)

| Graph Type | Current | Proposed |
|------------|---------|----------|
| react_3node | ❌ Missing | Small + slow hardware |
| collapsed_5node | ⚠️ Partial | Small + fast hardware |
| frontier_8node | ✅ Exists | Medium+/LARGE |
| full_16node | ✅ Exists | Standard pipeline |

#### Context Limit Support

| Model | Current Limit | Calculated |
|-------|---------------|------------|
| Qwen3.5 9B Q4 | ~16K (percentage) | 16K (5.5GB weights + 1.6GB KV) |
| Qwen3-14B Q4 | ~32K (percentage) | 32K (8.2GB weights + 3.2GB KV) |
| Gemma4 27B Q4 | ~16K | 16K (13GB weights + 1.6GB KV) |
| GPT-4o/Claude | ~128K+ | Unlimited (cloud) |

**Conclusion:** Hardware profiles are viable and complementary to existing tier system. No breaking changes required.

### Frontier Model Support (Existing)

The system already supports frontier models via:

| Frontier Model | Current Implementation |
|---------------|----------------------|
| GPT-4o/Claude/Gemini | `FRONTIER` tier + `frontier_loop_node` |
| Gemma4 31B | `FRONTIER` tier (fits 16GB in Q4) |
| Gemma4 26B A4B | `MEDIUM` tier (4B active, 77% LCB) |

**WorkflowSelector Integration:**

```python
# Binary split logic (extends existing _is_large_or_frontier())
if model.capability_tier in ("nano", "small"):
    return _small_workflow()  # collapsed_5node or react_3node
else:
    return _medium_frontier_workflow()  # frontier_8node or full_16node
```

This extends existing `_is_large_or_frontier()` logic without breaking changes.

### Phase 1: Critical ✅
- [x] **Workflow-driven graph compilation** — Enable workflow-based graph selection
  - File: `src/core/orchestration/graph/builder.py`
  - Changes: Added `_is_lite_mode()`, `_compile_lite_graph()`, v2 workflow_selector import
  - Status: INTEGRATED 2026-04-26
- [x] **VRAM-aware context** — TokenBudget respects safe_context_tokens
  - File: `src/core/orchestration/token_budget.py` (existing)
  - Integration: runtime_profile.safe_context_tokens flows to token_budget via state["_context_budget"]
  - Status: INTEGRATED 2026-04-26

### Phase 2: Quality ✅
- [x] **kv_cache_governor.py** — VRAM monitoring
  - File: `src/core/inference/kv_cache_governor.py`
  - Features: KVCacheState, CompactionAction enum, create_governor_for_model()
  - Status: CREATED 2026-04-26
- [x] **Qwen3 tool parser** — XML format support
  - File: `src/core/orchestration/tool_parser.py`
  - Added: `_parse_qwen3_xml()` for `<tool_call><name>...<arguments>...</arguments></tool_call>`
  - Status: CREATED 2026-04-26
- [x] **--thinking CLI flag** — Thinking mode control
  - File: `src/main.py`
  - Added: `--thinking auto|on|off` flag
  - File: `src/core/inference/thinking_utils.py`
  - Added: `ThinkingMode` enum, `resolve_thinking_mode()`, `get_thinking_directive()`
  - Status: CREATED 2026-04-26

### Phase 3: Optimization ✅
- [x] **CPU-aware LSP governor** — Max 2 concurrent LSPs
  - File: `src/core/indexing/lsp_manager.py`
  - Added: `max_concurrent` parameter, `_get_default_max_lsps()`
  - Status: CREATED 2026-04-26
- [x] **HuggingFace tokenizer** — For Qwen3/Gemma
  - File: `src/core/inference/tokenizer.py`
  - Added: `_get_hf_tokenizer()`, `_hf_model_name()`, updated count_tokens()
  - Status: CREATED 2026-04-26
- [x] **64GB RAM optimization** — LRU embedding cache
  - File: `src/core/indexing/vector_store.py`
  - Added: `_EMBEDDING_CACHE`, `_get_cached_embedding()`, `clear_embedding_cache()`
  - Status: CREATED 2026-04-26

### Phase 4: Polish
- [x] ~~**slash_commands.py** — /compact, /context, /agents, /skills~~
  - **STATUS:** ❌ REMOVED — not CLI-first priority (commands in commands.py instead)
- [x] ~~**multi-layer prompts** — SOUL.md personality layer~~
  - **STATUS:** ❌ REMOVED — token overhead for small models
- [x] ~~**Agent dispatch** — Lightweight subagent routing~~
  - **STATUS:** ❌ REMOVED — too complex for single-agent workflow
- [x] **E2E tests** — Qwen3/Gemma4 pipelines
  - File: `tests/e2e/test_small_model_pipeline.py` (✅ IMPLEMENTED)
- [x] **LOCAL_SETUP.md** — Documentation
  - File: `docs/LOCAL_SETUP.md` (✅ IMPLEMENTED)

### Phase 5: Context & Compaction ✅
- [x] **OP-2: Structured compaction format** — Already exists in auto_compactor.py
  - File: `src/core/memory/auto_compactor.py`
  - Features: `<summary>` block format, structured sections, format_compact_summary()
  - Status: EXISTING 2026-04-26
- [x] **OP-9: Tool output truncation 50KB** — Already exists in execution_node.py
  - File: `src/core/orchestration/graph/nodes/execution_node.py`
  - Features: _truncate_tool_output(), 50KB limit, _TOOL_OUTPUT_MAX_BYTES
  - Status: EXISTING 2026-04-26

### Phase 8: Permissions & Commands (PARTIAL)
- [x] **TASK-PERM-1: Per-tool permission policy** — Already exists
  - File: `src/tools/tools_config.py`
  - Features: PermissionLevel enum, TOOL_PERMISSIONS dict, set_tool_permission()
  - Status: EXISTING 2026-04-26
- [x] **TASK-PERM-2: Reject-with-feedback** — ✅ FIXED vol25
- [x] **TASK-PERM-3: Allow always glob patterns** — ✅ FIXED vol25
- [x] **OP-3**: PRUNE_PROTECT = 40K tokens
  - File: `src/core/orchestration/graph/nodes/perception_node.py` (✅ IMPLEMENTED)
- [x] **OP-4**: Overflow detection using real limits
  - File: `src/core/orchestration/token_budget.py` (✅ IMPLEMENTED)
- [x] **OP-9**: Tool output truncation 50KB
  - File: `src/core/orchestration/graph/nodes/execution_node.py` (✅ IMPLEMENTED)
- [x] **OP-10**: Protected tool outputs in compaction
  - File: `src/core/memory/auto_compactor.py` (✅ IMPLEMENTED)

### Phase 6: Project Config & TODO
- [x] **OP-5**: Project config JSON
  - File: `src/core/orchestration/project_settings.py` (✅ IMPLEMENTED)
- [x] **OP-7**: TODO injection in perception
  - File: `src/core/context/context_builder.py` (✅ IMPLEMENTED)
- [x] **Config hot-reload** — File watching for settings changes
  - File: `src/core/orchestration/project_settings.py` (✅ IMPLEMENTED)

### Phase 7: Prompt & Personality (REMOVED ❌)

> **Decision:** Multi-layer prompts and personality layers add token overhead that hurts small models.

| Task | Reason Removed |
|------|----------------|
| ~~SOUL.md personality layer~~ | ❌ Token overhead, small models need simplicity |
| ~~FRONTIER reflection loop~~ | ❌ Overhead for Lite mode |

### Phase 7: Prompt & Personality (REMOVED ❌)
- [x] ~~**SOUL.md**: Personality layer~~
  - **STATUS:** ❌ REMOVED — token overhead, small models need simplicity
- [x] ~~**OP-11**: FRONTIER reflection loop~~
  - **STATUS:** ❌ REMOVED — overhead for Lite/Standard modes

### Phase 8: Permissions & Commands
- [x] **TASK-PERM-1**: Per-tool permission policy
  - File: `src/core/orchestration/permission_gateway.py`
- [x] **TASK-PERM-2**: Reject-with-feedback
  - File: `src/tools/_approval.py`
- [x] **TASK-PERM-3**: Allow always glob patterns
  - File: `src/core/orchestration/permission_table.py` (TUI implemented)
- [x] **TASK-CMD-1**: /undo command
  - File: `src/core/orchestration/commands.py`
- [x] **TASK-CMD-3**: /diff improvements
  - File: `src/core/orchestration/commands.py`
- [x] **TASK-CMD-4**: /context token visualization
  - File: `src/core/orchestration/commands.py`

### Phase 9: Operations & Memory (PARTIAL)
- [x] ~~**Dual memory**: MEMORY.md + USER.md~~
  - **STATUS:** ❌ REMOVED — too complex, single memory sufficient
- [x] ~~**CRON jobs**: File-based configuration~~
  - **STATUS:** ❌ REMOVED — not needed for local models
- [x] **Config hot-reload** — File watching for settings changes
  - File: `src/core/orchestration/project_settings.py` (✅ IMPLEMENTED)

### Phase 10: Loop Detection & RBW Fixes
- [x] **P2-C**: Pre-populate `_session_read_files`
  - File: `src/core/orchestration/task_lifecycle.py` (already implemented)
- [x] **P2-D**: Canonical fingerprints
  - File: `src/core/orchestration/loop_guards.py` (already implemented)
- [x] **P2-D**: Alternating loop detection
  - File: `src/core/orchestration/loop_guards.py` (already implemented)

### Phase 11: TUI Enhancements (Most FIXED per audit-report-vol25)
- [x] **TASK-TUI-1**: Per-tool icons
  - File: `tui/src/ui/components/inline_tool.py` (✅ IMPLEMENTED)
- [x] **TASK-TUI-2**: Inline diff view
  - File: `tui/src/ui/components/diff_viewer.py` (✅ FIXED vol25)
- [x] **TASK-TUI-3**: Bash block expandable
  - File: `tui/src/ui/components/bash_block.py` (✅ IMPLEMENTED in app.py)
- [x] **TASK-TUI-4**: Interactive TodoWrite
  - File: `tui/src/ui/components/todo_list.py` (✅ IMPLEMENTED in app.py)
- [x] **TASK-TUI-6**: Live subagent progress
  - File: `tui/src/ui/components/subagent_progress.py` (✅ IMPORTED vol25)
- [x] **TASK-TUI-7**: Permissions counter
  - File: `tui/src/ui/app.py` (✅ FIXED)
- [x] **TASK-TUI-8**: LSP/MCP counts
  - File: `tui/src/ui/app.py` (✅ FIXED)
- [x] **TASK-TUI-9**: Compaction divider
  - File: `tui/src/ui/app.py` (✅ FIXED)

### Phase 12: OpenCode Gap Parity (Most FIXED per audit-report-vol25)
- [x] **GAP-TUI-2**: Inline diff (HIGH) — ✅ FIXED vol25
- [x] **GAP-PERM-2**: Reject-with-feedback (HIGH) — ✅ FIXED vol25
- [x] **GAP-PERM-3**: Allow always glob (MEDIUM) — ✅ FIXED vol25
- [x] **GAP-FOOTER-3**: Subagent footer nav (LOW) — ✅ FIXED vol25
- [x] **GAP-CMD-2**: /share (LOW) — ✅ FIXED vol25
- [x] **GAP-CMD-3**: /rename (LOW) — ✅ FIXED vol25
- [x] **GAP-WORKTREE-1**: Git worktree (LOW) — ✅ FIXED vol25
- [x] **GAP-CONFIG**: TUI options (LOW) — ✅ FIXED vol25

---

## Part 11: Additional Open Tasks Summary

### From IMPROVEMENT_PLAN_OPENCLAW.md (OPEN items — filtered for small models)

| ID | Task | Category | Priority | Small Model? |
|----|------|----------|----------|--------------|
| TASK-PERM-1 | Per-tool permission policy | Permissions | HIGH | ✅ Keep |
| TASK-PERM-2 | "Reject with feedback" flow | Permissions | MEDIUM | ✅ Keep |
| TASK-PERM-3 | "Allow always" glob patterns | Permissions | MEDIUM | ✅ Keep |
| TASK-CMD-1 | /undo command | Commands | HIGH | ✅ Implemented |
| TASK-CMD-3 | /diff improvements | Commands | MEDIUM | ✅ Implemented |
| TASK-CMD-4 | /context token visualization | Commands | LOW | ✅ Implemented |
| TASK-OPS-3 | Dual memory architecture | Operations | MEDIUM | ❌ REMOVED |
| TASK-OPS-4 | CRON.md file-based jobs | Operations | LOW | ❌ REMOVED |
| TASK-OPS-1 | Config deep-merge hot-reload | Operations | MEDIUM | ✅ Defer |
| TASK-TUI-1 | Per-tool inline icons | TUI | MEDIUM | ✅ Keep |
| TASK-TUI-2 | Inline diff view | TUI | MEDIUM | ✅ Keep |
| TASK-TUI-4 | Interactive TodoWrite list | TUI | MEDIUM | ✅ Keep |
| TASK-TUI-7 | Permissions counter in footer | TUI | LOW | ✅ Keep |
| TASK-TUI-8 | LSP/MCP counts in footer | TUI | LOW | ✅ Keep |

### From agent-loop-improvement-analysis.md (OPEN items)

| ID | Task | Priority |
|----|------|----------|
| P2-C | Pre-populate `_session_read_files` with agent-internal files | HIGH |
| P2-D | Canonical fingerprints for doom-loop detection | MEDIUM |
| P2-D | Detect alternating loops | MEDIUM |

### From gap-analysis-opencode-vs-codingagent-v2.md (OPEN items)

| ID | Task | Priority | Small Model? |
|----|------|----------|--------------|
| GAP-TUI-2 | Inline diff view (deferred) | HIGH | ✅ Keep |
| GAP-PERM-2 | Reject-with-feedback text input | HIGH | ✅ Keep |
| GAP-PERM-3 | "Allow always" with glob patterns | MEDIUM | ✅ Keep |
| GAP-FOOTER-3 | Subagent footer navigation | LOW | ✅ Keep |
| GAP-CMD-2 | /share /unshare | LOW | ❌ REMOVED |
| GAP-CMD-3 | /rename session title | LOW | ❌ REMOVED |
| GAP-WORKTREE-1 | Git worktree isolation for subagents | LOW | ❌ REMOVED |
| GAP-CONFIG-1 | tui.diff_style option | LOW | ✅ Keep |
| GAP-CONFIG-2 | tui.scroll_speed option | LOW | ✅ Keep |
| GAP-CONFIG-3 | conceal toggle for secrets | LOW | ✅ Keep |

---

*Merged from:*
- *build-your-own-openclaw analysis (SKILL.md, slash commands, agent dispatch)*
- *Hardware-model suggestions (VRAM-aware context, workflow selection)*
- *tiered-model-redesign-plan.md (compaction, TODO injection)*
- *IMPROVEMENT_PLAN_OPENCLAW.md (TUI, permissions, commands)*
- *agent-loop-improvement-analysis.md (loop detection, RBW fixes)*
- *gap-analysis-opencode-vs-codingagent-v2.md (TUI parity, OpenCode gaps)*
*Created 2026-04-26*
*See docs/codingagent-architecture.md for current architecture (v1)*
*See docs/ARCHITECTURE_V2.md for target architecture (v2 — single loop)*
*See docs/audit/audit-report-vol33.md for latest audit*

---

## Simplification Summary: Removed for Small Models

**Principle:** "For small local models, make the system smaller, not smarter."
**Target:** See `docs/ARCHITECTURE_V2.md` for full v2 architecture spec.

### Removed Features (Complexity Not Worth It)

| Feature | Original Phase | Reason |
|---------|----------------|--------|
| Multi-layer prompts (SOUL.md) | Phase 4/7 | Token overhead, small models need simplicity |
| Specialized agent dispatch | Phase 4 | Too complex for single-agent workflow |
| Slash commands | Phase 4 | Not needed for CLI-first workflow |
| Dual memory (MEMORY.md + USER.md) | Phase 9 | Too complex, single memory sufficient |
| Cron/scheduled tasks | Phase 9 | Not needed for local models |
| FRONTIER reflection loop | Phase 7 | Overhead for Lite/Standard modes |

### Retained Features (Even for Small Models)

| Feature | Reason |
|---------|--------|
| Atomic writes | Reliability, no overhead |
| Hardware profile detection | Essential for VRAM management |
| Phase-based graph builder | Simplifies instead of complicates |
| Tool routing by task | Reduces tool count, improves accuracy |
| Basic E2E tests | Validation without complexity |
| LOCAL_SETUP.md docs | Essential for onboarding |

---

## Nano Models & Workflows

### Model Tiers

CodingAgent defines 4 tiers based on hardware constraints:

| Tier | VRAM | Context | Example Models | Use Case |
|------|-----|--------|--------------|------------|
| **SMALL** | 6-8GB | 16K-128K | Qwen3 9B, Gemma E4B | Quick edits |
| **MEDIUM** | 18GB | 256K | Gemma 4 26B A4B ⭐ | **Best value** |
| **LARGE** | 20GB+ | 32K-256K | Qwen3 14B, Gemma 31B | Complex tasks |
| **FRONTIER** | Cloud | 200K+ | Claude, Gemini Ultra | Maximum capability |

**Gemma 4 26B A4B** is a Mixture-of-Experts (MoE) model:
- 26B total / 3.8B active parameters
- 256K context (max!)
- ~40 tok/s speed (same as 4B model)
- ~18GB VRAM with Q4 quantization

### Workflows by Tier

Each tier uses a different agent loop pattern optimized for its constraints:

```
SMALL  → SingleLoop (1 iteration, fast feedback)
MEDIUM+ → FrontierLoop (continuous until done)
```

**SingleLoop (SMALL):**
- perception → execution → verification in one pass
- Best for: quick fixes, single-file edits
- Max turns: 25

**FrontierLoop (MEDIUM+):**
- perception → execution → verification → replan
- Loops until verification passes or max turns
- Best for: complex multi-step tasks

### Workflow Selection

The system automatically selects based on detected VRAM:

```python
from src.core.inference.workflow_selector import select_workflow, WorkflowType

# Auto-detect hardware and select workflow
workflow = select_workflow(hardware_profile)
# Returns: SINGLE_LOOP or FRONTIER_LOOP
```

### Small Model Optimizations

For SMALL tier (8GB VRAM):
- Standard context (16K tokens)
- All tools enabled
- Optional thinking
- Normal truncation (60 lines)

### Lite Mode vs Full Mode

| Setting | Lite (≤14B) | Full (Frontier) |
|---------|-------------|-----------------|
| Max LLM calls | 6 | 40 |
| Tools/turn | 3-5 | Unlimited |
| Evaluation node | ❌ | ✅ |
| Replan node | ❌ | ✅ |
| Vector memory | ❌ | ✅ |
| Prompt layers | 1 | 3+ |