"""
Regression tests for the P1–P10 opencode-inspired improvements (batch 2):

  P1  — bash() stdout/stderr truncated at 16384/6000 chars
  P2  — read_file binary detection, 50KB output cap, per-line 2000-char cap
  P3  — edit_file_atomic fuzzy matching (trailing whitespace, normalised whitespace, indentation)
  P4  — multiedit atomic multi-replacement
  P5  — batch tool registration + nested-batch rejection
  P6  — bash() description param logged (no error)
  P7  — ask_user choices param validation
  P8  — read_web_page HTTP→HTTPS upgrade + format param validation
  P9  — format_file helper returns skipped for unknown extension
  P10 — load_skill / list_skills path-traversal rejection + not-found error
"""


# ruff: noqa: E501
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# P1 — bash output truncation
# ---------------------------------------------------------------------------

def test_bash_stdout_truncated(tmp_path):
    """Stdout longer than 16384 chars is truncated with a notice."""
    from src.tools.file_tools import bash

    # Write a file that when cat'd produces >16KB
    big = tmp_path / "big.txt"
    big.write_text("A" * 20_000)

    result = bash(f"cat {big}", workdir=tmp_path)
    assert result["status"] == "ok"
    assert len(result["stdout"]) <= 16_384 + 100  # notice adds a few chars
    assert "truncated" in result["stdout"]


def test_bash_stderr_truncated(tmp_path):
    """Stderr longer than _BASH_STDERR_MAX chars is truncated with a notice."""
    from src.tools.file_tools import bash, _BASH_STDERR_MAX

    # Generate stderr larger than the cap
    script = tmp_path / "err.py"
    script.write_text(f"import sys; sys.stderr.write('E' * {_BASH_STDERR_MAX + 2000})")
    result = bash(f"python3 {script}", workdir=tmp_path)
    assert result["status"] == "ok"
    assert len(result["stderr"]) <= _BASH_STDERR_MAX + 200  # cap + truncation notice
    assert "truncated" in result["stderr"]


def test_bash_no_truncation_when_short(tmp_path):
    """Short stdout is returned verbatim, no truncation notice."""
    from src.tools.file_tools import bash

    result = bash("echo hello", workdir=tmp_path)
    assert result["status"] == "ok"
    assert "truncated" not in result["stdout"]
    assert result["stdout"].strip() == "hello"


# ---------------------------------------------------------------------------
# P2 — read_file binary detection + caps
# ---------------------------------------------------------------------------

def test_read_file_binary_rejected(tmp_path):
    """Binary files (containing null bytes) are rejected."""
    from src.tools.file_tools import read_file

    binary = tmp_path / "img.bin"
    binary.write_bytes(b"\x00\x01\x02\x03binary content here\x00\xff")

    result = read_file(str(binary), workdir=tmp_path)
    assert result["status"] == "error"
    assert "binary" in result["error"].lower()


def test_read_file_50kb_cap(tmp_path):
    """Files larger than 50KB are truncated with a notice."""
    from src.tools.file_tools import read_file

    big = tmp_path / "big.py"
    big.write_text("# line\n" * 10_000)  # ~70KB

    result = read_file(str(big), workdir=tmp_path)
    assert result["status"] == "ok"
    assert result.get("truncated") is True
    assert "truncated" in result["content"]


def test_read_file_long_line_capped(tmp_path):
    """Lines longer than 2000 chars are truncated inline."""
    from src.tools.file_tools import read_file

    f = tmp_path / "long.txt"
    long_line = "X" * 5000
    f.write_text(f"before\n{long_line}\nafter\n")

    result = read_file(str(f), workdir=tmp_path)
    assert result["status"] == "ok"
    for line in result["content"].splitlines():
        assert len(line) <= 2_100  # 2000 + truncation notice overhead


def test_read_file_normal_file(tmp_path):
    """Normal text files are returned fully."""
    from src.tools.file_tools import read_file

    f = tmp_path / "ok.py"
    f.write_text("def hello(): pass\n")

    result = read_file(str(f), workdir=tmp_path)
    assert result["status"] == "ok"
    assert "def hello" in result["content"]
    assert not result.get("truncated")


# ---------------------------------------------------------------------------
# P3 — fuzzy edit matching in edit_file_atomic
# ---------------------------------------------------------------------------

