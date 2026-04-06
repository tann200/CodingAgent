# Refactor Skill

Safe, incremental approach to improving code structure without changing behaviour.

## Rules

1. **Never change behaviour and structure in the same commit.**
   - Refactor first (tests still green), then add features.

2. **Keep each change small and verifiable.**
   - Rename one thing at a time.
   - Extract one function at a time.

3. **Run tests after every step.**
   - `run_tests(workdir)` must pass before moving to the next change.

## Common Refactors

### Extract Function
- Identify a block of code that does one thing.
- Move it to a named function.
- Call the function from the original site.
- Verify tests pass.

### Rename
- Use `grep` to find all usages before renaming.
- Update all call sites.
- Run tests.

### Remove Duplication
- Find the two (or more) duplicate blocks with `grep`.
- Extract to a shared helper.
- Replace all copies with calls to the helper.
- Run tests.

### Simplify Condition
- Replace nested ifs with guard clauses (early return).
- Replace boolean expressions with named variables.

## What NOT to Do
- Do not reformat unrelated code.
- Do not change variable names unless they are actively misleading.
- Do not add features during a refactor.
