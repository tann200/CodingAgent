# LAWS

- **Zero elision**: Never truncate delivered code with `// ...`, `# existing logic`, or placeholders; never silence failing tests or linters to mask broken logic.
- **Ground truth**: Never guess file contents or repository structure; inspect via tools before modifying.
- **Tool sovereignty**: Execute all workspace changes via native tool calls, never as plain-text code blocks simulating edits.
- **Minimal scope**: Change only what the active task authorizes; preserve existing behavior and formatting.
- **Adaptive recovery**: On any tool or verification failure, inspect the diagnostic and change strategy rather than repeating the same failing call.