def test_edit_atomic_fuzzy_trailing_whitespace(tmp_path):
    """edit_file_atomic matches when old_string has trailing spaces that differ."""
    from src.tools.file_tools import edit_file_atomic

    f = tmp_path / "code.py"
    # File has no trailing spaces on the def line
    f.write_text("def foo():\n    return 1\n")
    # old_string has trailing space on first line
    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = edit_file_atomic(
            str(f),
            old_string="def foo():   \n    return 1",
            new_string="def foo():\n    return 42",
            workdir=tmp_path,
        )
    assert result["status"] == "ok", result.get("error")
    assert "42" in f.read_text()


def test_edit_atomic_fuzzy_indentation(tmp_path):
    """edit_file_atomic matches when indentation level differs."""
    from src.tools.file_tools import edit_file_atomic

    f = tmp_path / "code.py"
    f.write_text("    def inner():\n        pass\n")
    # old_string with 2-space indent instead of 4
    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = edit_file_atomic(
            str(f),
            old_string="  def inner():\n    pass",
            new_string="    def inner():\n        return True",
            workdir=tmp_path,
        )
    assert result["status"] == "ok", result.get("error")
    assert "return True" in f.read_text()


def test_edit_atomic_exact_still_works(tmp_path):
    """Exact matches continue to work after fuzzy logic is added."""
    from src.tools.file_tools import edit_file_atomic

    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n")
    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = edit_file_atomic(str(f), old_string="x = 1", new_string="x = 99", workdir=tmp_path)
    assert result["status"] == "ok"
    assert "x = 99" in f.read_text()


def test_edit_atomic_not_found_returns_error(tmp_path):
    """Returns error with fuzzy explanation when no strategy matches."""
    from src.tools.file_tools import edit_file_atomic

    f = tmp_path / "code.py"
    f.write_text("def hello(): pass\n")
    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = edit_file_atomic(str(f), old_string="NOTHERE", new_string="x", workdir=tmp_path)
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# P4 — multiedit
# ---------------------------------------------------------------------------

def test_multiedit_applies_all_edits(tmp_path):
    """multiedit applies all edits atomically."""
    from src.tools.file_tools import multiedit

    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")

    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = multiedit(
            str(f),
            edits=[
                {"old_string": "a = 1", "new_string": "a = 10"},
                {"old_string": "b = 2", "new_string": "b = 20"},
                {"old_string": "c = 3", "new_string": "c = 30"},
            ],
            workdir=tmp_path,
        )
    assert result["status"] == "ok"
    assert result["edits_applied"] == 3
    content = f.read_text()
    assert "a = 10" in content
    assert "b = 20" in content
    assert "c = 30" in content


def test_multiedit_aborts_on_missing_string(tmp_path):
    """multiedit aborts if any old_string is not found — file unchanged."""
    from src.tools.file_tools import multiedit

    f = tmp_path / "m.py"
    original = "x = 1\ny = 2\n"
    f.write_text(original)

    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = multiedit(
            str(f),
            edits=[
                {"old_string": "x = 1", "new_string": "x = 99"},
                {"old_string": "NOTHERE", "new_string": "z = 0"},
            ],
            workdir=tmp_path,
        )
    assert result["status"] == "error"
    assert f.read_text() == original  # file unchanged


def test_multiedit_rejects_ambiguous(tmp_path):
    """multiedit rejects if any old_string matches more than once."""
    from src.tools.file_tools import multiedit

    f = tmp_path / "m.py"
    f.write_text("x = 1\nx = 1\n")

    with patch("src.tools.guardrails.check_read_before_write", return_value=None):
        result = multiedit(
            str(f),
            edits=[{"old_string": "x = 1", "new_string": "x = 99"}],
            workdir=tmp_path,
        )
    assert result["status"] == "error"
    assert "times" in result["error"]


# ---------------------------------------------------------------------------
# P5 — batch tool
# ---------------------------------------------------------------------------

def test_batch_rejects_nested():
    """batch rejects a call list that contains another batch call."""
    from src.tools.batch_tools import batch

    result = batch(calls=[{"tool": "batch", "input": {"calls": []}}])
    assert result["status"] == "error"
    assert "nested" in result["error"].lower()


def test_batch_rejects_too_many_calls():
    """batch rejects more than 10 calls."""
    from src.tools.batch_tools import batch

    calls = [{"tool": "read_file", "input": {"path": f"f{i}.py"}} for i in range(11)]
    result = batch(calls=calls)
    assert result["status"] == "error"
    assert "10" in result["error"]


def test_batch_empty_calls():
    """batch rejects empty calls list."""
    from src.tools.batch_tools import batch

    result = batch(calls=[])
    assert result["status"] == "error"


def test_batch_missing_tool_key():
    """batch rejects entries without a 'tool' key."""
    from src.tools.batch_tools import batch

    result = batch(calls=[{"input": {}}])
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# P6 — bash description param
# ---------------------------------------------------------------------------

