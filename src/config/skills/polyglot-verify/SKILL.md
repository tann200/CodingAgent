---
name: polyglot-verify
description: Multi-language syntax, type, and test verification suite with zero-overhead compiler invocation across Python, TypeScript/JavaScript, Go, Rust, and C/C++.
author: CodingAgent
tags: [verification, testing, syntax, polyglot, quality]
compatibility: [Claude Code, Cursor, Copilot, CodingAgent, Agensi]
version: 1.0.0
---

# Skill: Polyglot Verification

## When to Use
Apply this skill whenever code in any language has been modified or generated, before declaring a task complete or requesting user review. Proactively detect syntax regressions, broken imports, missing types, and broken test assertions.

## Strategy
Always verify changes locally and hierarchically:
1. **Pre-flight Syntax Check**: Zero-dependency compilation/AST parse first (fastest feedback).
2. **Type Diagnostics / Linters**: Static type checker and linter execution.
3. **Targeted Test Execution**: Run only the tests directly related to modified modules before running wider regression suites.
4. **Baseline Deduplication**: Never fail on preexisting issues in untouched files. Only flag new errors introduced by the current changes.

## Language Protocols

### 1. Python
- **Syntax**: `python -m py_compile <file.py>` (instant AST check).
- **Type Checking**: `python -m mypy <file.py> --ignore-missing-imports`.
- **Linting**: `python -m ruff check <file.py>`.
- **Unit Testing**: `python -m pytest <test_file.py> -q -p no:logging`.

### 2. TypeScript & JavaScript
- **Syntax & Execution**: `node --check <file.js>` for pure JS syntax.
- **Type Checking**: `npx tsc --noEmit` or `tsc --project tsconfig.json --noEmit`.
- **Linting**: `npx eslint <file> --max-warnings 0`.
- **Testing**: `npm test -- <test_spec>`.

### 3. Go
- **Syntax & Types**: `go vet ./...` or `go build -v -o /dev/null ./...`.
- **Testing**: `go test -v -run <TestName> ./...`.

### 4. Rust
- **Fast Syntax & Check**: `cargo check --tests`.
- **Linting**: `cargo clippy -- -D warnings`.
- **Testing**: `cargo test --test <test_name>`.

### 5. C / C++
- **Syntax Verification**: `clang -fsyntax-only <file.c>` or `g++ -fsyntax-only <file.cpp>`.
- **Build / Test**: `make test` or `ctest`.

## Execution Discipline
- If verification fails with a syntax error, do not panic or revert immediately. Read the error line, inspect the surroundings, and apply a surgical edit to fix the missing token/paren/import.
- If a test fails, verify whether the test itself had stale expectations or the implementation had a logic flaw.
- Keep execution confined to the workspace root.
