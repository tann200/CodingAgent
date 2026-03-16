from __future__ import annotations
import ast
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ExecutionSandbox:
    """Sandbox for safely applying patches and running validation."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.temp_workspace: Optional[Path] = None

    def _create_temp_workspace(self) -> Path:
        """Create a temporary copy of the workspace for safe testing."""
        if self.temp_workspace:
            return self.temp_workspace

        self.temp_workspace = Path(tempfile.mkdtemp(prefix="sandbox_"))

        for item in self.workdir.iterdir():
            if item.name.startswith(".") or item.name in [
                "__pycache__",
                "node_modules",
                ".venv",
            ]:
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, self.temp_workspace / item.name)
                else:
                    shutil.copy2(item, self.temp_workspace / item.name)
            except Exception:
                pass

        return self.temp_workspace

    def apply_patch(self, patch: str, file_path: str) -> Dict[str, Any]:
        """Apply a patch to a file in the sandbox."""
        temp_dir = self._create_temp_workspace()
        target_file = temp_dir / file_path

        if not target_file.exists():
            return {"status": "error", "error": f"File not found: {file_path}"}

        try:
            import subprocess

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".patch", delete=False
            ) as f:
                f.write(patch)
                patch_file = f.name

            result = subprocess.run(
                ["patch", "-u", "-f", str(target_file), "-i", patch_file],
                capture_output=True,
                text=True,
            )

            Path(patch_file).unlink()

            if result.returncode != 0:
                return {"status": "error", "error": f"Patch failed: {result.stderr}"}

            return {"status": "ok", "sandbox_path": str(target_file)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def validate_ast(self, file_path: str) -> Dict[str, Any]:
        """Validate Python file AST."""
        temp_dir = self._create_temp_workspace()
        target_file = temp_dir / file_path

        if not target_file.exists():
            return {"status": "error", "error": "File not found"}

        try:
            source = target_file.read_text()
            ast.parse(source)
            return {"status": "ok", "valid": True}
        except SyntaxError as e:
            return {"status": "ok", "valid": False, "error": str(e), "line": e.lineno}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def run_ruff(self, file_path: str = None) -> Dict[str, Any]:
        """Run ruff linter in sandbox."""
        temp_dir = self._create_temp_workspace()

        try:
            cmd = ["ruff", "check", str(temp_dir)]
            if file_path:
                cmd[-1] = str(temp_dir / file_path)

            result = subprocess.run(cmd, capture_output=True, text=True)

            return {
                "status": "ok" if result.returncode in [0, 1] else "error",
                "returncode": result.returncode,
                "output": result.stdout + result.stderr,
                "issues": self._parse_ruff_output(result.stdout),
            }
        except FileNotFoundError:
            return {"status": "error", "error": "ruff not installed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def run_mypy(self, file_path: str = None) -> Dict[str, Any]:
        """Run mypy type checker in sandbox."""
        temp_dir = self._create_temp_workspace()

        try:
            cmd = ["mypy", str(temp_dir)]
            if file_path:
                cmd[-1] = str(temp_dir / file_path)

            result = subprocess.run(cmd, capture_output=True, text=True)

            return {
                "status": "ok" if result.returncode in [0, 1] else "error",
                "returncode": result.returncode,
                "output": result.stdout + result.stderr,
            }
        except FileNotFoundError:
            return {"status": "error", "error": "mypy not installed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def run_pytest(self, file_path: str = None) -> Dict[str, Any]:
        """Run pytest in sandbox."""
        temp_dir = self._create_temp_workspace()

        try:
            cmd = ["pytest", str(temp_dir), "-v", "--tb=short"]
            if file_path:
                cmd[-1] = str(temp_dir / file_path)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            return {
                "status": "ok",
                "returncode": result.returncode,
                "output": result.stdout + result.stderr,
                "passed": "passed" in result.stdout.lower(),
                "failed": "failed" in result.stdout.lower(),
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "Tests timed out"}
        except FileNotFoundError:
            return {"status": "error", "error": "pytest not installed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def full_validation(self, file_path: str = None) -> Dict[str, Any]:
        """Run all validation tools on the sandbox."""
        results = {
            "ast_validation": self.validate_ast(file_path)
            if file_path
            else {"status": "ok"},
            "ruff": self.run_ruff(file_path),
            "pytest": self.run_pytest(file_path),
        }

        has_errors = any(
            r.get("status") == "error" for r in results.values() if isinstance(r, dict)
        )

        return {"status": "error" if has_errors else "ok", "validations": results}

    def _parse_ruff_output(self, output: str) -> List[Dict]:
        """Parse ruff output into structured issues."""
        issues = []
        for line in output.splitlines():
            if ":" in line:
                parts = line.split(":")
                if len(parts) >= 3:
                    issues.append(
                        {
                            "file": parts[0],
                            "line": parts[1],
                            "message": ":".join(parts[2:]).strip(),
                        }
                    )
        return issues

    def cleanup(self):
        """Clean up temporary workspace."""
        if self.temp_workspace and self.temp_workspace.exists():
            try:
                shutil.rmtree(self.temp_workspace)
                self.temp_workspace = None
            except Exception:
                pass

    def __del__(self):
        self.cleanup()


class SelfDebugLoop:
    """Self-debugging loop with retry logic."""

    MAX_RETRIES = 3

    def __init__(self, sandbox: ExecutionSandbox = None):
        self.sandbox = sandbox or ExecutionSandbox()
        self.retry_count = 0

    def analyze_failure(
        self, test_output: str, patch: str, file_path: str
    ) -> Dict[str, Any]:
        """Analyze test failure and suggest fixes."""
        error_analysis = {
            "test_output": test_output,
            "patch": patch,
            "file_path": file_path,
            "likely_causes": [],
            "suggested_fix": None,
        }

        if "SyntaxError" in test_output:
            error_analysis["likely_causes"].append("syntax_error")
            error_analysis["suggested_fix"] = "Fix syntax errors before applying patch"

        elif "ImportError" in test_output or "ModuleNotFoundError" in test_output:
            error_analysis["likely_causes"].append("missing_import")
            error_analysis["suggested_fix"] = "Check import statements"

        elif "AssertionError" in test_output:
            error_analysis["likely_causes"].append("assertion_failure")
            error_analysis["suggested_fix"] = "Review test expectations"

        elif "AttributeError" in test_output:
            error_analysis["likely_causes"].append("attribute_error")
            error_analysis["suggested_fix"] = "Check attribute access"

        elif "TypeError" in test_output:
            error_analysis["likely_causes"].append("type_error")
            error_analysis["suggested_fix"] = "Check type compatibility"

        else:
            error_analysis["likely_causes"].append("unknown")
            error_analysis["suggested_fix"] = "Review error output for details"

        return error_analysis

    def attempt_fix(
        self, patch: str, file_path: str, test_output: str
    ) -> Dict[str, Any]:
        """Attempt to fix and re-test."""
        if self.retry_count >= self.MAX_RETRIES:
            return {
                "status": "error",
                "error": f"Max retries ({self.MAX_RETRIES}) exceeded",
                "retry_count": self.retry_count,
            }

        self.retry_count += 1

        analysis = self.analyze_failure(test_output, patch, file_path)

        apply_result = self.sandbox.apply_patch(patch, file_path)
        if apply_result.get("status") != "ok":
            return apply_result

        validation = self.sandbox.full_validation(file_path)

        return {
            "status": "ok" if validation.get("status") == "ok" else "error",
            "retry_count": self.retry_count,
            "analysis": analysis,
            "validation": validation,
        }

    def reset(self):
        """Reset retry counter."""
        self.retry_count = 0
