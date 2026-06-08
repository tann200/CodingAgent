"""
Real integration tests for tool chain execution.

Tests verify that multiple tools work together correctly with real execution,
not mocked behavior.

NOTE: These are TEMPLATE tests that demonstrate the integration testing approach.
They need to be updated to match the actual Orchestrator API.
Current status: FAILING - Need API fixes for:
- Orchestrator LLM adapter injection
- Tool result format (content vs result wrapper)
- Bash command security restrictions
- ContextBuilder API
"""

import pytest
from pathlib import Path


@pytest.mark.integration_real
@pytest.mark.skip(reason="Template tests - need API updates to match actual Orchestrator interface")
class TestToolChainIntegration:
    """Tests for real tool chain execution (search → read → edit)."""

    def test_search_read_edit_integration(self, tmp_path: Path):
        """
        Integration test: search_code → read_file → edit_file with REAL tools.
        
        This test verifies:
        1. search_code finds the correct file
        2. read_file reads the actual file content
        3. edit_file modifies the real file on disk
        4. Orchestrator coordinates the chain correctly
        """
        from src.core.orchestration.orchestrator import Orchestrator
        from src.core.inference.adapters.mock_adapter import MockAdapter
        
        # Setup: Create test file
        test_file = tmp_path / "calculator.py"
        test_file.write_text("""
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
""")
        
        # Create orchestrator with real working directory
        orch = Orchestrator(working_dir=str(tmp_path))
        
        # Use MockAdapter with scripted responses that trigger real tool calls
        responses = [
            # Response 1: Search for "subtract" function
            """I'll search for the subtract function.
<tool_calls>
[{"name": "search_code", "arguments": {"pattern": "subtract", "file_pattern": "*.py"}}]
</tool_calls>""",
            
            # Response 2: Read the file
            """Found it in calculator.py. Let me read the file.
<tool_calls>
[{"name": "read_file", "arguments": {"path": "calculator.py"}}]
</tool_calls>""",
            
            # Response 3: Edit the file
            """I'll rename subtract to minus.
<tool_calls>
[{"name": "edit_file", "arguments": {"path": "calculator.py", "old_string": "def subtract(a, b):", "new_string": "def minus(a, b):"}}]
</tool_calls>""",
            
            # Response 4: Completion
            "Done! I've renamed the function from subtract to minus."
        ]
        
        adapter = MockAdapter(responses=responses)
        orch._llm_manager._default_adapter = adapter
        
        # Execute: Run agent with task
        result = orch.run_agent_once(
            messages=[{
                "role": "user",
                "content": "Rename the subtract function to minus in calculator.py"
            }]
        )
        
        # Verify: Real file was modified
        modified_content = test_file.read_text()
        assert "def minus(a, b):" in modified_content, \
            "File should contain renamed function"
        assert "def subtract(a, b):" not in modified_content, \
            "Old function name should be gone"
        
        # Verify: Result indicates success
        assert result.get("ok") or result.get("assistant_message")

    def test_read_before_write_enforcement_integration(self, tmp_path: Path):
        """
        Integration test: Verify read-before-write guardrail works with real orchestrator.
        
        This test verifies:
        1. edit_file without prior read is blocked
        2. After read_file, edit_file is allowed
        3. Real files are protected by guardrails
        """
        from src.core.orchestration.orchestrator import Orchestrator
        
        # Setup: Create test file
        test_file = tmp_path / "config.json"
        test_file.write_text('{"setting": "old_value"}')
        
        orch = Orchestrator(working_dir=str(tmp_path))
        
        # Test 1: edit_file without read should be blocked
        edit_result_blocked = orch.execute_tool({
            "name": "edit_file",
            "arguments": {
                "path": "config.json",
                "old_string": '"old_value"',
                "new_string": '"new_value"'
            }
        })
        
        assert edit_result_blocked.get("ok") is False, \
            "edit_file without read should be blocked"
        assert "must read" in edit_result_blocked.get("error", "").lower(), \
            "Error should mention read requirement"
        
        # Test 2: After read, edit should be allowed
        read_result = orch.execute_tool({
            "name": "read_file",
            "arguments": {"path": "config.json"}
        })
        assert read_result.get("ok") is True, "read_file should succeed"
        
        edit_result_allowed = orch.execute_tool({
            "name": "edit_file",
            "arguments": {
                "path": "config.json",
                "old_string": '"old_value"',
                "new_string": '"new_value"'
            }
        })
        
        assert edit_result_allowed.get("ok") is not False or \
               "must read" not in edit_result_allowed.get("error", "").lower(), \
            "edit_file after read should be allowed"
        
        # Verify: File was modified
        content = test_file.read_text()
        assert '"new_value"' in content, "File should be modified after read"

    def test_bash_execution_integration(self, tmp_path: Path):
        """
        Integration test: Verify bash tool executes real commands.
        
        This test verifies:
        1. bash tool executes actual shell commands
        2. Output is captured correctly
        3. Exit codes are returned
        """
        from src.core.orchestration.orchestrator import Orchestrator
        
        orch = Orchestrator(working_dir=str(tmp_path))
        
        # Create a test file via bash
        result = orch.execute_tool({
            "name": "bash",
            "arguments": {
                "command": "echo 'Hello from bash' > test_output.txt"
            }
        })
        
        assert result.get("ok") is True, "Bash command should execute"
        
        # Verify file was created
        output_file = tmp_path / "test_output.txt"
        assert output_file.exists(), "Bash should create real file"
        assert "Hello from bash" in output_file.read_text()

    def test_file_tools_integration(self, tmp_path: Path):
        """
        Integration test: Verify write_file → read_file → edit_file chain.
        
        This test verifies:
        1. write_file creates real files
        2. read_file reads actual content
        3. edit_file modifies real files
        4. Session state tracks file operations
        """
        from src.core.orchestration.orchestrator import Orchestrator
        
        orch = Orchestrator(working_dir=str(tmp_path))
        
        # Step 1: Write file
        write_result = orch.execute_tool({
            "name": "write_file",
            "arguments": {
                "path": "notes.txt",
                "content": "Original content\nLine 2\nLine 3"
            }
        })
        assert write_result.get("ok") is True, "write_file should succeed"
        
        # Verify file exists
        notes_file = tmp_path / "notes.txt"
        assert notes_file.exists(), "File should be created"
        
        # Step 2: Read file
        read_result = orch.execute_tool({
            "name": "read_file",
            "arguments": {"path": "notes.txt"}
        })
        assert read_result.get("ok") is True, "read_file should succeed"
        assert "Original content" in read_result.get("content", ""), \
            "Should read actual file content"
        
        # Step 3: Edit file
        edit_result = orch.execute_tool({
            "name": "edit_file",
            "arguments": {
                "path": "notes.txt",
                "old_string": "Original content",
                "new_string": "Modified content"
            }
        })
        assert edit_result.get("ok") is not False or \
               "must read" not in edit_result.get("error", "").lower(), \
            "edit_file should succeed after read"
        
        # Verify modification
        final_content = notes_file.read_text()
        assert "Modified content" in final_content, "File should be modified"
        assert "Original content" not in final_content, "Old content should be replaced"


