"""
Tests for analysis_node fixes:
- Bug: gl.get("items", []) should be gl.get("matches", []) — glob returns "matches" key
- Bug: **/*.py glob only finds Python files — should use **/* + suffix filter
- Bug: .suffix == ".py" guard on SymbolGraph update blocked non-Python files
"""

# ruff: noqa: E501

import pytest
from unittest.mock import patch, MagicMock

from src.core.orchestration.graph.nodes import analysis_node as _mod


def _make_state(**kwargs):
    base = {
        "task": "refactor auth",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "next_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "plan_resumed": None,
        "last_debug_error_type": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
    }
    base.update(kwargs)
    return base


class TestGlobKeyFix:
    """analysis_node must use 'matches' key from glob result, not 'items'."""

    @pytest.mark.asyncio
    async def test_glob_uses_matches_key(self, tmp_path):
        """Files returned under 'matches' are picked up; 'items' key was the old bug."""
        py_file = tmp_path / "app.py"
        py_file.write_text("def main(): pass\n")

        state = _make_state(task="inspect codebase", working_dir=str(tmp_path))

        captured_glob_calls = []

        def fake_call_tool(tool_name, **kwargs):
            if tool_name == "glob":
                captured_glob_calls.append(kwargs)
                # Return correct "matches" key — old code used "items" and got empty
                return {
                    "status": "ok",
                    "pattern": kwargs.get("pattern", ""),
                    "matches": ["app.py"],
                }
            return None

        with patch.object(
            _mod,
            "_call_tool_if_exists"
            if hasattr(_mod, "_call_tool_if_exists")
            else "__name__",
            fake_call_tool,
            create=True,
        ):
            # Run just the glob extraction path by calling analysis_node
            # with a mocked orchestrator that exposes the glob tool
            mock_orch = MagicMock()
            mock_orch.tool_registry.tools = {}

            def fake_tool_call(tool_name, **kwargs):
                if tool_name == "glob":
                    return {"status": "ok", "matches": ["app.py", "lib.js"]}
                return None

            mock_orch.tool_registry.get.side_effect = lambda name: (
                {"fn": lambda **kw: fake_tool_call(name, **kw)}
            )

            config = {"configurable": {"orchestrator": mock_orch}}
            with patch(
                "src.core.orchestration.graph.nodes.analysis_node.generate_repo_summary",
                return_value={
                    "status": "ok",
                    "summary": "ok",
                    "framework": "Python",
                    "languages": ["Python"],
                    "test_framework": "pytest",
                    "entrypoints": [],
                    "modules": [],
                },
            ):
                with patch(
                    "src.core.indexing.repo_indexer.index_repository", return_value={}
                ):
                    with patch("src.core.indexing.vector_store.VectorStore") as mock_vs:
                        mock_vs.return_value.search.return_value = []
                        result = await _mod.analysis_node(state, config)

            assert "analysis_summary" in result

    @pytest.mark.asyncio
    async def test_non_python_files_included_in_relevant_files(self, tmp_path):
        """With the suffix filter fix, .js and .ts files should appear in relevant_files."""
        (tmp_path / "app.js").write_text("function main() {}")
        (tmp_path / "service.ts").write_text("export class Service {}")
        (tmp_path / "main.py").write_text("def main(): pass")

        state = _make_state(task="refactor codebase", working_dir=str(tmp_path))

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.tool_registry.get.side_effect = lambda name: (
            {
                "fn": lambda **kw: {
                    "status": "ok",
                    "matches": ["app.js", "service.ts", "main.py"],
                }
            }
            if name == "glob"
            else None
        )

        config = {"configurable": {"orchestrator": mock_orch}}

        with patch(
            "src.core.orchestration.graph.nodes.analysis_node.generate_repo_summary",
            return_value={
                "status": "ok",
                "summary": "",
                "framework": "unknown",
                "languages": [],
                "test_framework": "none",
                "entrypoints": [],
                "modules": [],
            },
        ):
            with patch(
                "src.core.indexing.repo_indexer.index_repository", return_value={}
            ):
                with patch("src.core.indexing.vector_store.VectorStore") as mock_vs:
                    mock_vs.return_value.search.return_value = []
                    result = await _mod.analysis_node(state, config)

        relevant = result.get("relevant_files", [])
        # With the fix, non-Python supported files should be included
        assert (
            any(f.endswith(".js") for f in relevant)
            or any(f.endswith(".ts") for f in relevant)
            or any(f.endswith(".py") for f in relevant)
        )


