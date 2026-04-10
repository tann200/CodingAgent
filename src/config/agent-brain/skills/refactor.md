---
name: refactor
triggers: [refactor, clean up, extract, restructure, technical debt]
roles: [operational, strategic, analyst]
---
# Skill: Refactor

## When to Use
Apply when code is working but hard to understand, duplicate, or tightly coupled. Do NOT refactor and add features simultaneously — separate the commits.

## Strategy
Refactor in the smallest safe steps. Each step must leave tests green. Work from the innermost scope outward.

## Execution Steps

1. **Establish a safety net first.**
   Before touching code: confirm the relevant tests pass. If tests are absent, write them first (apply write_tests skill). Refactoring without a test safety net is rewriting.

2. **Identify the smell.**
   Common smells:
   - Long method (>40 lines) → extract helper functions
   - Repeated logic (≥2 occurrences) → extract shared utility (apply DRY skill)
   - Deeply nested conditions (>3 levels) → invert conditions / early return
   - Large parameter list (>4 params) → introduce a config dataclass / TypedDict
   - Mixed abstraction levels in one function → split into orchestrator + workers

3. **One refactor, one commit.**
   Each refactor step (rename, extract, move) should be a separate, reviewable change. Batch changes make diffs unreadable and bugs harder to bisect.

4. **Rename for clarity first.**
   - Rename ambiguous variables/functions before extracting them.
   - Use the existing naming convention in the file (snake_case, CamelCase, etc.).

5. **Extract, don't rewrite.**
   When extracting a helper, copy the exact logic first, then simplify inside the helper. Confirm tests still pass after each step.

6. **Move to the right layer.**
   - Business logic belongs in domain modules, not in I/O handlers.
   - I/O (file reads, network) belongs at the edges; pass data inward.

7. **Verify.**
   Run the full test suite after each refactor step. If a step breaks tests, revert immediately and try a smaller step.
