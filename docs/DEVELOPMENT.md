# Development Guide

This document outlines standard development workflows, focusing on the local execution of the Minimal Viable Product (MVP) testing scenarios.

## Provider Configuration
`src/config/providers.json` is the authoritative source for provider discovery in tests and adapter initialization. **Prefer this over setting environment variables.**

To add a local provider for integration testing, create a file at `src/config/providers.json` (or copy an example if one exists).

### Example `providers.json`
```json
[
  {
    "name": "lm_studio_local",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "api_key": "",
    "models": ["qwen/qwen3.5-9b"]
  }
]
```

## Running Tests
Run only unit tests related to recent LLM changes:
```bash
pytest tests/unit/test_tool_parser.py -q
pytest tests/unit/test_llm_client_contract.py -q
```

Run all unit tests:
```bash
pytest tests/unit/
```

Run integration tests (requires local providers running):
```bash
pytest tests/integration/
```

## Running Scenarios Headless
*(Coming soon in Phase 4 of the MVP)*
To execute specific scripted scenarios against an Orchestrator headless testing framework:
```bash
python scripts/test_agent_stability.py --scenario fix_syntax --working-dir /tmp/agent_run
```
