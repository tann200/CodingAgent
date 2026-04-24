"""RS-1 tests: remote skill discovery and caching.

Tests cover:
- fetch_remote_skills: cache miss (fresh fetch), cache hit, stale-cache fallback
  on network error, bad JSON handling, TTL staleness logic.
- load_all_remote_skills: config-driven URL list, per-URL merging, local-wins
  collision when wired into AgentBrainManager.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.remote_skills import (
    _cache_dir_for_url,
    _is_stale,
    fetch_remote_skills,
    load_all_remote_skills,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_INDEX = [
    {"name": "remote_skill_a", "file": "remote_skill_a.md", "description": "Skill A"},
    {"name": "remote_skill_b", "file": "remote_skill_b.md"},
]
_SKILL_A_CONTENT = "---\nname: remote_skill_a\n---\n# Remote Skill A\nDo things."
_SKILL_B_CONTENT = "# Remote Skill B\nDo other things."


def _make_response(text: str, status: int = 200) -> MagicMock:
    """Build a minimal requests.Response mock."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.raise_for_status = MagicMock(
        side_effect=(None if status < 400 else Exception(f"HTTP {status}"))
    )
    return resp


# ---------------------------------------------------------------------------
# _is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    """RS-1 unit tests for the staleness helper."""

    def test_nonexistent_path_is_stale(self, tmp_path):
        """A path that does not exist is always stale."""
        assert _is_stale(tmp_path / "nope.txt", 3600) is True

    def test_fresh_file_is_not_stale(self, tmp_path):
        """A file written just now is not stale with a 1-hour TTL."""
        f = tmp_path / "fresh.txt"
        f.write_text("hi")
        assert _is_stale(f, 3600) is False

    def test_old_file_is_stale(self, tmp_path):
        """A file with mtime in the past exceeds a 0-second TTL."""
        f = tmp_path / "old.txt"
        f.write_text("hi")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        import os

        os.utime(f, (old_time, old_time))
        assert _is_stale(f, 3600) is True


# ---------------------------------------------------------------------------
# _cache_dir_for_url
# ---------------------------------------------------------------------------


class TestCacheDirForUrl:
    """Stable per-URL directory mapping."""

    def test_same_url_gives_same_dir(self):
        d1 = _cache_dir_for_url("https://example.com/skills/")
        d2 = _cache_dir_for_url("https://example.com/skills/")
        assert d1 == d2

    def test_different_urls_give_different_dirs(self):
        d1 = _cache_dir_for_url("https://example.com/skills/")
        d2 = _cache_dir_for_url("https://other.com/skills/")
        assert d1 != d2


# ---------------------------------------------------------------------------
# fetch_remote_skills
# ---------------------------------------------------------------------------


class TestFetchRemoteSkills:
    """RS-1 tests for fetch_remote_skills."""

    def test_empty_url_returns_empty(self):
        """Empty base_url short-circuits without hitting network."""
        result = fetch_remote_skills("", ttl_seconds=3600)
        assert result == {}

    def test_fresh_fetch_returns_skills(self, tmp_path):
        """On cache miss, both index and skill files are fetched."""
        base_url = "https://example.com/skills/"

        def fake_get(url, timeout=10):
            if "index.json" in url:
                return _make_response(json.dumps(_SAMPLE_INDEX))
            if "remote_skill_a.md" in url:
                return _make_response(_SKILL_A_CONTENT)
            if "remote_skill_b.md" in url:
                return _make_response(_SKILL_B_CONTENT)
            raise AssertionError(f"Unexpected URL: {url}")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fake_get),
        ):
            result = fetch_remote_skills(base_url, ttl_seconds=3600)

        assert "remote_skill_a" in result
        assert "remote_skill_b" in result
        # Body extraction strips front-matter
        assert "Remote Skill A" in result["remote_skill_a"]

    def test_cache_hit_skips_network(self, tmp_path):
        """When cache files are fresh, no HTTP requests are made."""
        base_url = "https://example.com/skills/"
        cache_dir = _cache_dir_for_url(base_url)
        full_cache = tmp_path / cache_dir.name
        full_cache.mkdir(parents=True)
        (full_cache / "_index.json").write_text(json.dumps(_SAMPLE_INDEX))
        (full_cache / "remote_skill_a.md").write_text(_SKILL_A_CONTENT)
        (full_cache / "remote_skill_b.md").write_text(_SKILL_B_CONTENT)

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get") as mock_get,
        ):
            result = fetch_remote_skills(base_url, ttl_seconds=3600)
            mock_get.assert_not_called()

        assert "remote_skill_a" in result
        assert "remote_skill_b" in result

    def test_stale_cache_fallback_on_network_error(self, tmp_path):
        """When network fails, stale cached index and skills are used."""
        base_url = "https://example.com/skills/"
        cache_dir = _cache_dir_for_url(base_url)
        full_cache = tmp_path / cache_dir.name
        full_cache.mkdir(parents=True)

        # Write stale index/skill (mtime = 2 hours ago)
        import os

        old_time = time.time() - 7200
        index_path = full_cache / "_index.json"
        skill_path = full_cache / "remote_skill_a.md"
        index_path.write_text(
            json.dumps([{"name": "remote_skill_a", "file": "remote_skill_a.md"}])
        )
        skill_path.write_text(_SKILL_A_CONTENT)
        os.utime(index_path, (old_time, old_time))
        os.utime(skill_path, (old_time, old_time))

        def fail_get(url, timeout=10):
            raise ConnectionError("network down")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fail_get),
        ):
            result = fetch_remote_skills(base_url, ttl_seconds=3600)

        assert "remote_skill_a" in result

    def test_bad_index_json_returns_empty(self, tmp_path):
        """Malformed index JSON causes graceful empty return."""
        base_url = "https://example.com/skills/"

        def fake_get(url, timeout=10):
            return _make_response("not json at all")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fake_get),
        ):
            result = fetch_remote_skills(base_url, ttl_seconds=3600)

        assert result == {}

    def test_missing_name_or_file_entries_skipped(self, tmp_path):
        """Index entries without 'name' or 'file' fields are skipped."""
        base_url = "https://example.com/skills/"
        bad_index = [
            {"name": "no_file_here"},  # missing 'file'
            {"file": "no_name.md"},  # missing 'name'
            {"name": "valid_skill", "file": "valid_skill.md"},
        ]

        def fake_get(url, timeout=10):
            if "index.json" in url:
                return _make_response(json.dumps(bad_index))
            return _make_response("# Valid skill content")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fake_get),
        ):
            result = fetch_remote_skills(base_url, ttl_seconds=3600)

        assert "valid_skill" in result
        assert "no_file_here" not in result


