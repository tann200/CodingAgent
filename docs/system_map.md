# System Map

Generated: 2026-03-25 23:30:32Z

```text
Repository: CodingAgent

├── docs
│   ├── audit
│   │   ├── audit-instructions.md
│   │   └── audit-report-vol9.md
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   └── system_map.md
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
│   ├── run_benchmark.py
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
│   ├── config
│   │   ├── agent-brain
│   │   │   ├── identity
│   │   │   │   ├── LAWS.md
│   │   │   │   └── SOUL.md
│   │   │   ├── roles
│   │   │   │   ├── analyst.md
│   │   │   │   ├── debugger.md
│   │   │   │   ├── operational.md
│   │   │   │   ├── researcher.md
│   │   │   │   ├── reviewer.md
│   │   │   │   ├── scout.md
│   │   │   │   ├── strategic.md
│   │   │   │   └── tester.md
│   │   │   └── skills
│   │   │       ├── context_hygiene.md
│   │   │       └── dry.md
│   │   ├── toolsets
│   │   │   ├── coding.yaml
│   │   │   ├── debug.yaml
│   │   │   ├── loader.py
│   │   │   ├── planning.yaml
│   │   │   └── review.yaml
│   │   ├── providers.json
│   │   └── schema.json
│   ├── core
│   │   ├── context
│   │   │   ├── context_builder.py
│   │   │   └── context_controller.py
│   │   ├── evaluation
│   │   │   └── scenario_evaluator.py
│   │   ├── indexing
│   │   │   ├── repo_indexer.py
│   │   │   ├── symbol_graph.py
│   │   │   └── vector_store.py
│   │   ├── inference
│   │   │   ├── adapters
│   │   │   │   ├── lm_studio_adapter.py
│   │   │   │   ├── ollama_adapter.py
│   │   │   │   ├── openai_compat_adapter.py
│   │   │   │   └── openrouter_adapter.py
│   │   │   ├── __init__.py
│   │   │   ├── adapter_wrappers.py
│   │   │   ├── llm_client.py
│   │   │   ├── llm_manager.py
│   │   │   ├── provider_context.py
│   │   │   ├── telemetry.py
│   │   │   └── thinking_utils.py
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
│   │   │   ├── agent_session_manager.py
│   │   │   ├── cross_session_bus.py
│   │   │   ├── dag_parser.py
│   │   │   ├── event_bus.py
│   │   │   ├── file_lock_manager.py
│   │   │   ├── graph_factory.py
│   │   │   ├── mcp_stdio_server.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   ├── plan_mode.py
│   │   │   ├── preview_service.py
│   │   │   ├── prsw_topics.py
│   │   │   ├── role_config.py
│   │   │   ├── rollback_manager.py
│   │   │   ├── schema.json
│   │   │   ├── session_lifecycle.py
│   │   │   ├── session_registry.py
│   │   │   ├── session_watcher.py
│   │   │   ├── token_budget.py
│   │   │   ├── tool_contracts.py
│   │   │   ├── tool_parser.py
│   │   │   ├── wave_coordinator.py
│   │   │   └── workspace_guard.py
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── _path_utils.py
│   │   ├── file_tools.py
│   │   ├── git_tools.py
│   │   ├── patch_tools.py
│   │   ├── registry.py
│   │   ├── repo_analysis_tools.py
│   │   ├── repo_summary.py
│   │   ├── repo_tools.py
│   │   ├── role_tools.py
│   │   ├── state_tools.py
│   │   ├── subagent_tools.py
│   │   ├── symbol_reader.py
│   │   ├── system_tools.py
│   │   ├── todo_tools.py
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
│   │   └── textual_app_impl.py
│   ├── main.py
├── pyproject.toml
├── README.md
├── requirements.txt
```
