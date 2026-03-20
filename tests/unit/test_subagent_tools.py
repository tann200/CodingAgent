"""
Tests for subagent tools.
"""



class TestDelegateTask:
    """Tests for delegate_task function."""

    def test_delegate_task_invalid_role(self):
        """Test delegate_task with invalid role."""
        from src.tools.subagent_tools import delegate_task

        result = delegate_task(
            role="invalid_role",
            subtask_description="Do something",
        )

        assert "Error: Invalid role" in result
        assert "invalid_role" in result

    def test_delegate_task_valid_roles(self):
        """Test delegate_task accepts valid roles."""
        from src.tools.subagent_tools import delegate_task

        for role in ["researcher", "coder", "reviewer", "planner"]:
            result = delegate_task(
                role=role,
                subtask_description="Test task",
                working_dir="/tmp",
            )
            # Should either work or fail gracefully (not invalid role error)
            assert "Invalid role" not in result

    def test_delegate_task_default_working_dir(self):
        """Test delegate_task uses default working directory."""
        from src.tools.subagent_tools import delegate_task

        # This should not raise an error even with None working_dir
        result = delegate_task(
            role="researcher",
            subtask_description="Test",
            working_dir=None,
        )
        # Just verify it doesn't crash
        assert result is not None

    def test_delegate_task_with_empty_subtask(self):
        """Test delegate_task with empty subtask description."""
        from src.tools.subagent_tools import delegate_task

        result = delegate_task(
            role="coder",
            subtask_description="",
        )
        # Should handle empty string gracefully
        assert result is not None


class TestListSubagentRoles:
    """Tests for list_subagent_roles function."""

    def test_list_subagent_roles_returns_dict(self):
        """Test list_subagent_roles returns expected structure."""
        from src.tools.subagent_tools import list_subagent_roles

        result = list_subagent_roles()

        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] == "ok"
        assert "available_roles" in result

    def test_list_subagent_roles_has_all_roles(self):
        """Test all canonical roles are listed."""
        from src.tools.subagent_tools import list_subagent_roles

        result = list_subagent_roles()
        roles = result["available_roles"]

        # Canonical role names (legacy aliases are in the 'aliases' field)
        expected_roles = ["analyst", "operational", "strategic", "reviewer", "debugger"]
        for role in expected_roles:
            assert role in roles, f"Role '{role}' missing from available_roles"
            assert "description" in roles[role]
            assert "best_for" in roles[role]

    def test_list_subagent_roles_has_aliases(self):
        """Test legacy alias names are documented in each role's aliases list."""
        from src.tools.subagent_tools import list_subagent_roles

        result = list_subagent_roles()
        roles = result["available_roles"]

        # Legacy aliases must be documented
        assert "researcher" in roles["analyst"]["aliases"]
        assert "coder" in roles["operational"]["aliases"]
        assert "planner" in roles["strategic"]["aliases"]

    def test_list_subagent_roles_descriptions(self):
        """Test role descriptions are meaningful."""
        from src.tools.subagent_tools import list_subagent_roles

        result = list_subagent_roles()
        roles = result["available_roles"]

        # Check that descriptions contain relevant keywords
        assert "research" in roles["analyst"]["description"].lower() or "analysis" in roles["analyst"]["description"].lower()
        assert "code" in roles["operational"]["description"].lower() or "implement" in roles["operational"]["description"].lower()
        assert "review" in roles["reviewer"]["description"].lower()
        assert "plan" in roles["strategic"]["description"].lower() or "decompos" in roles["strategic"]["description"].lower()


class TestGraphFactoryIntegration:
    """Tests for GraphFactory integration with subagent tools."""

    def test_graph_factory_get_researcher_graph(self):
        """Test GraphFactory can create researcher graph."""
        from src.core.orchestration.graph_factory import GraphFactory

        graph = GraphFactory.get_graph("researcher")
        assert graph is not None

    def test_graph_factory_get_coder_graph(self):
        """Test GraphFactory can create coder graph."""
        from src.core.orchestration.graph_factory import GraphFactory

        graph = GraphFactory.get_graph("coder")
        assert graph is not None

    def test_graph_factory_get_reviewer_graph(self):
        """Test GraphFactory can create reviewer graph."""
        from src.core.orchestration.graph_factory import GraphFactory

        graph = GraphFactory.get_graph("reviewer")
        assert graph is not None

    def test_graph_factory_get_planner_graph(self):
        """Test GraphFactory can create planner graph."""
        from src.core.orchestration.graph_factory import GraphFactory

        graph = GraphFactory.get_graph("planner")
        assert graph is not None


class TestSubagentToolsImport:
    """Tests for module import."""

    def test_subagent_tools_import(self):
        """Test subagent_tools can be imported."""
        from src.tools import subagent_tools

        assert subagent_tools is not None

    def test_delegate_task_function_exists(self):
        """Test delegate_task function exists."""
        from src.tools.subagent_tools import delegate_task

        assert callable(delegate_task)

    def test_list_subagent_roles_function_exists(self):
        """Test list_subagent_roles function exists."""
        from src.tools.subagent_tools import list_subagent_roles

        assert callable(list_subagent_roles)
