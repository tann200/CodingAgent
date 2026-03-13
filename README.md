# CodingAgent

## Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

### Install dependencies

```
uv pip install -r requirements.txt  # If you use requirements.txt
uv pip install --system  # If you use pyproject.toml (recommended)
```

### Run tests

```
uv pip install --system  # Ensure dependencies are installed
pytest tests/unit/
```

## LLM Provider Adapters

- The Ollama adapter is in `src/adapters/ollama_adapter.py`.
- The LM Studio adapter is in `src/adapters/lm_studio_adapter.py`.
- Both inherit from the `LLMClient` abstract interface (`src/core/inference/llm_client.py`).
- They use the provider configuration from `src/config/providers.json`. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup details.

## Development

Please see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for commands, testing strategies, and provider configuration notes.

## Notes
- Ensure Ollama or LM Studio is running locally and models are available as listed in your config if you want to run the full integration suite (`pytest tests/integration/`).