def test_bash_description_param_accepted(tmp_path):
    """bash() accepts description param and still runs the command."""
    from src.tools.file_tools import bash

    result = bash("echo hi", workdir=tmp_path, description="greeting test")
    assert result["status"] == "ok"
    assert result["stdout"].strip() == "hi"


# ---------------------------------------------------------------------------
# P7 — ask_user choices param
# ---------------------------------------------------------------------------

def test_ask_user_choices_validation():
    """ask_user rejects choices with fewer than 2 items."""
    from src.tools.interaction_tools import ask_user

    result = ask_user("Pick one", choices=["only one"])
    assert result["status"] == "error"
    assert "choices" in result["error"].lower()


def test_ask_user_no_choices_unchanged():
    """ask_user without choices still accepts non-empty questions."""
    # Just test the validation path (don't actually block on event bus)
    from src.tools.interaction_tools import ask_user

    # Empty question should still be rejected
    result = ask_user("", choices=None)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# P8 — read_web_page improvements
# ---------------------------------------------------------------------------

def test_read_web_page_http_upgraded():
    """HTTP URLs are upgraded to HTTPS before the request is made."""
    from src.tools.web_tools import read_web_page

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "<html><body>hello</body></html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        read_web_page("http://example.com/docs")

        called_url = mock_get.call_args[0][0]
        assert called_url.startswith("https://"), f"Expected https, got: {called_url}"


def test_read_web_page_invalid_format():
    """read_web_page rejects unknown format values."""
    from src.tools.web_tools import read_web_page

    result = read_web_page("https://example.com", format="pdf")
    assert result["status"] == "error"
    assert "format" in result["error"].lower()


def test_read_web_page_text_format():
    """format='text' strips HTML tags."""
    from src.tools.web_tools import read_web_page

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>Hello world</p></body></html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = read_web_page("https://example.com", format="text")
        assert result["status"] == "ok"
        assert "Hello world" in result["content"]


def test_read_web_page_larger_cap():
    """MAX cap is 100_000 (was 10_000)."""
    from src.tools import web_tools

    assert web_tools._MAX_WEB_PAGE_CHARS == 100_000


# ---------------------------------------------------------------------------
# P9 — format_file helper
# ---------------------------------------------------------------------------

def test_format_file_skipped_for_unknown_ext(tmp_path):
    """format_file returns skipped for .xyz files with no known formatter."""
    from src.tools.verification_tools import format_file

    f = tmp_path / "test.xyz"
    f.write_text("data")

    result = format_file(str(f))
    assert result["status"] == "skipped"


def test_format_file_skipped_for_missing_file():
    """format_file returns skipped when the file does not exist."""
    from src.tools.verification_tools import format_file

    result = format_file("/nonexistent/path/file.py")
    assert result["status"] == "skipped"


# ---------------------------------------------------------------------------
# P10 — skill tools
# ---------------------------------------------------------------------------

def test_load_skill_path_traversal_rejected():
    """load_skill rejects path-traversal attempts."""
    from src.tools.skill_tools import load_skill

    result = load_skill("../../etc/passwd")
    assert result["status"] == "error"
    assert "invalid" in result["error"].lower()


def test_load_skill_not_found_lists_available(tmp_path):
    """load_skill returns available skills when skill name not found but dir exists."""
    from src.tools import skill_tools

    original_dir = skill_tools._SKILLS_DIR
    try:
        skill_tools._SKILLS_DIR = tmp_path  # dir exists, but no skills in it
        result = skill_tools.load_skill("nonexistent_skill_xyz")
        assert result["status"] == "not_found"
        assert "available" in result
    finally:
        skill_tools._SKILLS_DIR = original_dir


def test_list_skills_returns_list():
    """list_skills always returns a list (may be empty if dir missing)."""
    from src.tools.skill_tools import list_skills

    result = list_skills()
    assert result["status"] == "ok"
    assert isinstance(result["skills"], list)


def test_load_skill_with_existing_skill(tmp_path):
    """load_skill reads an existing .md file from the skills directory."""
    from src.tools import skill_tools

    # Temporarily override _SKILLS_DIR
    original_dir = skill_tools._SKILLS_DIR
    try:
        skill_tools._SKILLS_DIR = tmp_path
        (tmp_path / "hello.md").write_text("# Hello Skill\nDo something.")

        result = skill_tools.load_skill("hello")
        assert result["status"] == "ok"
        assert "Hello Skill" in result["content"]
        assert result["name"] == "hello"
    finally:
        skill_tools._SKILLS_DIR = original_dir
