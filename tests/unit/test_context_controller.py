"""
Tests for ContextController — token budget enforcement for file context.
"""

from src.core.context.context_controller import ContextController, create_context_controller


class TestContextControllerInit:
    def test_default_max_tokens(self):
        cc = ContextController()
        assert cc.max_context_tokens == ContextController.DEFAULT_MAX_TOKENS

    def test_custom_max_tokens(self):
        cc = ContextController(max_context_tokens=4000)
        assert cc.max_context_tokens == 4000

    def test_factory_function(self):
        cc = create_context_controller(max_tokens=8000)
        assert isinstance(cc, ContextController)
        assert cc.max_context_tokens == 8000


class TestTokenEstimation:
    def test_empty_string_returns_one(self):
        cc = ContextController()
        assert cc.estimate_tokens("") == 1

    def test_proportional_to_length(self):
        cc = ContextController()
        short = cc.estimate_tokens("hi")
        long = cc.estimate_tokens("x" * 400)
        assert long > short

    def test_four_chars_per_token(self):
        cc = ContextController()
        assert cc.estimate_tokens("abcd") == 1
        assert cc.estimate_tokens("a" * 400) == 100


class TestPrioritizeFiles:
    def test_sorts_by_relevance_descending(self):
        cc = ContextController()
        files = [
            {"path": "low.py"},
            {"path": "high.py"},
            {"path": "mid.py"},
        ]
        scores = {"high.py": 0.9, "mid.py": 0.5, "low.py": 0.1}
        result = cc.prioritize_files(files, scores)
        assert result[0]["path"] == "high.py"
        assert result[-1]["path"] == "low.py"

    def test_missing_score_defaults_to_zero(self):
        cc = ContextController()
        files = [{"path": "a.py"}, {"path": "b.py"}]
        scores = {"b.py": 0.8}
        result = cc.prioritize_files(files, scores)
        assert result[0]["path"] == "b.py"


class TestShouldSummarize:
    def test_small_file_not_summarized(self):
        cc = ContextController()
        assert cc.should_summarize({"line_count": 10}) is False

    def test_large_file_is_summarized(self):
        cc = ContextController()
        assert cc.should_summarize({"line_count": 600}) is True

    def test_exactly_at_threshold_not_summarized(self):
        cc = ContextController()
        assert cc.should_summarize({"line_count": ContextController.LARGE_FILE_THRESHOLD}) is False


class TestSummarizeFileContent:
    def test_short_content_returned_unchanged(self):
        cc = ContextController()
        content = "line\n" * 10
        result = cc.summarize_file_content(content, target_lines=50)
        assert result == content

    def test_long_content_truncated(self):
        cc = ContextController()
        content = "pass\n" * 200
        result = cc.summarize_file_content(content, target_lines=50)
        assert "more lines" in result

    def test_important_lines_extracted(self):
        cc = ContextController()
        content = "import os\nclass Foo:\n    pass\n" + "x = 1\n" * 200
        result = cc.summarize_file_content(content, target_lines=50)
        # Should include the class or import
        assert "class Foo" in result or "import os" in result


class TestEnforceBudget:
    def test_small_files_all_included(self):
        cc = ContextController(max_context_tokens=10000)
        files = [
            {"path": "a.py", "line_count": 10, "estimated_tokens": 50},
            {"path": "b.py", "line_count": 10, "estimated_tokens": 50},
        ]
        included, excluded = cc.enforce_budget(files, [])
        assert len(included) == 2
        assert len(excluded) == 0

    def test_excess_files_excluded(self):
        cc = ContextController(max_context_tokens=200)
        files = [
            {"path": f"file_{i}.py", "line_count": 5, "estimated_tokens": 100}
            for i in range(10)
        ]
        included, excluded = cc.enforce_budget(files, [])
        assert len(excluded) > 0

    def test_returns_tuple(self):
        cc = ContextController()
        result = cc.enforce_budget([], [])
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestGetRelevantSnippets:
    def test_returns_matching_lines(self):
        cc = ContextController()
        content = "def foo(): pass\ndef bar(): pass\nx = foo()\n"
        snippets = cc.get_relevant_snippets(content, "foo")
        assert len(snippets) >= 1
        combined = "\n".join(snippets)
        assert "foo" in combined

    def test_no_match_returns_first_portion(self):
        cc = ContextController()
        content = "line one\nline two\nline three\n"
        snippets = cc.get_relevant_snippets(content, "zzznomatch")
        assert len(snippets) >= 1

    def test_max_five_snippets(self):
        cc = ContextController()
        # Create content with many matches
        content = "\n".join([f"target line {i}" for i in range(100)])
        snippets = cc.get_relevant_snippets(content, "target")
        assert len(snippets) <= 5