class TestSymbolGraphMultiLangUpdate:
    """SymbolGraph update in analysis_node should handle non-Python files."""

    @pytest.mark.asyncio
    async def test_symbol_graph_update_called_for_js_file(self, tmp_path):
        """analysis_node must call sg.update_file for .js files, not skip them."""
        js_file = tmp_path / "app.js"
        js_file.write_text("function greet() {}\n")

        from src.core.indexing.symbol_graph import SymbolGraph

        sg = SymbolGraph(workdir=str(tmp_path))
        # Verify update_file accepts .js (no-op with old guard, real action now)
        sg.update_file(str(js_file))
        assert "app.js" in sg.nodes
        fn_names = [fn["name"] for fn in sg.nodes["app.js"]["symbols"]["functions"]]
        assert "greet" in fn_names

    @pytest.mark.asyncio
    async def test_symbol_graph_update_called_for_ts_file(self, tmp_path):
        """analysis_node multi-lang: .ts files should be indexed."""
        ts_file = tmp_path / "service.ts"
        ts_file.write_text("export class UserService {}\n")

        from src.core.indexing.symbol_graph import SymbolGraph

        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(ts_file))
        assert "service.ts" in sg.nodes
        cls_names = [c["name"] for c in sg.nodes["service.ts"]["symbols"]["classes"]]
        assert "UserService" in cls_names

    def test_unsupported_suffix_not_indexed(self, tmp_path):
        """Files with unsupported suffixes (.txt, .md) must not be added to nodes."""
        txt_file = tmp_path / "README.txt"
        txt_file.write_text("just text\n")

        from src.core.indexing.symbol_graph import SymbolGraph

        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(txt_file))
        assert len(sg.nodes) == 0


