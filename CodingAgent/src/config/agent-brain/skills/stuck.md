# Skill Name: Stuck Auto-Recovery

## When to Use

Apply this skill when the agent detects it is **stuck** — i.e. repeating the same
action or producing the same failed output three or more times without progress.
Indicators include:

- `no_plan_fail_count ≥ 2` in `AgentState`
- Identical tool calls in consecutive turns
- A loop detected by `_check_loop_prevention()`
- Debug attempts (`total_debug_attempts`) exceeding the budget
- Consecutive `STATUS:failed` results for the same plan step

## Strategy

The core strategy is to **break the cycle**, gather fresh context, and either
reformulate the plan or escalate to the user.

## Execution Steps

1. **Acknowledge the loop.**  Emit a brief note to the conversation: "I seem to be
   stuck. Stepping back to reconsider."

2. **Stop repeating the same action.**  Do not retry the identical tool call with
   the same arguments.  If the previous attempt failed, the failure is *information*.

3. **Re-read the task specification.**  Use `read_file` to load the relevant source
   files or specification documents fresh — avoid relying solely on context-window
   content that may have drifted.

4. **Check what changed.**  Run `git_status` or `bash("git diff --stat HEAD")`
   to see exactly what the agent has modified so far.  Compare this to what the
   task requires.

5. **Diagnose the root cause.**  Ask: Why did the last attempt fail?
   - Wrong file path?  → Use `glob` to find the correct path.
   - Wrong API / interface?  → Re-read the relevant module.
   - Syntax error in generated code?  → Run `syntax_check` before writing.
   - Tool not available?  → List available tools and choose an alternative.

6. **Reformulate.**  If the current plan is broken, drop it and produce a minimal
   replacement plan targeting only the failing sub-step.  Prefer smaller atomic
   actions over broad multi-file rewrites.

7. **Escalate if stuck after three reformulations.**  If this skill has been
   applied three times on the same task without progress, surface a clear error
   message to the user describing:
   - What was attempted
   - What error was produced
   - What specific information or action is needed from the user

## Anti-patterns to Avoid

- **Do not** call `read_file` on the same already-loaded file without a new
  diagnostic question.
- **Do not** write to a file without first verifying the intended change is
  syntactically valid.
- **Do not** retry a bash command that returned a non-zero exit code without
  understanding the error message.
- **Do not** generate a new full plan without referencing the partial progress
  already achieved.

## Integration with AgentState

The orchestrator increments `no_plan_fail_count` and `total_debug_attempts`
automatically.  This skill is triggered when these counters exceed their
respective thresholds (configurable in `initial_state`; defaults: 3 and 5).
After applying this skill, expect the graph to route through `analysis_node`
for deeper context before re-entering `planning_node`.
