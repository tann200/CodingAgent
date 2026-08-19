# Write Tests Skill

Guidelines for writing effective regression and unit tests.

## Principles

- **Test behaviour, not implementation** — call public APIs; don't reach into private state.
- **One assertion per concern** — split unrelated assertions into separate test functions.
- **Name the scenario** — `test_read_file_binary_rejected`, not `test_read_file_2`.
- **Arrange / Act / Assert** — set up inputs, call the code, check the output.

## Structure

```python
def test_<function>_<scenario>():
    """One-line description of what this test proves."""
    # Arrange
    ...
    # Act
    result = function_under_test(...)
    # Assert
    assert result["status"] == "ok"
    assert "expected_key" in result
```

## What to Cover

1. **Happy path** — normal valid input returns expected output.
2. **Error path** — invalid input returns `{"status": "error", "error": "..."}`.
3. **Edge cases** — empty string, None, zero, very large input, Unicode.
4. **Boundary** — one below and one above any documented limit.

## Mocking

- Mock external I/O (network, filesystem) using `unittest.mock.patch`.
- Prefer `tmp_path` fixture for real filesystem operations.
- Never mock the unit under test; only mock its dependencies.

## Running

```bash
.venv/bin/pytest tests/unit/test_<module>.py -p no:logging -v
```
