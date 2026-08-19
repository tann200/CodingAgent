---
name: write_tests
triggers: [test, unit test, pytest, coverage, TDD]
roles: [reviewer, operational, debugger]
---
# Skill: Write Tests

## When to Use
Apply when asked to add tests, when you've just written a new function/class, or when debugging a regression (write a failing test first to pin the bug).

## Strategy
Tests are specifications written in code. A good test is readable as documentation — it tells the reader what the subject does, not just that it runs.

## Execution Steps

1. **Locate the test file before creating one.**
   Run `glob("tests/**/*<module_name>*")` to check if a test file already exists. Prefer adding to an existing file over creating a new one.

2. **Follow the AAA pattern (Arrange, Act, Assert).**
   ```python
   def test_function_does_x_when_y():
       # Arrange
       input_data = ...
       # Act
       result = function_under_test(input_data)
       # Assert
       assert result == expected
   ```

3. **Test naming: `test_<unit>_<behaviour>_<condition>`.**
   Good: `test_parse_config_returns_empty_dict_when_file_missing`
   Bad: `test_parse_config_1`

4. **Scope: one logical assertion per test.**
   Multiple `assert` statements are fine when they all verify the same logical outcome. Do not test multiple independent behaviours in one test.

5. **Fixtures over setUp/tearDown.**
   Use `@pytest.fixture` for reusable setup. Keep fixtures in the same file unless shared across 3+ test files (then use `conftest.py`).

6. **Mock at the boundary, not inside the unit.**
   Mock I/O, network, and time. Do not mock internal helpers of the function being tested — that couples the test to implementation.

7. **Cover at minimum:**
   - Happy path (expected input, expected output)
   - Primary error path (expected exception or error return)
   - Boundary: empty input, zero, None, empty list

8. **Run after writing.**
   Always execute the test with `pytest <path> -x` to confirm it passes before marking the task complete.
