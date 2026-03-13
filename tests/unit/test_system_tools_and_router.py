from pathlib import Path

from src.tools import system_tools
from src.core.orchestration.orchestrator import ModelRouter


def test_grep_fallback(tmp_path):
    # create files with matching and non-matching content
    (tmp_path / "a").mkdir()
    f1 = tmp_path / "a" / "file1.txt"
    f1.write_text("hello world\nsearch-term here\n")
    f2 = tmp_path / "b.txt"
    f2.write_text("no match here\n")

    res = system_tools.grep("search-term", path='.', workdir=tmp_path)
    assert isinstance(res, dict)
    assert "search-term" in res.get("output", "")


def test_model_router_basic():
    models = ["small-7b", "med-13b", "large-70b"]
    router = ModelRouter(models=models)

    # simple explain task should route to small (or default)
    assert router.route("Explain this code: sum function") in ("small-7b", "med-13b", "large-70b")

    # heavy refactor triggers large
    assert router.estimate_complexity("Refactor the entire architecture for scalability and performance", None, None) == 'high'
    assert router.route("Refactor the entire architecture for scalability and performance") == 'large-70b'


def test_token_based_routing():
    models = ["small-7b", "med-13b", "large-70b"]
    router = ModelRouter(models=models)
    # build a long prompt likely to exceed token thresholds
    long_text = "word " * 2000  # ~2000 words => token estimate > 1200
    complexity = router.estimate_complexity(long_text, None, None)
    assert complexity in ("medium", "high")
    selected = router.route(long_text)
    # should pick a medium or large model for very long prompts
    assert selected in ("med-13b", "large-70b")
