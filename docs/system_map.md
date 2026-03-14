# System Map

Generated: 2026-03-13 19:10:51Z

```text
Repository: CodingAgent

├── agent-brain
│   ├── identity
│   │   ├── LAWS.md
│   │   └── SOUL.md
│   ├── roles
│   │   ├── operational.md
│   │   └── strategic.md
│   └── skills
│       ├── context_hygiene.md
│       └── dry.md
├── docs
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── FINAL_AUDIT_REPORT.md
│   ├── memory-implementation.md
│   ├── MEMORY_ARCHITECTURE.md
│   ├── mvp-tasklist.md
│   ├── NEW_AUDIT_INSTRUCTIONS.md
│   ├── system_capability_report.md
│   ├── system_map.md
│   ├── tooloptimization.md
│   ├── tuispec.md
│   └── unified_plan.md
├── scripts
│   ├── add_provider.py
│   ├── analyze_tokens.py
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
│   ├── test_agent_stability.py
│   ├── test_langgraph_node.py
│   ├── test_llm_stability.py
│   ├── test_real_lmstudio.py
│   ├── test_real_lmstudio_file_edit.py
│   ├── test_tools.py
│   ├── tree.json
│   ├── validate_ollama.py
│   └── wait_for_model.py
├── src
│   ├── adapters
│   │   ├── lm_studio_adapter.py
│   │   └── ollama_adapter.py
│   ├── config
│   │   └── providers.json
│   ├── core
│   │   ├── context
│   │   │   └── context_builder.py
│   │   ├── inference
│   │   │   ├── __init__.py
│   │   │   ├── adapter_wrappers.py
│   │   │   ├── llm_client.py
│   │   │   └── telemetry.py
│   │   ├── memory
│   │   │   └── distiller.py
│   │   ├── orchestration
│   │   │   ├── graph
│   │   │   │   ├── nodes
│   │   │   │   │   └── ...
│   │   │   │   ├── builder.py
│   │   │   │   └── state.py
│   │   │   ├── agent_brain.py
│   │   │   ├── event_bus.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   ├── schema.json
│   │   │   └── tool_parser.py
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
