---
name: git-safe-review
description: Non-destructive, read-only Git status, diff, and change inspection workflow designed to safeguard repository integrity during autonomous agent execution.
author: CodingAgent
tags: [git, diff, review, safety, secrets]
compatibility: [Claude Code, Cursor, Copilot, CodingAgent, Agensi]
version: 1.0.0
---

# Skill: Safe Git Inspection & Review

## When to Use
Apply this skill whenever reviewing git changes, preparing a summary of work completed, checking modified worktrees, or verifying that no unintended files or credentials have been touched.

## Guiding Principles
- **Read-Only Inspection**: Use non-mutating Git operations (`git status`, `git diff`, `git log`) for evaluation.
- **Never Run Destructive Commands**: Never run `git checkout -- .`, `git reset --hard`, `git clean -fd`, or force pushes unless explicitly requested by the user.
- **Secrets Sanitization**: Scan every diff for leaked API keys, tokens, `.env` files, or passwords before committing or finishing a task.

## Inspection Procedure

### 1. Check Working Tree State
```bash
git status --porcelain
```
- Verify which files are modified (`M`), added (`??`), or deleted (`D`).
- Confirm only task-relevant files are touched. Untracked temporary files should be cleaned up.

### 2. Inspect Exact Code Differences
```bash
git diff
```
- Verify each hunk for correctness and minimal scope.
- Ensure no unintended whitespace churn, accidental formatting changes, or commented-out debug code remain.

### 3. Check Staged Changes (if applicable)
```bash
git diff --cached
```
- Confirm staged files match intended commit scope.

### 4. Secret & Sensitive File Audit
Before presenting completed work, ensure the following are NEVER committed:
- Files named `.env*`, `credentials.json`, `id_rsa`, `*.pem`, `*.key`.
- In-line strings containing high-entropy tokens or prefixes such as `sk-`, `ghp_`, `bearer `, `AKIA`.

### 5. Structured Review Output
Report findings clearly using the following format:
- **Files Modified**: List of exact relative paths changed.
- **Core Summary**: Brief description of the functional change.
- **Verification Status**: Test and syntax verification results.
