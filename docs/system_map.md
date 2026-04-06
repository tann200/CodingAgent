# System Map

Generated: 2026-04-05 22:51:50Z

```text
Repository: CodingAgent

├── docs
│   ├── archive
│   │   ├── audit-report-tui-eventbus_complete_2026-03-26.md
│   │   ├── audit-report-vol11_complete_2026-03-27.md
│   │   ├── dev-plan-claw-parity_complete_2026-04-05.md
│   │   ├── gap-analysis-claw-code-v2-source_2026-04-05.md
│   │   ├── gap-analysis-claw-code-v2_2026-04-03.md
│   │   ├── gap-analysis-claw-code_2026-04-03.md
│   │   ├── gap-analysis-vs-opencode-claw-source_2026-04-05.md
│   │   ├── gap-analysis-vs-opencode-claw_2026-04-03.md
│   │   ├── mitigation-plan-opencode-claw-parity_complete_2026-04-05.md
│   │   ├── orchestration-gap-analysis-source_2026-04-05.md
│   │   ├── orchestration-gap-analysis_2026-04-03.md
│   │   ├── TASK_LIST_sprint1_complete_2026-04-05.md
│   │   ├── TASK_LIST_sprint2_complete_2026-04-05.md
│   │   ├── TOOLS_GAP_ANALYSIS_complete_2026-04-05.md
│   │   ├── TOOLS_GAP_ANALYSIS_complete_2026-04-06.md
│   │   ├── tui-gap-analysis-source_2026-04-05.md
│   │   └── tui-gap-analysis_2026-04-03.md
│   ├── audit
│   │   ├── audit-instructions.md
│   │   ├── audit-report-vol12.md
│   │   ├── audit-report-vol13.md
│   │   ├── audit-report-vol14.md
│   │   └── deep-dive-claw-code-architecture.md
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── IMPLEMENTATION_PLAN.md
│   ├── system_map.md
│   └── TUI_SPEC.md
├── scripts
│   ├── add_provider.py
│   ├── analyze_tokens.py
│   ├── check_providers_and_models.py
│   ├── diagnose_lmstudio.py
│   ├── ensure_venv.sh
│   ├── fetch_ollama.py
│   ├── generate_system_map.py
│   ├── list_prompts.py
│   ├── lmstudio_diagnostic.py
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
│   │   │       ├── dry.md
│   │   │       └── stuck.md
│   │   ├── skills
│   │   │   ├── code_review.md
│   │   │   ├── debug_checklist.md
│   │   │   ├── explore_codebase.md
│   │   │   ├── refactor.md
│   │   │   └── write_tests.md
│   │   ├── toolsets
│   │   │   ├── analysis.yaml
│   │   │   ├── coding.yaml
│   │   │   ├── debug.yaml
│   │   │   ├── loader.py
│   │   │   ├── planning.yaml
│   │   │   └── review.yaml
│   │   ├── formatters.yaml
│   │   ├── lsp_servers.yaml
│   │   ├── providers.json
│   │   └── schema.json
│   ├── core
│   │   ├── context
│   │   │   ├── context_builder.py
│   │   │   └── context_controller.py
│   │   ├── evaluation
│   │   │   └── scenario_evaluator.py
│   │   ├── indexing
│   │   │   ├── lsp_client.py
│   │   │   ├── lsp_context.py
│   │   │   ├── lsp_manager.py
│   │   │   ├── repo_indexer.py
│   │   │   ├── symbol_graph.py
│   │   │   └── vector_store.py
│   │   ├── inference
│   │   │   ├── adapters
│   │   │   │   ├── github_copilot_adapter.py
│   │   │   │   ├── github_copilot_auth.py
│   │   │   │   ├── lm_studio_adapter.py
│   │   │   │   ├── mock_adapter.py
│   │   │   │   ├── ollama_adapter.py
│   │   │   │   ├── openai_compat_adapter.py
│   │   │   │   └── openrouter_adapter.py
│   │   │   ├── __init__.py
│   │   │   ├── adapter_wrappers.py
│   │   │   ├── llm_client.py
│   │   │   ├── llm_manager.py
│   │   │   ├── model_tiers.py
│   │   │   ├── provider_context.py
│   │   │   ├── telemetry.py
│   │   │   ├── thinking_utils.py
│   │   │   └── tokenizer.py
│   │   ├── mcp
│   │   │   ├── __init__.py
│   │   │   └── mcp_client.py
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
│   │   │   ├── agent_types.py
│   │   │   ├── approval_gate.py
│   │   │   ├── cross_session_bus.py
│   │   │   ├── dag_parser.py
│   │   │   ├── deferred_init.py
│   │   │   ├── event_bus.py
│   │   │   ├── event_log.py
│   │   │   ├── file_lock_manager.py
│   │   │   ├── graph_factory.py
│   │   │   ├── instruction_loader.py
│   │   │   ├── loop_guards.py
│   │   │   ├── mcp_stdio_server.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   ├── permission_policy.py
│   │   │   ├── plan_mode.py
│   │   │   ├── preview_coordinator.py
│   │   │   ├── preview_service.py
│   │   │   ├── prsw_topics.py
│   │   │   ├── role_config.py
│   │   │   ├── rollback_manager.py
│   │   │   ├── schema.json
│   │   │   ├── session_cost_tracker.py
│   │   │   ├── session_lifecycle.py
│   │   │   ├── session_registry.py
│   │   │   ├── session_store.py
│   │   │   ├── session_watcher.py
│   │   │   ├── snapshot_manager.py
│   │   │   ├── token_budget.py
│   │   │   ├── tool_contracts.py
│   │   │   ├── tool_execution_service.py
│   │   │   ├── tool_hooks.py
│   │   │   ├── tool_parser.py
│   │   │   ├── tool_result_formatter.py
│   │   │   ├── wave_coordinator.py
│   │   │   └── workspace_guard.py
│   │   ├── prompts
│   │   │   ├── templates
│   │   │   │   ├── anthropic.txt
│   │   │   │   ├── build_switch.txt
│   │   │   │   ├── default.txt
│   │   │   │   ├── local-medium.md
│   │   │   │   ├── local-small.md
│   │   │   │   ├── max_steps.txt
│   │   │   │   ├── openai.txt
│   │   │   │   ├── plan_reminder.txt
│   │   │   │   ├── README.md
│   │   │   │   └── reasoning.md
│   │   │   ├── __init__.py
│   │   │   └── system_prompt_builder.py
│   │   ├── settings
│   │   │   ├── __init__.py
│   │   │   └── controller.py
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── utils
│   │   │   ├── __init__.py
│   │   │   └── retry.py
│   │   ├── config_loader.py
│   │   ├── credentials.py
│   │   ├── errors.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── toolsets
│   │   │   ├── __init__.py
│   │   │   ├── coding.yaml
│   │   │   ├── debug.yaml
│   │   │   ├── loader.py
│   │   │   ├── planning.yaml
│   │   │   └── review.yaml
│   │   ├── __init__.py
│   │   ├── _approval.py
│   │   ├── _path_utils.py
│   │   ├── _registry.py
│   │   ├── _result.py
│   │   ├── _security.py
│   │   ├── _tool.py
│   │   ├── _truncate.py
│   │   ├── ast_tools.py
│   │   ├── bash_security.py
│   │   ├── batch_tools.py
│   │   ├── file_tools.py
│   │   ├── formatter.py
│   │   ├── git_tools.py
│   │   ├── guardrails.py
│   │   ├── interaction_tools.py
│   │   ├── lint_dispatch.py
│   │   ├── lsp_tools.py
│   │   ├── memory_tools.py
│   │   ├── patch_tools.py
│   │   ├── permission_context.py
│   │   ├── plan_mode_tools.py
│   │   ├── project_tools.py
│   │   ├── registry.py
│   │   ├── repo_analysis_tools.py
│   │   ├── repo_summary.py
│   │   ├── repo_tools.py
│   │   ├── role_tools.py
│   │   ├── sandbox.py
│   │   ├── skill_tools.py
│   │   ├── state_tools.py
│   │   ├── subagent_tools.py
│   │   ├── symbol_reader.py
│   │   ├── system_tools.py
│   │   ├── todo_tools.py
│   │   ├── tools_config.py
│   │   ├── verification_tools.py
│   │   └── web_tools.py
│   ├── main.py
├── pyproject.toml
├── README.md
├── requirements.txt
```
