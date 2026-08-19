"""P4-T2: Real-LLM end-to-end smoke test.

Skipped automatically unless CODINGAGENT_LLM_API_KEY is set, so it is safe
to include in the test suite and will only run when a real API key is available
(e.g. in a dedicated CI job with the secret injected).
"""
import os
import pytest


_SKIP_REASON = (
    "Real-LLM smoke test requires CODINGAGENT_LLM_API_KEY environment variable. "
    "Set this variable to run the test against a live LLM endpoint."
)


@pytest.mark.skipif(
    not os.environ.get("CODINGAGENT_LLM_API_KEY"),
    reason=_SKIP_REASON,
)
def test_simple_file_creation_with_real_llm(tmp_path):
    """Ask the real LLM to create a hello-world Python file and verify it exists."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(tmp_path))
    result = orch.run_agent_once(
        system_prompt_name=None,
        messages=[
            {
                "role": "user",
                "content": "Create a file hello.py that prints 'hello world'",
            }
        ],
        tools={},
    )
    assert result.get("ok") or result.get("assistant_message"), (
        f"Agent returned unexpected result: {result}"
    )
    assert (tmp_path / "hello.py").exists(), (
        "Expected hello.py to be created but it was not found"
    )


@pytest.mark.skipif(
    not os.environ.get("CODINGAGENT_LLM_API_KEY"),
    reason=_SKIP_REASON,
)
def test_simple_function_edit_with_real_llm(tmp_path):
    """Ask the real LLM to rename a function and verify the change."""
    import shutil

    src = tmp_path / "utils.py"
    src.write_text("def compute(x):\n    return x * 2\n")

    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(tmp_path))
    result = orch.run_agent_once(
        system_prompt_name=None,
        messages=[
            {
                "role": "user",
                "content": (
                    "Rename the function `compute` to `double` in utils.py. "
                    "Keep the implementation identical."
                ),
            }
        ],
        tools={},
    )
    content = src.read_text()
    assert "def double" in content, (
        f"Expected 'def double' in utils.py after rename; got:\n{content}"
    )


@pytest.mark.skipif(
    not os.environ.get("CODINGAGENT_LLM_API_KEY"),
    reason=_SKIP_REASON,
)
def test_multi_file_creation_with_real_llm(tmp_path):
    """Ask the real LLM to create two interdependent files."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(tmp_path))
    orch.run_agent_once(
        system_prompt_name=None,
        messages=[
            {
                "role": "user",
                "content": (
                    "Create greeter.py with a function greet(name) that returns "
                    "'Hello, {name}!'. Also create main.py that imports greet and "
                    "calls greet('World')."
                ),
            }
        ],
        tools={},
    )
    assert (tmp_path / "greeter.py").exists(), "Expected greeter.py to be created"
    assert (tmp_path / "main.py").exists(), "Expected main.py to be created"
    assert "from greeter import greet" in (tmp_path / "main.py").read_text(), (
        "Expected main.py to import from greeter"
    )
