def test_build_planning_task_description_includes_repo_and_analysis_context():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_planning_task_description,
    )

    text = _build_planning_task_description(
        state={
            "working_dir": "/tmp/project",
            "analyst_findings": "Watch auth/session coupling.",
            "call_graph": {"AuthService.login": ["LoginView.post"]},
            "test_map": {"auth/service.py": ["tests/test_auth_service.py"]},
        },
        task="update auth login flow",
        analysis_summary="Login touches service and view layers.",
        relevant_files=["auth/views.py", "auth/service.py"],
        key_symbols=["login_user", "AuthService"],
        repo_lookup_symbols=[{"name": "login_user", "file_path": "auth/views.py"}],
        plan_step_limit=8,
    )

    assert "Task: update auth login flow" in text
    assert "Repository Context:" in text
    assert "Relevant files: auth/views.py, auth/service.py" in text
    assert "Key symbols: login_user, AuthService" in text
    assert "Analysis: Login touches service and view layers." in text
    assert "Analyst Findings:" in text
    assert "Watch auth/session coupling." in text
    assert "Call Graph (symbol" in text
    assert "AuthService.login" in text
    assert "Test Map (module" in text
    assert "tests/test_auth_service.py" in text
    assert "Test Coverage Hint:" in text
    assert "Maximum 8 steps total" in text
    assert "Respond ONLY with valid JSON" in text


def test_build_planning_task_description_uses_ra1_symbols_when_call_graph_absent():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_planning_task_description,
    )

    text = _build_planning_task_description(
        state={"working_dir": "/tmp/project"},
        task="inspect parser",
        analysis_summary="No analysis available",
        relevant_files=[],
        key_symbols=[],
        repo_lookup_symbols=[{"name": "parse_token", "file_path": "src/parser.py"}],
        plan_step_limit=5,
    )

    assert "## Relevant Symbols" in text
    assert "parse_token" in text
    assert "src/parser.py" in text
