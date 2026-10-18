"""
Workspace Safety Guard - Protects critical files from automatic modification.
"""

import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

from src.core.security.redaction import is_secret_path


PROTECTED_PATTERNS = [
    ".git/",
    ".gitignore",
    ".gitattributes",
    ".env",
    ".env.local",
    ".env.example",
    "requirements.txt",
    "package-lock.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Cargo.lock",
    "go.sum",
    ".npmrc",
    ".pypirc",
    "poetry.lock",
    "Pipfile.lock",
    "yarn.lock",
    "bun.lockb",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    "Makefile",
    "CMakeLists.txt",
    "tsconfig.json",
    "jsconfig.json",
    ".eslintrc",
    ".prettierrc",
    "pytest.ini",
    "tox.ini",
    ".github/",
    ".gitlab-ci.yml",
    ".travis.yml",
]


class WorkspaceGuard:
    """Protects critical and secret-bearing files from modification and unauthorized reading."""

    def __init__(
        self,
        protected_patterns: Optional[List[str]] = None,
        workspace_root: Optional[Union[str, Path]] = None,
    ):
        self.protected_patterns = protected_patterns or PROTECTED_PATTERNS
        self.workspace_root = (
            Path(workspace_root).resolve()
            if workspace_root is not None
            else Path.cwd().resolve()
        )

    def is_secret(self, path: Union[str, Path]) -> bool:
        """Check if path points to a secret-bearing file or credential."""
        if not path:
            return False
        # 1. Lexical check on normalized input path
        norm = os.path.normpath(str(path))
        if is_secret_path(norm) or is_secret_path(Path(norm).name):
            return True
        # 2. Resolved check relative to workspace_root
        try:
            p = Path(norm)
            resolved = (
                (self.workspace_root / p).resolve()
                if not p.is_absolute()
                else p.resolve()
            )
            if is_secret_path(resolved) or is_secret_path(resolved.name):
                return True
        except Exception:
            pass
        return False

    def is_protected(self, path: Union[str, Path]) -> bool:
        """Check if path matches protected pattern or secret path."""
        if not path:
            return False
        if self.is_secret(path):
            return True

        norm_path = os.path.normpath(str(path))
        candidates = [norm_path]

        try:
            p = Path(norm_path)
            resolved = (
                (self.workspace_root / p).resolve()
                if not p.is_absolute()
                else p.resolve()
            )
            candidates.append(str(resolved))
            try:
                rel = str(resolved.relative_to(self.workspace_root))
                candidates.append(rel)
            except ValueError:
                pass
        except Exception:
            pass

        for cand in candidates:
            cand_obj = Path(cand)
            cand_str = str(cand_obj)
            cand_name = cand_obj.name
            cand_parts = set(cand_obj.parts)
            for pattern in self.protected_patterns:
                if pattern.endswith("/"):
                    clean_pat = pattern.rstrip("/")
                    if (
                        cand_str == clean_pat
                        or cand_str.startswith(pattern)
                        or f"/{pattern}" in cand_str
                        or clean_pat in cand_parts
                    ):
                        return True
                else:
                    if cand_str.endswith(pattern) or cand_name == pattern:
                        return True

        return False

    def require_approval(self, path: str) -> bool:
        """Return True if explicit user approval required for this path."""
        return self.is_protected(path)

    def guard_operation(
        self, operation: str, path: str, user_approved: bool = False
    ) -> Dict[str, Any]:
        """Validate operation against protected files.

        Args:
            operation: Type of operation (write_file, edit_file, delete_file, read_file, etc)
            path: File path being operated on
            user_approved: Whether user has explicitly approved this operation

        Returns:
            Dict with status and error message if blocked
        """
        is_sec = self.is_secret(path)
        is_prot = self.is_protected(path)

        if not is_prot and not is_sec:
            return {"status": "ok", "protected": False, "secret": False}

        if user_approved:
            return {
                "status": "ok",
                "protected": True,
                "secret": is_sec,
                "user_approved": True,
                "message": f"Operation on protected {'secret ' if is_sec else ''}file '{path}' approved by user",
            }

        # Reading secret-bearing files requires explicit approval and is restricted
        if is_sec and operation in (
            "read_file",
            "read_file_chunk",
            "tail_log_file",
            "fs.read",
            "grep",
        ):
            return {
                "status": "error",
                "error": f"Protected secret file: '{path}'. Reading credentials, environment files, or private keys is restricted.",
                "requires_approval": True,
                "operation": operation,
                "protected": True,
                "secret": True,
            }

        return {
            "status": "error",
            "error": f"Protected file: '{path}'. Explicit user approval required for {operation}.",
            "requires_approval": True,
            "operation": operation,
            "protected": True,
            "secret": is_sec,
        }

    def get_protected_info(self, path: str) -> Dict[str, Any]:
        """Get detailed info about protection status."""
        is_protected = self.is_protected(path)

        return {
            "path": path,
            "protected": is_protected,
            "requires_approval": is_protected,
            "reason": self._get_protection_reason(path) if is_protected else None,
        }

    def _get_protection_reason(self, path: str) -> str:
        """Explain why a path is protected."""
        path_obj = Path(path)
        name = path_obj.name

        if self.is_secret(path):
            return "Secret file containing credentials, keys, or sensitive configuration"
        if ".git" in str(path_obj):
            return "Git repository metadata (do not modify)"
        if name == ".env" or ".env." in name:
            return "Environment configuration (may contain secrets)"
        if name in ["requirements.txt", "package-lock.json", "pyproject.toml"]:
            return "Dependency lock file (use package manager)"
        if name in ["Dockerfile", "docker-compose.yml"]:
            return "Container configuration"

        return "Protected file pattern"


def create_workspace_guard() -> WorkspaceGuard:
    """Factory function to create WorkspaceGuard."""
    return WorkspaceGuard()


def guard_write_operation(
    tool_name: str, path: str, user_approved: bool = False
) -> Dict[str, Any]:
    """Convenience function to guard write operations."""
    guard = WorkspaceGuard()
    return guard.guard_operation(tool_name, path, user_approved)


def guard_read_operation(
    tool_name: str, path: str, user_approved: bool = False
) -> Dict[str, Any]:
    """Convenience function to guard read operations."""
    guard = WorkspaceGuard()
    return guard.guard_operation(tool_name, path, user_approved)
