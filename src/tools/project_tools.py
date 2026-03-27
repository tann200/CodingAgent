"""
Project tools for tech-stack detection.

Provides fingerprint_tech_stack() which scans the workspace for manifest
files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.) and returns
a structured summary of detected languages, frameworks, and tools.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from src.tools._tool import tool

logger = logging.getLogger(__name__)


def _safe_read(path: Path, max_chars: int = 50_000) -> str:
    """Read a file safely, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


@tool(tags=["planning", "coding"])
def fingerprint_tech_stack(workdir: str = None) -> Dict[str, Any]:
    """Detect languages, frameworks, and tools present in the workspace.

    Scans for package.json, pyproject.toml, Cargo.toml, go.mod, pom.xml,
    Gemfile, Dockerfile, and other manifest files. Returns a structured
    summary of detected technologies, test runners, and build tools.

    Args:
        workdir: Working directory (defaults to cwd).

    Returns:
        status, languages, frameworks, test_runners, build_tools, manifests_found.
    """
    root = Path(workdir) if workdir else Path.cwd()
    languages: List[str] = []
    frameworks: List[str] = []
    test_runners: List[str] = []
    build_tools: List[str] = []
    manifests_found: List[str] = []
    has_docker = False
    has_ci = False
    ci_providers: List[str] = []

    # Python
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        manifests_found.append("pyproject.toml")
        if "python" not in languages:
            languages.append("python")
        content = _safe_read(pyproject)
        for fw in ("fastapi", "django", "flask", "pyramid", "starlette", "tornado"):
            if fw in content.lower():
                frameworks.append(fw)
        if "pytest" in content.lower():
            test_runners.append("pytest")
        if "poetry" in content.lower() or "[tool.poetry]" in content:
            build_tools.append("poetry")
        if "ruff" in content.lower():
            build_tools.append("ruff")

    setup_py = root / "setup.py"
    if setup_py.exists():
        manifests_found.append("setup.py")
        if "python" not in languages:
            languages.append("python")
        if "setuptools" not in build_tools:
            build_tools.append("setuptools")

    req_files = list(root.glob("requirements*.txt"))
    for rf in req_files:
        manifests_found.append(rf.name)
        if "python" not in languages:
            languages.append("python")

    # JavaScript / TypeScript
    package_json = root / "package.json"
    if package_json.exists():
        manifests_found.append("package.json")
        content = _safe_read(package_json)
        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {})

        # Note: pathlib.glob does not support brace expansion — use separate globs
        has_ts = any(True for _ in root.glob("**/*.ts")) or any(
            True for _ in root.glob("**/*.tsx")
        )
        if has_ts:
            languages.append("typescript")
        if "javascript" not in languages:
            languages.append("javascript")

        for fw in (
            "react",
            "next",
            "vue",
            "angular",
            "svelte",
            "express",
            "fastify",
            "koa",
        ):
            if fw in deps:
                frameworks.append(fw)
        if "jest" in deps:
            test_runners.append("jest")
        if "vitest" in deps:
            test_runners.append("vitest")
        if "eslint" in deps:
            build_tools.append("eslint")
        if "vite" in deps:
            build_tools.append("vite")
        if "webpack" in deps:
            build_tools.append("webpack")
        if "test" in scripts and "jest" in str(scripts["test"]).lower():
            if "jest" not in test_runners:
                test_runners.append("jest")

    # Rust
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.exists():
        manifests_found.append("Cargo.toml")
        languages.append("rust")
        content = _safe_read(cargo_toml)
        for fw in ("actix-web", "rocket", "axum", "tokio", "async-std"):
            if fw in content:
                frameworks.append(fw)
        build_tools.append("cargo")

    # Go
    go_mod = root / "go.mod"
    if go_mod.exists():
        manifests_found.append("go.mod")
        languages.append("go")
        content = _safe_read(go_mod)
        for fw in ("gin-gonic/gin", "go-chi/chi", "labstack/echo", "gofiber/fiber"):
            if fw in content:
                frameworks.append(fw)
        build_tools.append("go")
        if (root / "go.sum").exists():
            test_runners.append("go test")

    # Java
    pom_xml = root / "pom.xml"
    if pom_xml.exists():
        manifests_found.append("pom.xml")
        languages.append("java")
        content = _safe_read(pom_xml)
        for fw in ("spring-boot", "springframework", "quarkus", "micronaut"):
            if fw in content:
                frameworks.append(fw)
        build_tools.append("maven")

    build_gradle = root / "build.gradle"
    if build_gradle.exists():
        manifests_found.append("build.gradle")
        if "java" not in languages:
            languages.append("java")
        build_tools.append("gradle")

    # Ruby
    gemfile = root / "Gemfile"
    if gemfile.exists():
        manifests_found.append("Gemfile")
        languages.append("ruby")
        content = _safe_read(gemfile)
        for fw in ("rails", "sinatra", "hanami"):
            if fw in content:
                frameworks.append(fw)
        if "rspec" in content:
            test_runners.append("rspec")
        if "rubocop" in content:
            build_tools.append("rubocop")

    # Docker
    dockerfile = root / "Dockerfile"
    if dockerfile.exists():
        manifests_found.append("Dockerfile")
        has_docker = True

    docker_compose = root / "docker-compose.yml"
    if not docker_compose.exists():
        docker_compose = root / "compose.yml"
    if docker_compose.exists():
        manifests_found.append(docker_compose.name)
        has_docker = True

    # CI
    workflows_dir = root / ".github" / "workflows"
    if workflows_dir.is_dir():
        for wf in workflows_dir.iterdir():
            if wf.suffix in (".yml", ".yaml"):
                manifests_found.append(f".github/workflows/{wf.name}")
                has_ci = True
                ci_providers.append("github_actions")

    gitlab_ci = root / ".gitlab-ci.yml"
    if gitlab_ci.exists():
        manifests_found.append(".gitlab-ci.yml")
        has_ci = True
        ci_providers.append("gitlab_ci")

    jenkinsfile = root / "Jenkinsfile"
    if jenkinsfile.exists():
        manifests_found.append("Jenkinsfile")
        has_ci = True
        ci_providers.append("jenkins")

    return {
        "status": "ok",
        "languages": languages,
        "frameworks": frameworks,
        "test_runners": test_runners,
        "build_tools": build_tools,
        "has_docker": has_docker,
        "has_ci": has_ci,
        "ci_providers": ci_providers,
        "manifests_found": manifests_found,
    }
