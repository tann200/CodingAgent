---
name: debug_checklist
triggers: [bug, crash, error, exception, traceback, unexpected, broken, failing]
roles: [debugger, operational]
---
# Skill: Debug Checklist

## When to Use
Apply at the start of any debugging session, especially when the root cause is unclear or a quick fix was tried and failed.

## Strategy
Debug by narrowing the search space, not by guessing. Every hypothesis must be falsifiable with a concrete test.

## Execution Steps

1. **Read the full error message and traceback.**
   Do not skim. The exact exception type, message, and innermost frame usually contain the root cause. Copy the relevant lines before searching.

2. **Reproduce the bug in isolation.**
   Write the smallest possible reproducer script or test. If you cannot reproduce it, you cannot fix it reliably. Pin the inputs.

3. **Form a hypothesis.**
   State: "I believe the bug is caused by X because Y." If you cannot complete this sentence, you need more information — add logging, not a fix.

4. **Check the most common causes first (80/20 rule).**
   - `None` where a value is expected → missing guard, wrong key, uninitialized field
   - `AttributeError` → wrong type, None dereference, typo
   - `KeyError / IndexError` → off-by-one, wrong dict key, empty collection
   - `TypeError` → argument type mismatch, missing/extra argument
   - `ImportError` → missing dependency, circular import, wrong path
   - Incorrect output (no crash) → wrong algorithm, wrong variable used, mutation side-effect

5. **Binary search the call stack.**
   Add a logging/print statement at the midpoint of the execution path. If the bug is before → search upper half. If after → search lower half. Repeat.

6. **Check recent changes.**
   Run `git log --oneline -10` and `git diff HEAD~1` to see what changed last. The bug is often in the newest code.

7. **Fix only what is broken.**
   The fix should be as small as possible. If the fix is large, you are probably fixing a symptom, not the cause.

8. **Write a regression test.**
   Before closing: add a test that would have caught this bug. Confirm it fails before the fix and passes after.
