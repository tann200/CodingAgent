---
name: atomic-edit-rollback
description: Surgical, fault-tolerant file mutation pattern with pre-write syntax validation, exact match replacement, and deferred transaction rollback recovery.
author: CodingAgent
tags: [editing, safety, atomic-write, rollback, refactoring]
compatibility: [Claude Code, Cursor, Copilot, CodingAgent, Agensi]
version: 1.0.0
---

# Skill: Atomic Edit & Rollback Recovery

## When to Use
Apply this skill whenever editing code or configuration files. This prevents clobbering existing code, avoids introducing unescaped syntax errors, and guarantees clean rollbacks when unexpected failures occur.

## Core Rules

1. **Read-Before-Write Invariant**
   - NEVER write or edit an existing file without reading it first in the current session.
   - Always verify the exact lines and formatting surrounding the target code block.

2. **Atomic Precise Replacements**
   - Prefer exact string replacement (`old_string` -> `new_string`) over full file overwrites whenever modifying existing files.
   - Ensure `old_string` includes enough context lines (2-4 lines) to guarantee uniqueness within the file.
   - Preserve existing indentation (tabs vs spaces, 2 vs 4 spaces), quote styles, and trailing newlines.

3. **Pre-Write Verification**
   - Validate proposed changes in memory before committing to disk.
   - Check AST / syntax validity before finalizing the file write. If a syntax error is detected, reject the write and adjust the replacement string.

4. **Deferred Rollback & Surgical Retry**
   - If a multi-step task encounters a failure on step N, do not immediately discard working progress from steps 1..N-1.
   - Inspect the exact diagnostic failure (e.g. unclosed parenthesis or misplaced import).
   - Apply a surgical fix to resolve the error.
   - Only execute an automated rollback if the recovery budget is exhausted or if user cancellation is requested.

## Failure Mode Checklist

| Symptom | Root Cause | Remedy |
|---------|------------|--------|
| `old_string not found` | Whitespace mismatch or stale file state | Re-read the file with `read_file` to capture fresh line content. |
| `SyntaxError: unexpected EOF` | Truncated edit or unclosed delimiter | Ensure closing braces/parentheses are present in `new_string`. |
| Read-before-write violation | File was not inspected in session | Call `read_file` before calling edit/write tools. |
