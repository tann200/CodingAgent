"""
Automatic Repo Summary - Generates quick overview of repository structure.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional


def detect_framework(workdir: str) -> Optional[str]:
    """Detect web framework from imports and files."""
    workdir_path = Path(workdir)

    # Check for common frameworks
    framework_indicators = {
        "FastAPI": ["from fastapi import", "FastAPI()", "uvicorn"],
        "Flask": ["from flask import", "Flask("],
        "Express": ["express()", "require('express')"],
        "React": ["import React", "create-react-app"],
        "Vue": ["import Vue", "createApp("],
        "Angular": ["@Component", "ng serve"],
        "Spring": ["@SpringBootApplication"],
        "Rails": ["rails new", "application.rb"],
        "Laravel": ["Illuminate\\", "artisan serve"],
    }

    # Scan Python files for imports
    py_files = list(workdir_path.rglob("*.py"))
    for py_file in py_files[:20]:  # Limit scanning
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for framework, indicators in framework_indicators.items():
                if any(ind in content for ind in indicators):
                    return framework
        except Exception:
            continue

    return None


def detect_languages(workdir: str) -> List[str]:
    """Detect programming languages in repo."""
    workdir_path = Path(workdir)
    languages = set()

    extensions = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".jsx": "JavaScript",
        ".tsx": "TypeScript",
        ".java": "Java",
        ".go": "Go",
        ".rs": "Rust",
        ".cpp": "C++",
        ".c": "C",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
        ".swift": "Swift",
        ".kt": "Kotlin",
    }

    # Single-pass directory scan instead of one rglob per extension (performance fix)
    _EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules"}
    for f in workdir_path.rglob("*"):
        if f.is_file() and not any(part in _EXCLUDE_DIRS for part in f.parts):
            lang = extensions.get(f.suffix.lower())
            if lang:
                languages.add(lang)

    return sorted(list(languages))


def detect_test_framework(workdir: str) -> Optional[str]:
    """Detect test framework."""
    workdir_path = Path(workdir)

    # Check for test files and configs
    test_indicators = {
        "pytest": ["pytest.ini", "conftest.py", "test_*.py", "tests/"],
        "unittest": ["unittest", "test_*.py"],
        "jest": ["jest.config.js", "jest.config.ts"],
        "vitest": ["vitest.config.js", "vitest.config.ts"],
        "mocha": ["mocha.opts", ".mocharc"],
        "rspec": ["spec/", "_spec.rb"],
        "go test": ["*_test.go"],
    }

    for framework, patterns in test_indicators.items():
        for pattern in patterns:
            if "*" in pattern:
                if list(workdir_path.rglob(pattern)):
                    return framework
            else:
                if (workdir_path / pattern).exists():
                    return framework

    return None


def detect_entrypoints(workdir: str) -> List[str]:
    """Detect entry point files."""
    workdir_path = Path(workdir)
    entrypoints = []

    common_entrypoints = [
        "main.py",
        "app.py",
        "server.py",
        "api.py",
        "index.js",
        "index.ts",
        "main.js",
        "main.ts",
        "main.go",
        "main.rs",
        "main.java",
        "run.py",
        "serve.py",
        "__main__.py",
    ]

    for entry in common_entrypoints:
        if (workdir_path / entry).exists():
            entrypoints.append(entry)

    # Look for __main__.py in packages
    for main_file in workdir_path.rglob("__main__.py"):
        entrypoints.append(str(main_file.relative_to(workdir_path)))

    return entrypoints


def list_modules(workdir: str) -> List[str]:
    """List top-level modules/packages."""
    workdir_path = Path(workdir)
    modules = []

    # Python packages
    for item in workdir_path.iterdir():
        if item.is_dir():
            if (item / "__init__.py").exists():
                modules.append(item.name)
            elif item.suffix == "" and not item.name.startswith("."):
                modules.append(item.name)

    # JS/TS packages
    if (workdir_path / "src").exists():
        for item in (workdir_path / "src").iterdir():
            if item.is_dir():
                modules.append(f"src/{item.name}")

    return sorted(modules)[:10]  # Limit to 10


def find_dependency_files(workdir: str) -> List[str]:
    """Find dependency/configuration files."""
    workdir_path = Path(workdir)
    deps = []

    dep_files = [
        "requirements.txt",
        "Pipfile",
        "Pipfile.lock",
        "pyproject.toml",
        "package.json",
        "yarn.lock",
        "package-lock.json",
        "bun.lockb",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ]

    for dep_file in dep_files:
        if (workdir_path / dep_file).exists():
            deps.append(dep_file)

    return deps


def generate_repo_summary(workdir: str) -> Dict[str, Any]:
    """Generate comprehensive repository summary.

    Returns:
        Dict containing:
        - framework: Main framework (e.g., FastAPI, Django)
        - languages: List of programming languages
        - test_framework: Test framework used
        - entrypoints: List of entry point files
        - modules: Top-level modules
        - dependency_files: Dependency configuration files
    """
    framework = detect_framework(workdir)
    languages = detect_languages(workdir)
    test_framework = detect_test_framework(workdir)
    entrypoints = detect_entrypoints(workdir)
    modules = list_modules(workdir)
    dependency_files = find_dependency_files(workdir)

    return {
        "framework": framework,
        "languages": languages,
        "test_framework": test_framework,
        "entrypoints": entrypoints,
        "modules": modules,
        "dependency_files": dependency_files,
        "summary": _format_summary(framework, languages, test_framework, modules),
    }


def _format_summary(
    framework: Optional[str],
    languages: List[str],
    test_framework: Optional[str],
    modules: List[str],
) -> str:
    """Format summary as readable string."""
    parts = []

    if framework:
        parts.append(f"Framework: {framework}")
    if languages:
        parts.append(f"Languages: {', '.join(languages)}")
    if test_framework:
        parts.append(f"Tests: {test_framework}")
    if modules:
        parts.append(f"Modules: {', '.join(modules[:5])}")

    return " | ".join(parts) if parts else "Unknown project structure"


# Tool wrapper
def summarize_repo(workdir: str = ".") -> Dict[str, Any]:
    """Tool wrapper for repo summary."""
    try:
        summary = generate_repo_summary(workdir)
        return {"status": "ok", **summary}
    except Exception as e:
        return {"status": "error", "error": str(e)}
