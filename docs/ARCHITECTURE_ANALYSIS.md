# Architecture Analysis: CodingAgent vs OpenCode/Claw-Code

**Date:** 2026-04-27  
**Status:** Complete

---

## 1. Architecture Comparison

| Aspect | CodingAgent | OpenCode | Claw-Code |
|--------|------------|----------|----------|
| **Paradigm** | LangGraph (60 files) | Effect-based (45 dirs) | LangGraph |
| **Session** | SQLite + JSONL | JSON files | SQLite |
| **Schema** | Pydantic | Zod | Pydantic |
| **Tools** | registry_builder | @tool decorators | registry |
| **Memory** | Tiered limits | Simple file | SQLite |

---

## 2. Key Differences Found

### 2.1 Session/Memory

| Feature | CodingAgent | OpenCode |
|---------|------------|---------|
| Dual stores | memory.md + preferences.md | session/*.json |
| FTS5 search | ✅ Implemented | Glob-based |
| Character bounds | 2200 chars | Not enforced |
| Tiered limits | lite/standard/full | N/A |
| Schema versioning | v2 with migrations | Per-file version |

### 2.2 TUI Integration

| Feature | CodingAgent | OpenCode |
|---------|------------|---------|
| Tool icons | Generic | Per-tool icons |
| Diff rendering | Basic | Full diff view |
| Permissions | bash only | allow/deny/ask |

### 2.3 Infrastructure

| Feature | CodingAgent | OpenCode |
|---------|------------|---------|
| Async | asyncio + threading | Effect (Fiber) |
| Event bus | EventBus | Effect context |
| Error handling | Structured errors | NamedError |

---

## 3. Strengths of CodingAgent

1. **Tiered memory** - Different limits for small/medium/large models
2. **FTS5 search** - Fast full-text search in SQLite
3. **Character bounds** - Prevents prompt overflow
4. **Context directory** - `.codingAgent` is project-specific
5. **Thread-local loops** - Efficient async for local models

---

## 4. Improvements to Consider

### HIGH Priority

| # | Improvement | From | Reason |
|---|-------------|------|--------|
| H1 | Tool-specific icons | OpenCode | Better UX |
| H2 | Permission gating | OpenCode | Security |
| H3 | Session commands | OpenCode | UX (/undo, /fork) |

### MEDIUM Priority

| # | Improvement | From | Reason |
|---|-------------|------|--------|
| M1 | Effect-based architecture | OpenCode | Cleaner |
| M2 | Zod schemas | OpenCode | Validation |
| M3 | Diff rendering | OpenCode | UX |

### LOW Priority

| # | Improvement | From | Reason |
|---|-------------|------|--------|
| L1 | Skill system docs | OpenCode | Discovery |
| L2 | Command registry | OpenCode | Extensibility |

---

## 5. Implementation Status (v2 Complete)

✅ **Implemented:**
- Context directory: `.codingAgent`
- Tiered memory (lite/standard/full)
- FTS5 search
- Character bounds (2200 chars)
- Schema versioning
- Thread-local event loops

---

## 6. Next Steps

Based on gap analysis, prioritize:

1. **Permission gating** - High value, medium effort
2. **Tool icons** - High value, low effort  
3. **TUI diff rendering** - High value, medium effort
4. **Effect migration** - Long-term, low priority

---

## 7. Architecture V2 Target

See `docs/ARCHITECTURE_V2.md` for complete target architecture.

### V2 Goals
- Single execution loop (not graph explosion)
- Two-axis adaptation (model × hardware)
- Context as budget, not buffer
- Tools > reasoning for small models
- Deterministic degradation