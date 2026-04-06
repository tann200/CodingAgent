# Debug Checklist

Systematic approach to diagnosing and fixing bugs.

## Steps

1. **Reproduce** — confirm the bug is reproducible before changing anything.
   - Run the failing test: `run_tests(workdir, test_files=[...])`
   - If no test exists, read the error and identify the input that triggers it.

2. **Locate** — find where the failure originates.
   - Read the full traceback; start at the innermost frame.
   - Use `grep` to find the relevant function/class.
   - Use `find_symbol` + `find_references` to understand call paths.

3. **Understand** — read the code around the failure before touching anything.
   - `read_file` the file at the failure line.
   - Check recent git history: `bash("git log --oneline -10 -- <file>")`

4. **Hypothesise** — form a specific theory about the root cause.
   - Write it out before editing.

5. **Fix** — make the minimal change that addresses the root cause.
   - Prefer `edit_file_atomic` for surgical edits.
   - Do NOT refactor surrounding code unless directly related.

6. **Verify** — re-run the test to confirm the fix.
   - Run `run_tests` targeting the specific test file first.
   - Then run the broader suite to check for regressions.

## Common Patterns

- **KeyError / AttributeError**: check whether the key/attribute can be absent; add a guard or default.
- **TypeError**: check argument types at the call site; check if a None is being passed.
- **Import error**: check whether the module exists; check circular imports.
- **Test isolation**: check whether the test leaves shared state; reset global state in teardown.
