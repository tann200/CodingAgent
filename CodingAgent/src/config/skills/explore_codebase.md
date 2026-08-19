# Explore Codebase Skill

Efficient strategy for understanding an unfamiliar codebase before making changes.

## Phase 1: Orient (2–3 minutes)

```
1. list_files(".")                          # top-level structure
2. bash("git log --oneline -10")            # recent history reveals intent
3. bash("git diff HEAD~1 --name-only")      # what changed recently
```

Look for: `README.md`, `pyproject.toml`/`package.json`/`Cargo.toml`, `Makefile`, `docker-compose.yml`.

## Phase 2: Find Entry Points

- **Python**: `main.py`, `__main__.py`, `app.py`, `cli.py`
- **TypeScript/JS**: `index.ts`, `main.ts`, `App.tsx`, `server.ts`
- **Go**: `main.go`, `cmd/`
- **Rust**: `src/main.rs`, `src/lib.rs`

Use `find_symbol("main")` and `glob("**/main.*")`.

## Phase 3: Trace the Relevant Path

Once you know the entry point:
1. `find_references(symbol)` — who calls this?
2. `find_symbol(symbol)` — where is it defined?
3. `grep(pattern, path)` — search for specific strings, error messages, config keys

## Phase 4: Batch Independent Reads

Use `batch` to read multiple files simultaneously:
```json
{"tool": "batch", "input": {"calls": [
  {"tool": "read_file", "input": {"path": "src/core/main.py"}},
  {"tool": "read_file", "input": {"path": "src/config/settings.py"}},
  {"tool": "bash", "input": {"command": "grep -r 'TODO' src/ --include='*.py' -l"}}
]}}
```

## Phase 5: Summarise Before Acting

Before making any changes, write a one-paragraph summary:
- What the relevant code does
- Which files will be affected
- What the correct approach is

Only then proceed to make changes.
