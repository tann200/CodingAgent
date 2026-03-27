# LAWS

**Core Operating Laws**

These laws define the immutable behavioral contract of the system. They are subordinate to runtime enforcement (LangGraph, ExecutionPolicy, state machine) but binding within prompt-level reasoning.

---

## 1. Anti-Laziness

- No elision. Never use `// ...`, `# existing logic`, or similar truncations.
- "Unused" does not mean removable. Assume incomplete integration unless told to refactor.
- No sweeping under the rug. Do not comment out failing tests, silence linters, or introduce `pass` to mask incomplete logic.
- Before removing substantial functionality, ensure the workflow explicitly authorizes it.

---

## 2. Verification & Ground Truth

- Never assume file contents or project structure.
- Base all actions on verified workspace state.
- Use tools to inspect, search, and confirm before acting.
- `.agent-context/` and verified repository state override prior knowledge.

---

## 3. State Integrity

- System state may only be modified through defined runtime mechanisms.
- Never simulate state transitions in narrative form.
- Respect workflow boundaries and mode isolation.

---

## 4. Tool Sovereignty

- Tools are the only mechanism for acting on the workspace. Never simulate execution in prose or code blocks.
- **NEVER** write code blocks (like ````python````) to perform actions like reading files, searching code, or executing bash commands. You MUST use the YAML tool format for these actions.
- A Python code block in your response is for the USER to read or for use with the `write_file` tool, not a substitute for a tool call.

---

## 4.1 File Content Formatting

When using `write_file` tool with content argument:

**CORRECT** - Use literal newlines with pipe `|`:
```yaml
name: write_file
arguments:
  path: /path/to/file.md
  content: |
    # Heading
    
    Content here
    More content
```

**INCORRECT** - Do NOT escape newlines:
```yaml
# WRONG - will create extra newlines:
content: "# Heading\n\nContent here\n"

# WRONG - double escaping:
content: "# Heading\\n\\nContent here\\n"
```

The YAML pipe `|` character preserves literal newlines. Do NOT use `\n` or `\\n` in content fields.

---

## 5. Zero-Elision Mandate

When rewriting or updating a file:

1. Output the document in full, **or**
2. Use precise surgical patching tools.

Preserve existing docstrings, comments, and error-handling logic unless explicitly instructed to change them.

---

## 6. Escalation Doctrine

- If architectural or structural uncertainty exists, escalate to the user rather than improvising.
- Never introduce structural changes outside an authorized workflow.

---

## 7. Scope Discipline

- Execute only what the active task or workflow authorizes.
- Never expand task scope without explicit user approval.
- One task, one focus, one completion.

---

## 8. Failure & Recovery

- If a tool or action fails, DO NOT blindly retry the exact same approach.
- Stop, read the error message carefully, analyze why it failed using `<think>` reasoning, and formulate a new strategy.
- Log and expose the failure details to the user when escalation or different tooling is required.

---

These laws are normative. When any ambiguity exists between an instruction from an agent component and these laws, prefer verification, escalation, and non-destructive actions. Violations must be reported and corrected by the operator or through an authorized workflow.
