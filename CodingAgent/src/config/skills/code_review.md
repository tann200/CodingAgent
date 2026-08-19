# Code Review Skill

Systematic approach to reviewing a code change for correctness, quality, and security.

## Review Dimensions

### 1. Correctness
- Does the code do what the task description asks?
- Are edge cases handled (empty inputs, None, zero, large values)?
- Are error paths covered and do they return meaningful messages?
- Are there off-by-one errors in loops or slice indices?

### 2. Security
- Is user-supplied input validated before use?
- Are file paths sanitised against path traversal (`..`)?
- Are shell commands built with user input? (→ injection risk)
- Are secrets/credentials ever logged or stored in plain text?

### 3. Quality
- Is the change minimal? (no unrelated edits)
- Are new functions named clearly?
- Is duplicated logic extracted into a helper?
- Are constants named (not magic numbers)?

### 4. Tests
- Is there a test for the new behaviour?
- Do existing tests still pass?
- Does the test cover the failure path, not just the happy path?

## Checklist Output Format

After reviewing, report findings as:
```
PASS  — no issues found
WARN  — non-blocking issue (explain)
FAIL  — blocking issue (explain, suggest fix)
```
