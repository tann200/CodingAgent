"""
src/core/defaults.py
Centralized defaults for the Sovereign Brain and Ralph GSD loop.
"""

PROVIDER_MODEL_DEFAULTS = {
    "local": "qwen2.5-coder-14b",
    "openai": "gpt-4o",
    "groq": "llama-3.3-70b-versatile",
    "zai": "glm-4.5",
    "anthropic": "claude-4-5-sonnet-latest"
}

DEFAULT_SETTINGS = {
    "active_mode": "lead_architect",
    "target_workspace": ".",
    "theme": "dark",

    "default_model": "qwen2.5-coder-14b",
    "default_provider": "local",

    "lead_architect_model": "qwen2.5-coder-14b",
    "full_stack_engineer_model": "qwen2.5-coder-14b",
    "qa_lead_model": "qwen2.5-coder-14b",

    "providers": {},
    "cached_models": {}
}