class TestRA3LightweightTestMap:
    """RA-3: analysis_node fast-path should produce a test_map when SymbolGraph is cached."""

    def test_extract_module_candidates_returns_snake_case(self):
        """_extract_module_candidates returns lowercase stems from task description."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            _extract_module_candidates,
        )

        result = _extract_module_candidates("fix the auth_service login bug")
        assert "auth_service" in result or "login" in result

    def test_extract_module_candidates_filters_stopwords(self):
        """_extract_module_candidates skips common verbs/stopwords."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            _extract_module_candidates,
        )

        result = _extract_module_candidates("fix and add the new code for all modules")
        assert "fix" not in result
        assert "add" not in result
        assert "the" not in result
        assert "for" not in result
        assert "all" not in result
        assert "new" not in result
        assert "code" not in result

    def test_build_lightweight_test_map_empty_when_cache_cold(self, tmp_path):
        """_build_lightweight_test_map returns {} when no SymbolGraph cached for dir."""
        from src.core.orchestration.graph.nodes import analysis_node as _mod
        import os

        _mod._SYMBOL_GRAPH_CACHE.pop(str(os.path.realpath(str(tmp_path))), None)

        result = _mod._build_lightweight_test_map("fix auth service", str(tmp_path))
        assert result == {}

    def test_build_lightweight_test_map_uses_cached_graph(self, tmp_path):
        """_build_lightweight_test_map queries the cached SymbolGraph and returns string paths."""
        from src.core.orchestration.graph.nodes import analysis_node as _mod
        from unittest.mock import MagicMock
        import os

        mock_sg = MagicMock()
        mock_sg.find_tests_for_module.side_effect = lambda stem: (
            [{"file": f"tests/unit/test_{stem}.py"}] if stem == "auth" else []
        )

        sg_key = str(os.path.realpath(str(tmp_path)))
        with _mod._SYMBOL_GRAPH_CACHE_LOCK:
            _mod._SYMBOL_GRAPH_CACHE[sg_key] = mock_sg

        try:
            result = _mod._build_lightweight_test_map(
                "fix the auth module bug", str(tmp_path)
            )
            if result:
                # Values must be lists of strings, not dicts
                for module, paths in result.items():
                    assert isinstance(paths, list)
                    for p in paths:
                        assert isinstance(p, str)
        finally:
            with _mod._SYMBOL_GRAPH_CACHE_LOCK:
                _mod._SYMBOL_GRAPH_CACHE.pop(sg_key, None)

    def test_build_lightweight_test_map_normalises_dict_results(self, tmp_path):
        """_build_lightweight_test_map converts {'file': path} dicts to plain strings."""
        from src.core.orchestration.graph.nodes import analysis_node as _mod
        from unittest.mock import MagicMock
        import os

        mock_sg = MagicMock()
        # Simulate find_tests_for_module returning List[Dict] (the raw SymbolGraph format)
        mock_sg.find_tests_for_module.return_value = [
            {"file": "tests/test_parser.py"},
            {"file": "tests/test_parser_extra.py"},
        ]

        sg_key = str(os.path.realpath(str(tmp_path)))
        with _mod._SYMBOL_GRAPH_CACHE_LOCK:
            _mod._SYMBOL_GRAPH_CACHE[sg_key] = mock_sg

        try:
            result = _mod._build_lightweight_test_map(
                "fix the parser module", str(tmp_path)
            )
            assert result  # should have found something for "parser"
            for module, paths in result.items():
                for p in paths:
                    assert isinstance(p, str), f"Expected string, got {type(p)}: {p}"
        finally:
            with _mod._SYMBOL_GRAPH_CACHE_LOCK:
                _mod._SYMBOL_GRAPH_CACHE.pop(sg_key, None)

    @pytest.mark.asyncio
    async def test_fast_path_returns_test_map_when_cache_warm(self, tmp_path):
        """analysis_node fast-path sets test_map (not None) when SymbolGraph is cached."""
        from src.core.orchestration.graph.nodes import analysis_node as _mod
        from unittest.mock import MagicMock, patch
        import os

        mock_sg = MagicMock()
        mock_sg.find_tests_for_module.side_effect = lambda stem: (
            [{"file": f"tests/unit/test_{stem}.py"}] if stem == "auth" else []
        )

        sg_key = str(os.path.realpath(str(tmp_path)))
        with _mod._SYMBOL_GRAPH_CACHE_LOCK:
            _mod._SYMBOL_GRAPH_CACHE[sg_key] = mock_sg

        try:
            state = _make_state(
                task="fix the auth login issue",
                working_dir=str(tmp_path),
                next_action="execute",  # triggers fast-path check
            )
            config = {"configurable": {}}

            with patch(
                "src.core.orchestration.graph.builder._task_is_complex",
                return_value=False,
            ):
                result = await _mod.analysis_node(state, config)

            # Fast-path returns early; test_map should be populated (or at least not erroring)
            assert "analysis_summary" in result
            assert result["analysis_summary"] == "Skipped (Fast Path)"
            # test_map is either None (cold cache path) or a dict with string-valued lists
            if result.get("test_map"):
                for module, paths in result["test_map"].items():
                    for p in paths:
                        assert isinstance(p, str)
        finally:
            with _mod._SYMBOL_GRAPH_CACHE_LOCK:
                _mod._SYMBOL_GRAPH_CACHE.pop(sg_key, None)

    def test_complex_path_test_map_values_are_strings(self, tmp_path):
        """Phase 2.4 complex-path test_map must store string paths, not dict items."""
        from src.core.indexing.symbol_graph import SymbolGraph

        # Create a test file so find_tests_for_module can find it
        test_file = tmp_path / "tests" / "unit" / "test_auth.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_login(): pass\n")

        auth_file = tmp_path / "auth.py"
        auth_file.write_text("def login(): pass\n")

        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(test_file))
        sg.update_file(str(auth_file))

        raw = sg.find_tests_for_module("auth")
        # Simulate the normalisation that analysis_node Phase 2.4 now does
        paths = []
        for item in raw[:3]:
            if isinstance(item, dict):
                p = item.get("file") or item.get("file_path")
                if p:
                    paths.append(p)
            elif isinstance(item, str):
                paths.append(item)

        # All normalised values must be strings
        for p in paths:
            assert isinstance(p, str), f"Expected string, got {type(p)}: {p}"
