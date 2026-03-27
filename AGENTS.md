# AGENTS.md - Agent Instructions

## Overview
This file provides instructions for AI coding agents working in this project.

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
