# AGENTS.md - Agent Instructions

## Overview

This file provides instructions for AI coding agents working in this project.

## Agent Definitions

### Available Agents

| Agent | Description |
|-------|-------------|
| coding | Default agent for coding tasks, file operations, and general development |
| analyst | Deep-dive code analysis, patterns, and architecture review |
| planning | Task breakdown, strategy, and planning |
| review | Code review, verification, and quality checks |
| debugging | Error investigation and fix suggestions |

### Agent Dispatch

Use `delegate_task` to delegate tasks to specialized subagents.

#### Syntax

```python
delegate_task(
    role="analyst",
    subtask_description="Analyze the authentication flow for security issues",
    working_dir="/path/to/project"
)
```

#### When to Dispatch

- **analyst**: Deep code analysis, pattern detection, architecture review
- **planning**: Complex task breakdown, strategy formulation
- **review**: Code review, test verification, quality checks
- **debugging**: Error investigation, bug hunting
- **operational**: File operations, refactoring, migrations

#### Example Patterns

```python
# Delegate code analysis
delegate_task(
    role="analyst",
    subtask_description="Find all uses of deprecated APIs in the codebase"
)

# Delegate code review
delegate_task(
    role="review", 
    subtask_description="Review the authentication module for security issues"
)

# Delegate debugging
delegate_task(
    role="debugging",
    subtask_description="Investigate why login fails with valid credentials"
)
```

## Tool Call Format

### write_file
When writing file content, output the **actual content** not escaped newlines:

```yaml
name: write_file
arguments:
  path: /path/to/file.md
  content: |
    # Heading
    
    Content here
    More content
```

**IMPORTANT**: Do NOT escape newlines as `\n`. Use literal newlines in the content field.

### edit_file  
When editing, use the exact content to replace:

```yaml
name: edit_file
arguments:
  path: /path/to/file.md
  oldString: |
    Old content
    to replace
  newString: |
    New content
    here
```

## File Content Guidelines

1. **Use literal newlines** - Not `\n` or `\\n`
2. **No trailing newlines** - Don't add extra blank lines at end of files
3. **Clean formatting** - One blank line between sections, not multiple

## Diff Display

- Diffs are automatically shown in the UI
- No need to manually format diffs in responses
- Focus on describing what changed, not showing the diff

## Task Completion

When a task is complete:
1. Output a brief summary of what was done
2. Do NOT read back the file to verify (the system handles this)
3. Move on to next task or indicate completion