# ---------------------------------------------------------------------------
# load_all_remote_skills
# ---------------------------------------------------------------------------


class TestLoadAllRemoteSkills:
    """RS-1 tests for load_all_remote_skills."""

    def test_no_urls_returns_empty(self):
        """Passing an empty list returns an empty dict without network calls."""
        result = load_all_remote_skills(urls=[])
        assert result == {}

    def test_reads_from_config_when_no_urls_given(self):
        """When urls=None, reads skills.urls from merged config."""
        mock_cfg = {"skills": {"urls": [], "cache_ttl_seconds": 3600}}
        with patch(
            "src.core.config_loader.load_merged_config",
            return_value=mock_cfg,
        ):
            result = load_all_remote_skills(urls=None)
        assert result == {}

    def test_multiple_urls_merged(self, tmp_path):
        """Skills from multiple URLs are merged; later URL wins on collision."""
        url_a = "https://source-a.com/skills/"
        url_b = "https://source-b.com/skills/"

        # url_a: skill_x
        # url_b: skill_x (override) + skill_y
        index_a = [{"name": "skill_x", "file": "skill_x.md"}]
        index_b = [
            {"name": "skill_x", "file": "skill_x.md"},
            {"name": "skill_y", "file": "skill_y.md"},
        ]
        contents = {
            url_a + "index.json": json.dumps(index_a),
            url_a + "skill_x.md": "# Skill X from A",
            url_b + "index.json": json.dumps(index_b),
            url_b + "skill_x.md": "# Skill X from B",
            url_b + "skill_y.md": "# Skill Y",
        }

        def fake_get(url, timeout=10):
            # normalise trailing slash differences
            for key, val in contents.items():
                if url.rstrip("/").endswith(key.rstrip("/").split("//", 1)[-1]):
                    return _make_response(val)
            return _make_response("# fallback")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fake_get),
        ):
            result = load_all_remote_skills(urls=[url_a, url_b], ttl_seconds=0)

        # skill_y must be present
        assert "skill_y" in result
        # skill_x from url_b overwrites url_a (later wins)
        assert "from B" in result.get("skill_x", "")

    def test_failed_url_does_not_abort_others(self, tmp_path):
        """A broken URL is skipped; skills from other URLs still load."""
        good_url = "https://good.com/skills/"
        bad_url = "https://bad.com/skills/"

        def fake_get(url, timeout=10):
            if "bad.com" in url:
                raise ConnectionError("down")
            if "index.json" in url:
                return _make_response(
                    json.dumps([{"name": "good_skill", "file": "good_skill.md"}])
                )
            return _make_response("# Good skill")

        with (
            patch("src.core.orchestration.remote_skills._CACHE_BASE", tmp_path),
            patch("requests.get", side_effect=fake_get),
        ):
            result = load_all_remote_skills(urls=[bad_url, good_url], ttl_seconds=0)

        assert "good_skill" in result


# ---------------------------------------------------------------------------
# AgentBrainManager integration: remote skills wired in, local wins
# ---------------------------------------------------------------------------


class TestAgentBrainManagerRemoteIntegration:
    """RS-1: Remote skills are merged into AgentBrainManager, local wins."""

    def test_remote_skill_added_to_manager(self, tmp_path):
        """A remote skill that doesn't exist locally is added to the manager."""
        remote_content = {
            "totally_new_remote_skill": "# Remote Only Skill\nDoes stuff."
        }

        with patch(
            "src.core.orchestration.remote_skills.load_all_remote_skills",
            return_value=remote_content,
        ):
            from src.core.orchestration.agent_brain import AgentBrainManager

            mgr = AgentBrainManager()
            # Force reload so the mock is applied
            mgr.reload()
            all_skills = mgr.get_all_skills()

        assert "totally_new_remote_skill" in all_skills

    def test_local_skill_wins_over_remote(self):
        """A local skill is NOT overwritten by a remote skill with the same name."""
        # Pick a skill we know exists locally
        from src.core.orchestration.agent_brain import AgentBrainManager

        mgr = AgentBrainManager()
        local_skills = mgr.get_all_skills()
        if not local_skills:
            pytest.skip("No local skills to test collision with")

        some_local_skill = next(iter(local_skills))
        original_content = local_skills[some_local_skill]

        remote_content = {some_local_skill: "# OVERWRITTEN\nThis should NOT appear."}

        with patch(
            "src.core.orchestration.remote_skills.load_all_remote_skills",
            return_value=remote_content,
        ):
            mgr.reload()
            content_after = mgr.get_skill(some_local_skill)

        # Content must be unchanged (local wins)
        assert "OVERWRITTEN" not in content_after
        assert content_after == original_content
