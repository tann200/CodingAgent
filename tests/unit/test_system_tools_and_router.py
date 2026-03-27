
from src.tools import system_tools


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