@pytest.mark.integration_real
@pytest.mark.skip(reason="Template tests - need API updates")
class TestCacheIntegration:
    """Tests for cache invalidation integration."""

    def test_file_write_invalidates_cache(self, tmp_path: Path):
        """
        Integration test: Verify write_file invalidates context cache.
        
        This test verifies:
        1. ContextBuilder caches file content
        2. write_file invalidates the cache
        3. Subsequent reads get fresh content
        """
        from src.core.orchestration.orchestrator import Orchestrator
        from src.core.context.context_builder import ContextBuilder
        
        # Setup
        test_file = tmp_path / "data.txt"
        test_file.write_text("Version 1")
        
        orch = Orchestrator(working_dir=str(tmp_path))
        ctx_builder = ContextBuilder(working_dir=str(tmp_path))
        
        # Cache the file content
        ctx1 = ctx_builder.build_context(
            messages=[],
            current_plan=None,
            additional_files=[str(test_file)]
        )
        assert "Version 1" in str(ctx1), "Should cache version 1"
        
        # Modify file via write_file tool
        orch.execute_tool({
            "name": "write_file",
            "arguments": {
                "path": "data.txt",
                "content": "Version 2"
            }
        })
        
        # Build context again - should get fresh content, not cached
        ctx2 = ctx_builder.build_context(
            messages=[],
            current_plan=None,
            additional_files=[str(test_file)]
        )
        
        # If cache was properly invalidated, should see version 2
        assert "Version 2" in str(ctx2), \
            "Cache should be invalidated after write_file"


# NOTE: These tests use REAL tools, REAL file operations, and REAL orchestrator.
# Only the LLM responses are mocked for deterministic behavior.
#
# For mock-heavy orchestration unit tests, see:
# tests/unit/orchestration/test_pipeline_orchestration.py
