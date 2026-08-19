# Local Setup Guide

**Date:** 2026-04-26

This guide covers setting up CodingAgent with local models (no cloud API required).

---

## Hardware Requirements

| Model | VRAM (Q4) | RAM | Context | Speed |
|-------|------------|-----|--------|-------|
| **Qwen3.5-9B** | 7GB | 16GB | **262K** ⭐ | ~40 tok/s |
| **Qwen3 9B** | 6GB | 16GB | 16K | ~35 tok/s |
| **Gemma 4 E4B** | 5GB | 12GB | 128K | ~30 tok/s |
| **Gemma 4 26B A4B** | 13GB | 24GB | 256K | ~40 tok/s |
| **Gemma 4 31B** | 20GB | 32GB | 256K | ~15 tok/s |

### Recommended Setup

| Hardware | Model | VRAM | Context |
|----------|-------|-----|--------|
| **8GB VRAM** | **Qwen3.5-9B** ⭐ | 7GB | **262K** |
| 16GB VRAM | Qwen3.5-9B | 7GB | 262K |
| 16GB VRAM | Gemma 4 26B A4B | 13GB | 80K safe |
| 24GB VRAM | Gemma 4 26B A4B | 13GB | 256K |

### Model Tiers

CodingAgent uses 4 model tiers:

- **SMALL** (6-8GB): Qwen3 9B — 16K context  
- **MEDIUM** (12-18GB): Gemma 4 26B A4B — 256K context
- **LARGE** (20GB+): Qwen3.5-9B — **262K** context ⭐
- **FRONTIER** (cloud): Claude — 200K+ context

---

## Setup Steps

### 1. Install Ollama

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

### 2. Pull Models

```bash
# Small models (recommended for local)
ollama pull qwen3:9b
ollama pull gemma:4b

# Medium models (if VRAM allows)
ollama pull qwen3:14b
ollama pull gemma:27b
```

### 3. Configure Per-Project

Create `{workdir}/.agent-context/settings.json`:

```json
{
  "model": "qwen3:9b",
  "permissionMode": "workspace_write",
  "maxTurns": 30
}
```

### 4. Configure Global (Optional)

Create `~/.codingagent/settings.json`:

```json
{
  "model": "qwen3:9b"
}
```

### 5. Run

```bash
# Using project config
cd /path/to/project
codingagent "Fix the bug in main.py"

# Using CLI override
codingagent --model ollama:gemma:4b "Write a test file"
```

---

## Configuration Options

| Setting | Type | Description |
|---------|------|-------------|
| `model` | string | Model name (ollama:qwen3, ollama:gemma, etc.) |
| `permissionMode` | string | `read_only`, `workspace_write`, `danger` |
| `maxTurns` | int | Max turns per session |
| `budgetCeiling` | float | Max USD spend per session |
| `enableSemanticEvaluation` | bool | Run LLM judge on completion |
| `maxLlmWaitSeconds` | int | Timeout for single LLM call |

---

## Performance Tips

1. **Use Q4 quantization** — Smaller, faster, nearly identical quality
2. **Disable thinking** — `--thinking off` for simpler tasks
3. **Limit tools** — Use smaller tool sets for small models
4. **Set lower maxTurns** — Prevents runaway loops

---

## Troubleshooting

| Issue | Solution |
|-------|---------|
| OOM errors | Reduce context or use smaller model |
| Slow response | Use Qwen3 9B instead of 14B |
| Tool failures | Reduce max_turns |
| Model not found | Run `ollama list` to see available models |

---

## Model Selection Reference

```python
from src.core.inference.model_tiers import ModelTier

# Auto-select based on detected VRAM
tier = ModelTier.NANO  # 4-7GB VRAM
tier = ModelTier.SMALL # 8GB VRAM  
tier = ModelTier.MEDIUM # 12GB VRAM
```

---

## CLI Reference

```bash
# Start a task with local model
codingagent --model ollama:qwen3:9b "Your task"

# With thinking disabled (faster for simple tasks)
codingagent --thinking off "Simple refactor"

# With custom settings file
codingagent --config /path/to/settings.json "Task"
```