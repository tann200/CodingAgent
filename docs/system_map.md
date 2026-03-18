# System Map

Generated: 2026-03-18 00:04:14Z

```text
Repository: CodingAgent

├── docs
│   ├── audit
│   │   └── audit-instructions.md
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── gap-analysis.md
│   ├── MEMORY_ARCHITECTURE.md
│   ├── system_map.md
│   └── tuispec.md
├── scripts
│   ├── add_provider.py
│   ├── analyze_tokens.py
│   ├── check_providers_and_models.py
│   ├── diagnose_lmstudio.py
│   ├── ensure_venv.sh
│   ├── fetch_ollama.py
│   ├── generate_system_map.py
│   ├── list_prompts.py
│   ├── refresh_summaries.py
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
│   ├── config
│   │   ├── agent-brain
│   │   │   ├── identity
│   │   │   │   ├── LAWS.md
│   │   │   │   └── SOUL.md
│   │   │   ├── roles
│   │   │   │   ├── analyst.md
│   │   │   │   ├── debugger.md
│   │   │   │   ├── operational.md
│   │   │   │   ├── reviewer.md
│   │   │   │   └── strategic.md
│   │   │   └── skills
│   │   │       ├── context_hygiene.md
│   │   │       └── dry.md
│   │   ├── toolsets
│   │   │   ├── coding.yaml
│   │   │   ├── debug.yaml
│   │   │   ├── planning.yaml
│   │   │   └── review.yaml
│   │   ├── providers.json
│   │   └── schema.json
│   ├── core
│   │   ├── context
│   │   │   ├── context_builder.py
│   │   │   └── context_controller.py
│   │   ├── indexing
│   │   │   ├── repo_indexer.py
│   │   │   ├── symbol_graph.py
│   │   │   └── vector_store.py
│   │   ├── inference
│   │   │   ├── adapters
│   │   │   │   ├── lm_studio_adapter.py
│   │   │   │   └── ollama_adapter.py
│   │   │   ├── __init__.py
│   │   │   ├── adapter_wrappers.py
│   │   │   ├── llm_client.py
│   │   │   ├── llm_manager.py
│   │   │   └── telemetry.py
│   │   ├── memory
│   │   │   ├── advanced_features.py
│   │   │   ├── distiller.py
│   │   │   ├── memory_tools.py
│   │   │   └── session_store.py
│   │   ├── orchestration
│   │   │   ├── graph
│   │   │   │   ├── nodes
│   │   │   │   │   └── ...
│   │   │   │   ├── builder.py
│   │   │   │   └── state.py
│   │   │   ├── agent_brain.py
│   │   │   ├── event_bus.py
│   │   │   ├── graph_factory.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   ├── role_config.py
│   │   │   ├── sandbox.py
│   │   │   ├── schema.json
│   │   │   ├── tool_contracts.py
│   │   │   ├── tool_parser.py
│   │   │   ├── tool_schema.py
│   │   │   └── workspace_guard.py
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── toolsets
│   │   │   ├── coding.yaml
│   │   │   ├── debug.yaml
│   │   │   ├── loader.py
│   │   │   ├── planning.yaml
│   │   │   └── review.yaml
│   │   ├── file_tools.py
│   │   ├── patch_tools.py
│   │   ├── registry.py
│   │   ├── repo_analysis_tools.py
│   │   ├── repo_summary.py
│   │   ├── repo_tools.py
│   │   ├── role_tools.py
│   │   ├── state_tools.py
│   │   ├── symbol_reader.py
│   │   ├── system_tools.py
│   │   └── verification_tools.py
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
├── pyproject.toml
├── README.md
├── requirements.txt
```
