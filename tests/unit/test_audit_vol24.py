"""
Regression tests for Vol24 audit fixes.

Covers:
  SEC-VOL24-1  approval_gate.py: assert ev is not None → RuntimeError guard
  SEC-VOL24-2  mcp_stdio_server.py: path traversal via missing os.sep in startswith
  PERF-VOL24-1 mcp_stdio_server.py: ThreadPoolExecutor reused (singleton in __init__)
  PERF-VOL24-2 openai_compat_adapter.py: assert r is not None → RuntimeError guard
"""

import inspect
import os


class TestSecVol241ApprovalGateAssert:
    """SEC-VOL24-1: assert ev is not None must be replaced with RuntimeError guard."""

    def test_assert_replaced_with_runtime_error(self):
        import src.core.orchestration.approval_gate as ag

        src_text = inspect.getsource(ag)
        # The bare assert must be gone
        assert "assert ev is not None" not in src_text, (
            "SEC-VOL24-1: assert ev is not None is stripped by python -O; "
            "must be replaced with if/raise RuntimeError"
        )
        # Proper guard must be present
        assert "if ev is None:" in src_text, (
            "SEC-VOL24-1: if ev is None: guard must be present in approval_gate"
        )
        assert "raise RuntimeError" in src_text, (
            "SEC-VOL24-1: RuntimeError must be raised when ev is None"
        )


class TestSecVol242McpPathTraversal:
    """SEC-VOL24-2: mcp_stdio_server path traversal fix — os.sep separator required."""

    def test_startswith_uses_sep_separator(self):
        import src.core.orchestration.mcp_stdio_server as mcp

        src_text = inspect.getsource(mcp)
        # The unsafe bare startswith(str(base)) must be gone from the resources/read block
        assert "startswith(str(base))" not in src_text, (
            "SEC-VOL24-2: bare startswith(str(base)) allows path traversal "
            "e.g. /workdir-evil matching /workdir; must append os.sep"
        )
        # os.sep must be used
        assert "_os.sep" in src_text or "os.sep" in src_text, (
            "SEC-VOL24-2: os.sep must be appended to base when checking path prefix"
        )

    def test_path_traversal_guard_logic_correct(self):
        """Demonstrate that /workdir-evil would have bypassed the old check."""
        import pathlib

        base = pathlib.Path("/workdir")
        evil = pathlib.Path("/workdir-evil/secret.txt")
        # Old (broken) check:
        assert str(evil).startswith(str(base)), (
            "Confirms that the old check was broken — evil path matches base prefix"
        )
        # New (correct) check using os.sep:
        base_prefix = str(base) + os.sep
        assert not str(evil).startswith(base_prefix), (
            "New check correctly rejects /workdir-evil/secret.txt"
        )


class TestPerfVol241McpExecutorReuse:
    """PERF-VOL24-1: MCPStdioServer must reuse a single ThreadPoolExecutor."""

    def test_executor_created_in_init(self):
        import src.core.orchestration.mcp_stdio_server as mcp

        src_text = inspect.getsource(mcp.MCPStdioServer.__init__)
        assert "_sampling_executor" in src_text, (
            "PERF-VOL24-1: MCPStdioServer.__init__ must create self._sampling_executor"
        )
        assert "ThreadPoolExecutor" in src_text, (
            "PERF-VOL24-1: __init__ must instantiate ThreadPoolExecutor for sampling"
        )

    def test_sampling_handler_does_not_create_executor(self):
        """sampling/create handler must not spin up a new ThreadPoolExecutor per call."""
        import src.core.orchestration.mcp_stdio_server as mcp

        # Get the full source and find the sampling/create section
        src_text = inspect.getsource(mcp)
        sampling_idx = src_text.find('"sampling/create"')
        assert sampling_idx != -1, "sampling/create handler not found"
        # After the sampling/create marker, no new ThreadPoolExecutor context manager
        sampling_section = src_text[sampling_idx: sampling_idx + 800]
        assert "ThreadPoolExecutor(" not in sampling_section, (
            "PERF-VOL24-1: sampling/create must use self._sampling_executor, "
            "not create a new ThreadPoolExecutor per call"
        )

    def test_executor_instance_attribute_exists(self):
        """Instantiating MCPStdioServer must expose _sampling_executor."""
        from src.core.orchestration.mcp_stdio_server import MCPStdioServer
        import concurrent.futures as cf

        server = MCPStdioServer()
        assert hasattr(server, "_sampling_executor"), (
            "PERF-VOL24-1: MCPStdioServer instance must have _sampling_executor"
        )
        assert isinstance(server._sampling_executor, cf.ThreadPoolExecutor), (
            "PERF-VOL24-1: _sampling_executor must be a ThreadPoolExecutor"
        )
        server._sampling_executor.shutdown(wait=False)


class TestPerfVol242OpenAICompatAssert:
    """PERF-VOL24-2: assert r is not None in openai_compat_adapter must be replaced."""

    def test_assert_replaced_with_runtime_error(self):
        import src.core.inference.adapters.openai_compat_adapter as oa

        src_text = inspect.getsource(oa)
        assert "assert r is not None" not in src_text, (
            "PERF-VOL24-2: assert r is not None is stripped by python -O; "
            "must be replaced with if/raise RuntimeError"
        )
        # Proper defensive guard must be present
        assert "if r is None:" in src_text, (
            "PERF-VOL24-2: if r is None: guard must be present"
        )
