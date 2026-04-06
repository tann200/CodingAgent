# src/core/prompts/templates/

Prompt template files used by SystemPromptBuilder.

- `anthropic.txt`  — base prompt for Anthropic / Claude models
- `openai.txt`     — base prompt for OpenAI / GPT models
- `default.txt`    — fallback base prompt for all other providers
- `plan_reminder.txt` — injected when the active agent is the plan agent
- `build_switch.txt`  — injected once when transitioning plan → build
- `max_steps.txt`     — injected when approaching the step limit
