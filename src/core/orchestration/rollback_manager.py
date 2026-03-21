"""
Automated Rollback Manager for CodingAgent.

This module provides automated rollback functionality when verification fails,
allowing the agent to recover to a previous state.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
from dataclasses import dataclass

from src.tools._path_utils import safe_resolve

logger = logging.getLogger(__name__)


@dataclass
class FileSnapshot:
    """Represents a snapshot of a file at a point in time."""

    path: str
    content: str
    timestamp: str
    checksum: str


class RollbackManager:
    """
    Manages automated rollback on verification failure.

    Usage:
        rollback_mgr = RollbackManager(workdir)

        # Before making changes
        rollback_mgr.snapshot_files(["src/main.py", "src/utils.py"])

        # After verification fails
        if not verification_passed:
            rollback_mgr.rollback()
    """

    def __init__(self, workdir: str):
        self.workdir = Path(workdir)
        self.snapshot_dir = self.workdir / ".agent-context" / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.current_snapshot: Optional[str] = None
        self.snapshots: Dict[str, List[FileSnapshot]] = {}

    def _compute_checksum(self, content: str) -> str:
        """Compute a simple checksum for file content."""
        import hashlib

        return hashlib.md5(content.encode()).hexdigest()

    def snapshot_files(
        self, file_paths: List[str], snapshot_id: Optional[str] = None
    ) -> str:
        """
        Take a snapshot of the specified files.

        Args:
            file_paths: List of file paths to snapshot
            snapshot_id: Optional ID for this snapshot (generated if not provided)

        Returns:
            Snapshot ID
        """
        snapshot_id = snapshot_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.current_snapshot = snapshot_id

        snapshots: List[FileSnapshot] = []

        for file_path in file_paths:
            try:
                path = safe_resolve(file_path, self.workdir)
            except (PermissionError, ValueError):
                logger.warning(f"snapshot_files: path '{file_path}' escapes workspace — skipping")
                continue
            if not path.exists():
                continue

            try:
                content = path.read_text(encoding="utf-8")
                snapshots.append(
                    FileSnapshot(
                        path=file_path,
                        content=content,
                        timestamp=datetime.now().isoformat(),
                        checksum=self._compute_checksum(content),
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to snapshot {file_path}: {e}")

        self.snapshots[snapshot_id] = snapshots

        # Save to disk
        snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"
        snapshot_data = {
            "snapshot_id": snapshot_id,
            "timestamp": datetime.now().isoformat(),
            "files": [
                {
                    "path": s.path,
                    "content": s.content,
                    "timestamp": s.timestamp,
                    "checksum": s.checksum,
                }
                for s in snapshots
            ],
        }
        snapshot_file.write_text(json.dumps(snapshot_data, indent=2))

        logger.info(f"Created snapshot {snapshot_id} with {len(snapshots)} files")
        return snapshot_id

    def rollback(self, snapshot_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Rollback to a previous snapshot.

        Args:
            snapshot_id: ID of snapshot to restore (uses current if not provided)

        Returns:
            Status of rollback operation
        """
        snapshot_id = snapshot_id or self.current_snapshot

        if not snapshot_id or snapshot_id not in self.snapshots:
            # Try to load from disk
            snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"
            if snapshot_file.exists():
                try:
                    data = json.loads(snapshot_file.read_text())
                    snapshots = [
                        FileSnapshot(
                            path=f["path"],
                            content=f["content"],
                            timestamp=f["timestamp"],
                            checksum=f["checksum"],
                        )
                        for f in data.get("files", [])
                    ]
                    self.snapshots[snapshot_id] = snapshots
                except Exception as e:
                    return {"ok": False, "error": f"Failed to load snapshot: {e}"}
            else:
                return {"ok": False, "error": f"Snapshot {snapshot_id} not found"}

        snapshots = self.snapshots.get(snapshot_id, [])

        restored_files = []
        for snap in snapshots:
            try:
                file_path = safe_resolve(snap.path, self.workdir)
            except (PermissionError, ValueError):
                logger.warning(f"rollback: path '{snap.path}' escapes workspace — skipping")
                continue
            try:
                # Create backup before restoring
                if file_path.exists():
                    backup_path = file_path.with_suffix(file_path.suffix + ".backup")
                    shutil.copy2(file_path, backup_path)

                # Restore content
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(snap.content, encoding="utf-8")
                restored_files.append(snap.path)

            except Exception as e:
                logger.error(f"Failed to restore {snap.path}: {e}")
                return {"ok": False, "error": f"Failed to restore {snap.path}: {e}"}

        logger.info(
            f"Rolled back {len(restored_files)} files from snapshot {snapshot_id}"
        )

        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "restored_files": restored_files,
            "restored_count": len(restored_files),
        }

    def append_to_snapshot(self, snapshot_id: str, file_path: str) -> bool:
        """
        Add a file to an existing snapshot (for multi-file atomic step transactions).

        If the file is already in the snapshot, it is skipped (already captured).

        Args:
            snapshot_id: The snapshot to extend.
            file_path: Relative path to the file to add.

        Returns:
            True if the file was added, False if skipped or snapshot not found.
        """
        try:
            path = safe_resolve(file_path, self.workdir)
        except (PermissionError, ValueError):
            logger.warning(f"append_to_snapshot: path '{file_path}' escapes workspace — skipping")
            return False
        if not path.exists():
            return False

        # Load snapshot from memory or disk; create empty if first append
        if snapshot_id not in self.snapshots:
            snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"
            if snapshot_file.exists():
                try:
                    data = json.loads(snapshot_file.read_text())
                    self.snapshots[snapshot_id] = [
                        FileSnapshot(
                            path=f["path"],
                            content=f["content"],
                            timestamp=f["timestamp"],
                            checksum=f["checksum"],
                        )
                        for f in data.get("files", [])
                    ]
                except Exception as e:
                    logger.warning(f"append_to_snapshot: failed to load {snapshot_id}: {e}")
                    return False
            else:
                # First file being added to this transaction — create empty entry
                self.snapshots[snapshot_id] = []

        existing_paths = {s.path for s in self.snapshots[snapshot_id]}
        if file_path in existing_paths:
            return False  # already captured

        try:
            content = path.read_text(encoding="utf-8")
            self.snapshots[snapshot_id].append(
                FileSnapshot(
                    path=file_path,
                    content=content,
                    timestamp=datetime.now().isoformat(),
                    checksum=self._compute_checksum(content),
                )
            )
        except Exception as e:
            logger.warning(f"append_to_snapshot: failed to read {file_path}: {e}")
            return False

        # Persist updated snapshot to disk
        try:
            snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"
            snapshot_data = {
                "snapshot_id": snapshot_id,
                "timestamp": datetime.now().isoformat(),
                "files": [
                    {
                        "path": s.path,
                        "content": s.content,
                        "timestamp": s.timestamp,
                        "checksum": s.checksum,
                    }
                    for s in self.snapshots[snapshot_id]
                ],
            }
            snapshot_file.write_text(json.dumps(snapshot_data, indent=2))
        except Exception as e:
            logger.warning(f"append_to_snapshot: failed to persist: {e}")

        logger.debug(f"append_to_snapshot: added {file_path} to {snapshot_id}")
        return True

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """List all available snapshots."""
        snapshots = []

        for snapshot_file in sorted(self.snapshot_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(snapshot_file.read_text())
                snapshots.append(
                    {
                        "snapshot_id": data["snapshot_id"],
                        "timestamp": data["timestamp"],
                        "file_count": len(data.get("files", [])),
                    }
                )
            except Exception:
                continue

        return snapshots

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot."""
        snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"

        if snapshot_file.exists():
            snapshot_file.unlink()
            if snapshot_id in self.snapshots:
                del self.snapshots[snapshot_id]
            return True

        return False

    def cleanup_old_snapshots(self, keep_last: int = 5) -> int:
        """Clean up old snapshots, keeping only the most recent ones."""
        snapshots = sorted(
            self.snapshot_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        deleted = 0
        for snapshot_file in snapshots[keep_last:]:
            try:
                snapshot_file.unlink()
                deleted += 1
            except Exception:
                pass

        return deleted


def create_rollback_manager(workdir: str) -> RollbackManager:
    """Factory function to create a RollbackManager."""
    return RollbackManager(workdir)
