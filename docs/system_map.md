# System Map

Generated: 2026-03-11 18:15:58Z

```text
Repository: CodingAgent

├── agent-brain
│   ├── agents
│   │   ├── coding_agent.md
│   │   ├── full_stack_engineer.md
│   │   └── qa_lead.md
│   ├── skills
│   │   ├── context_hygiene.md
│   │   └── dry.md
│   ├── templates
│   │   ├── architecture.md
│   │   ├── concerns.md
│   │   ├── conventions.md
│   │   ├── stack.md
│   │   ├── structure.md
│   │   └── testing.md
│   ├── workflows
│   │   ├── debug.md
│   │   └── plan_phase.md
│   ├── LAWS.md
│   ├── SOUL.md
│   ├── system_prompt_coding.md
│   ├── system_prompt_planner.md
│   └── system_prompts.md
├── docs
│   ├── ARCHITECTURE.md
│   ├── FINAL_AUDIT_REPORT.md
│   ├── memory-implementation.md
│   ├── NEW_AUDIT_INSTRUCTIONS.md
│   ├── system_capability_report.md
│   ├── tooloptimization.md
│   └── tuispec.md
├── scripts
│   ├── add_provider.py
│   ├── check_providers_and_models.py
│   ├── diagnose_lmstudio.py
│   ├── fetch_ollama.py
│   ├── generate_system_map.py
│   ├── list_prompts.py
│   ├── run_generate.py
│   ├── run_tests_settings.py
│   ├── run_tui.py
│   ├── simulate_tui.py
│   ├── start_tui.py
│   ├── test_langgraph_node.py
│   ├── test_tools.py
│   ├── validate_ollama.py
│   └── wait_for_model.py
├── src
│   ├── adapters
│   │   ├── lm_studio_adapter.py
│   │   └── ollama_adapter.py
│   ├── config
│   │   └── providers.json
│   ├── core
│   │   ├── orchestration
│   │   │   ├── agent_brain.py
│   │   │   ├── event_bus.py
│   │   │   ├── langgraph_node.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   └── schema.json
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── llm_manager.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── file_tools.py
│   │   ├── registry.py
│   │   └── system_tools.py
│   ├── ui
│   │   ├── components
│   │   │   ├── __init__.py
│   │   │   └── log_panel.py
│   │   ├── styles
│   │   │   ├── main.tcss
│   │   │   └── README.md
│   │   ├── views
│   │   │   ├── __init__.py
│   │   │   ├── main_view.py
│   │   │   ├── provider_panel.py
│   │   │   └── settings_panel.py
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── README.md
│   │   ├── textual_app.py
│   │   └── textual_app_impl.py
│   ├── main.py
│   └── tmp_app_started.log
├── pyproject.toml
├── README.md
├── requirements.txt
```
