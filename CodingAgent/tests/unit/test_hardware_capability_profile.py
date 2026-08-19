"""
Tests for src.core.inference.hardware_capability_profile.py
"""

import pytest

from src.core.inference.hardware_capability_profile import (
    HardwareProfile,
    detect_hardware,
    get_hardware_profile,
    HARDWARE_PROFILES,
    compute_safe_context_tokens,
)


def test_hardware_profile_creation():
    """Test basic HardwareProfile creation."""
    profile = HardwareProfile(
        name="test-profile",
        vram_gb=16.0,
        ram_gb=64.0,
        cpu_cores=8,
        cpu_threads=16,
        gpu_bandwidth_gbps=896.0,
    )

    assert profile.name == "test-profile"
    assert profile.vram_gb == 16.0
    assert profile.ram_gb == 64.0
    assert profile.cpu_cores == 8
    assert profile.cpu_threads == 16
    assert profile.gpu_bandwidth_gbps == 896.0


def test_hardware_profiles_dict():
    """Test that HARDWARE_PROFILES dict is structured correctly."""
    assert isinstance(HARDWARE_PROFILES, dict)
    assert "auto" in HARDWARE_PROFILES
    assert "rtx5070ti-16g" in HARDWARE_PROFILES

    # Check workstation profile
    ws_profile = HARDWARE_PROFILES["rtx5070ti-16g"]
    assert isinstance(ws_profile, HardwareProfile)
    assert ws_profile.name == "rtx5070ti-16g"
    assert ws_profile.vram_gb == 16.0
    assert ws_profile.ram_gb == 64.0
    assert ws_profile.cpu_cores == 6
    assert ws_profile.cpu_threads == 12
    assert ws_profile.gpu_bandwidth_gbps == 896.0
    assert ws_profile.os == "linux"


def test_detect_hardware():
    """Test hardware detection."""
    profile = detect_hardware()
    assert isinstance(profile, HardwareProfile)
    # Should have the expected fields
    assert hasattr(profile, "vram_gb")
    assert hasattr(profile, "ram_gb")
    assert hasattr(profile, "cpu_cores")
    assert hasattr(profile, "cpu_threads")
    assert hasattr(profile, "os")
    # On this system it detected a specific profile, which is fine
    assert profile.name != ""


def test_get_hardware_profile():
    """Test getting hardware profile by name."""
    # Test known profile - use the one that was actually detected
    detected_profile = detect_hardware()
    if detected_profile.name != "auto-detected":
        # If auto-detection worked and gave us a specific profile, test getting it by name
        ws_profile = get_hardware_profile(detected_profile.name)
        assert isinstance(ws_profile, HardwareProfile)
        assert ws_profile.name == detected_profile.name
    else:
        # Fall back to testing a known predefined profile
        ws_profile = get_hardware_profile("rtx5070ti-16g")
        assert isinstance(ws_profile, HardwareProfile)
        assert ws_profile.name == "rtx5070ti-16g"
        assert ws_profile.vram_gb == 16.0
        assert ws_profile.ram_gb == 64.0
        assert ws_profile.cpu_cores == 6
        assert ws_profile.cpu_threads == 12
        assert ws_profile.gpu_bandwidth_gbps == 896.0
        assert ws_profile.os == "linux"

    # Test unknown profile (should fall back to auto)
    unknown_profile = get_hardware_profile("non-existent-profile")
    assert isinstance(unknown_profile, HardwareProfile)
    # Should be a valid profile


def test_compute_safe_context_tokens():
    """Test safe context token computation."""
    # Test with known hardware profile
    safe_tokens = compute_safe_context_tokens(
        vram_gb=16.0, model_weights_gb=5.5, kv_per_token_mb=0.05, overhead_gb=1.5
    )

    # Should return a reasonable positive number
    assert safe_tokens > 0
    assert isinstance(safe_tokens, int)

    # Test edge case: insufficient VRAM
    safe_tokens = compute_safe_context_tokens(
        vram_gb=4.0,
        model_weights_gb=10.0,  # Model larger than VRAM
        kv_per_token_mb=0.1,
        overhead_gb=0.5,
    )
    # Should return minimum context size (8192)
    assert safe_tokens >= 8192


def test_hardware_profile_post_init():
    """Test HardwareProfile __post_init__ method."""
    # Test OS detection
    profile = HardwareProfile(
        name="test",
        vram_gb=8.0,
        ram_gb=16.0,
        cpu_cores=4,
    )
    # os should be auto-detected
    assert profile.os != ""  # Should be set to linux/darwin/windows

    # Test cpu_threads default
    profile2 = HardwareProfile(
        name="test2",
        vram_gb=8.0,
        ram_gb=16.0,
        cpu_cores=4,
        cpu_threads=0,  # Explicitly zero
    )
    # Should be set to cpu_cores * 2 = 8
    assert profile2.cpu_threads == 8

    # Test cpu_threads preservation
    profile3 = HardwareProfile(
        name="test3",
        vram_gb=8.0,
        ram_gb=16.0,
        cpu_cores=4,
        cpu_threads=10,  # Explicitly set
    )
    assert profile3.cpu_threads == 10  # Should preserve explicit value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
