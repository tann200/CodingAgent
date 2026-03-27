"""
Unit tests for preview_service.py - Phase 3: Preview Mode
"""

import pytest
from src.core.orchestration.preview_service import (
    PreviewService,
    DiffPreview,
    get_preview_service,
)


class TestDiffPreview:
    def test_diff_preview_creation(self):
        preview = DiffPreview(
            preview_id="12345678",
            tool_name="edit_file",
            args={"path": "test.py"},
            old_content="old content",
            new_content="new content",
            diff="",
            file_path="test.py",
            status="pending",
        )
        assert preview.preview_id == "12345678"
        assert preview.tool_name == "edit_file"
        assert preview.status == "pending"


class TestPreviewService:
    def test_singleton(self):
        service1 = get_preview_service("/tmp")
        service2 = get_preview_service("/tmp")
        assert service1 is service2

    def test_generate_preview(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
            old_content="old content",
            new_content="new content",
        )

        assert preview.tool_name == "edit_file"
        assert preview.preview_id is not None
        assert len(preview.diff) > 0
        assert preview.status == "pending"

    def test_get_preview(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        retrieved = service.get_preview(preview.preview_id)
        assert retrieved is not None
        assert retrieved.preview_id == preview.preview_id

    def test_confirm(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        result = service.confirm(preview.preview_id)
        assert result is True

        updated = service.get_preview(preview.preview_id)
        assert updated.status == "confirmed"

    def test_reject(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        service.reject(preview.preview_id)

        updated = service.get_preview(preview.preview_id)
        assert updated.status == "rejected"

    def test_clear_preview(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        service.clear_preview(preview.preview_id)

        retrieved = service.get_preview(preview.preview_id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_wait_for_confirmation_confirmed(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        # Simulate user confirming
        service.confirm(preview.preview_id)

        result = await service.wait_for_confirmation(preview.preview_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_confirmation_rejected(self):
        service = PreviewService("/tmp")
        preview = service.generate_preview(
            tool_name="edit_file",
            args={"path": "test.py"},
        )

        # Simulate user rejecting
        service.reject(preview.preview_id)

        result = await service.wait_for_confirmation(preview.preview_id)
        assert result is False


class TestPreviewServiceComputeDiff:
    def test_diff_computation(self):
        service = PreviewService("/tmp")

        diff = service._compute_diff(
            "line 1\nline 2\nline 3\n",
            "line 1\nline 2 modified\nline 3\n",
        )

        assert "---" in diff
        assert "+++" in diff
        assert "modified" in diff
