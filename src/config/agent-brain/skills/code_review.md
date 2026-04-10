---
name: code_review
triggers: [review, pr, pull request, code quality, LGTM]
roles: [reviewer, analyst]
---
# Skill: Code Review

## When to Use
Apply when asked to review a pull request, diff, or code change. Also apply proactively when you have just written or modified code and want to self-review before completing a task.

## Strategy
Systematic multi-pass review: correctness first, then security, then style. Never approve code you cannot explain.

## Execution Steps

1. **Understand intent before reading code.**
   Read the PR description or task context first. Know *what* the code is supposed to do before judging *how* it does it.

2. **Pass 1 — Correctness.**
   - Does the logic match the stated intent?
   - Are edge cases handled (empty input, None, overflow, off-by-one)?
   - Are error paths handled and meaningful errors surfaced to callers?
   - Are async/concurrent paths safe (race conditions, missing awaits)?

3. **Pass 2 — Security.**
   - Is user-supplied input validated/sanitised before use?
   - Are secrets/credentials handled correctly (no hardcoding, no logging)?
   - Are file paths validated against path-traversal?
   - Are subprocess calls using shell=False or equivalent?

4. **Pass 3 — Maintainability.**
   - Does the code follow existing naming conventions in the file?
   - Are functions short enough to be testable in isolation (<40 lines preferred)?
   - Is duplicated logic extracted (apply DRY skill)?
   - Are public functions/methods documented with a short docstring?

5. **Pass 4 — Test coverage.**
   - Does a test exist for the happy path?
   - Does a test exist for the primary error path?
   - Are new branches reachable by existing tests?

6. **Output format.**
   - List findings by severity: BLOCKER > WARNING > SUGGESTION.
   - For each finding: file path, line range, brief description, suggested fix.
   - End with an explicit APPROVE / REQUEST_CHANGES verdict.
