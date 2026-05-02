"""hardware_capability_profile.py — Hardware detection and profiling.

Detects VRAM, RAM, CPU cores to compute safe context limits.
Primary target: rtx5070ti-16g (16GB VRAM, 64GB RAM, 6-core 5600X).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class HardwareProfile:
    name: str
    vram_gb: float
    ram_gb: float
    cpu_cores: int
    cpu_threads: int = 0
    gpu_bandwidth_gbps: Optional[float] = None  # For MoE models

    supports_gpu_offload: bool = True
    os: str = ""  # linux, darwin, windows

    # Computed fields
    max_kv_cache_gb: Optional[float] = None
    safe_context_tokens: Optional[int] = None

    def __post_init__(self):
        if not self.os:
            self.os = platform.system().lower()
        if self.cpu_threads == 0:
            self.cpu_threads = self.cpu_cores * 2


# Predefined hardware profiles
HARDWARE_PROFILES: dict[str, HardwareProfile] = {
    "auto": HardwareProfile(
        name="auto-detected",
        vram_gb=0,
        ram_gb=0,
        cpu_cores=4,
    ),
    "rtx5070ti-16g": HardwareProfile(
        name="rtx5070ti-16g",
        vram_gb=16.0,
        ram_gb=64.0,
        cpu_cores=6,
        cpu_threads=12,
        gpu_bandwidth_gbps=896.0,
        supports_gpu_offload=True,
        os="linux",
    ),
    "rtx4080-16g": HardwareProfile(
        name="rtx4080-16g",
        vram_gb=16.0,
        ram_gb=32.0,
        cpu_cores=8,
        cpu_threads=16,
        gpu_bandwidth_gbps=716.8,
        supports_gpu_offload=True,
        os="linux",
    ),
    "rtx3090-24g": HardwareProfile(
        name="rtx3090-24g",
        vram_gb=24.0,
        ram_gb=64.0,
        cpu_cores=12,
        cpu_threads=24,
        gpu_bandwidth_gbps=936.2,
        supports_gpu_offload=True,
        os="linux",
    ),
    "m4-mac-48g": HardwareProfile(
        name="m4-mac-48g",
        vram_gb=48.0,  # Unified memory acts as VRAM
        ram_gb=48.0,
        cpu_cores=14,
        cpu_threads=14,
        supports_gpu_offload=True,
        os="darwin",
    ),
    "m4-mac-24g": HardwareProfile(
        name="m4-mac-24g",
        vram_gb=24.0,
        ram_gb=24.0,
        cpu_cores=12,
        cpu_threads=12,
        supports_gpu_offload=True,
        os="darwin",
    ),
    "m3-mac-36g": HardwareProfile(
        name="m3-mac-36g",
        vram_gb=36.0,
        ram_gb=36.0,
        cpu_cores=12,
        cpu_threads=12,
        supports_gpu_offload=True,
        os="darwin",
    ),
    "m2-mac-24g": HardwareProfile(
        name="m2-mac-24g",
        vram_gb=24.0,
        ram_gb=24.0,
        cpu_cores=8,
        cpu_threads=8,
        supports_gpu_offload=True,
        os="darwin",
    ),
    "cloud": HardwareProfile(
        name="cloud",
        vram_gb=0,  # No local VRAM
        ram_gb=0,
        cpu_cores=0,
        supports_gpu_offload=False,
        os="cloud",
    ),
}


def _detect_vram_nvidia() -> float:
    """Detect VRAM on Linux/Windows NVIDIA GPUs via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            vram_mb = int(result.stdout.strip().split("\n")[0])
            return vram_mb / 1024
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        logger.debug(f"nvidia-smi detection failed: {e}")
    return 0


def _detect_vram_windows() -> float:
    """Detect VRAM on Windows via WMI or PowerShell fallback (G6).

    Tries three strategies in order:
    1. ``wmi`` Python package (optional, fastest).
    2. PowerShell ``Get-CimInstance Win32_VideoController`` (no extra deps).
    3. ``nvidia-smi`` via ``_detect_vram_nvidia()`` (covers NVIDIA on Windows too).
    """
    # Strategy 1: wmi package
    try:
        import wmi  # type: ignore[import]

        c = wmi.WMI()
        vram_bytes = max(
            (int(v.AdapterRAM or 0) for v in c.Win32_VideoController()), default=0
        )
        if vram_bytes > 0:
            return vram_bytes / (1024**3)
    except Exception as e:
        logger.debug(f"wmi VRAM detection failed: {e}")

    # Strategy 2: PowerShell CIM
    try:
        ps_cmd = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object -ExpandProperty AdapterRAM"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # May return multiple lines if multiple GPUs; take the max
            values = [
                int(line.strip())
                for line in result.stdout.strip().split("\n")
                if line.strip().isdigit()
            ]
            if values:
                return max(values) / (1024**3)
    except Exception as e:
        logger.debug(f"PowerShell VRAM detection failed: {e}")

    # Strategy 3: nvidia-smi (works on Windows too)
    return _detect_vram_nvidia()


def _detect_vram_metal() -> float:
    """Estimate VRAM on macOS (unified memory)."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ram_bytes = int(result.stdout.strip())
            return ram_bytes / (1024**3)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        logger.debug(f"sysctl detection failed: {e}")
    return 0


def _detect_ram() -> float:
    """Detect total RAM in GB."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip()) / (1024**3)
        elif platform.system() == "Linux":
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(re.search(r"\d+", line).group())  # type: ignore
                        return kb / (1024**2)
        else:
            # Windows — use GlobalMemoryStatusEx (supports >4 GB via ULONGLONG fields).
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return stat.ullTotalPhys / (1024**3)
    except Exception as e:
        logger.debug(f"RAM detection failed: {e}")
    return 0


def _detect_cpu_cores() -> tuple[int, int]:
    """Detect CPU cores and threads."""
    try:
        cores = os.cpu_count() or 4
        return cores, cores * 2
    except Exception:
        return 4, 8


def _detect_gpu_name() -> str:
    """Get GPU name for profile matching."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "Chip" in line or "Model" in line:
                        return line.strip()
        elif platform.system() == "Linux":
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        elif platform.system() == "Windows":
            # Try nvidia-smi first (fastest for NVIDIA)
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().split("\n")[0]
            except Exception:
                pass
            # Fallback: PowerShell CIM
            try:
                ps_cmd = (
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object -First 1 -ExpandProperty Name"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
    except Exception:
        pass
    return ""


def detect_hardware() -> HardwareProfile:
    """Auto-detect hardware and return best-matching profile."""
    os_name = platform.system().lower()

    # Detect VRAM
    vram_gb = 0.0
    if os_name == "darwin":
        vram_gb = _detect_vram_metal()
    elif os_name == "linux":
        vram_gb = _detect_vram_nvidia()
        if vram_gb == 0:
            vram_gb = _detect_vram_metal()  # Fallback to unified memory detection
    elif os_name == "windows":
        # G6: Windows GPU detection via WMI / PowerShell / nvidia-smi
        vram_gb = _detect_vram_windows()

    # Detect RAM
    ram_gb = _detect_ram()

    # Detect CPU
    cores, threads = _detect_cpu_cores()

    # Match to predefined profile
    if vram_gb == 0 and ram_gb == 0:
        return HARDWARE_PROFILES["cloud"]

    # Find best match by VRAM
    best_match = "rtx5070ti-16g"  # Default
    for name, profile in HARDWARE_PROFILES.items():
        if name == "auto" or name == "cloud":
            continue
        if abs(profile.vram_gb - vram_gb) < 2:
            best_match = name
            break

    # Clone matched profile and override with detected values
    base = HARDWARE_PROFILES.get(best_match, HARDWARE_PROFILES["rtx5070ti-16g"])
    detected = HardwareProfile(
        name=f"detected-{base.name}",
        vram_gb=vram_gb or base.vram_gb,
        ram_gb=ram_gb or base.ram_gb,
        cpu_cores=cores or base.cpu_cores,
        cpu_threads=threads or base.cpu_threads,
        gpu_bandwidth_gbps=base.gpu_bandwidth_gbps,
        supports_gpu_offload=vram_gb > 0,
        os=os_name,
    )

    logger.info(
        f"Detected hardware: {detected.vram_gb:.1f}GB VRAM, "
        f"{detected.ram_gb:.1f}GB RAM, {detected.cpu_cores} cores"
    )

    return detected


def get_hardware_profile(name: str) -> HardwareProfile:
    """Get hardware profile by name, or auto-detect if 'auto'."""
    if name.lower() == "auto":
        return detect_hardware()
    return HARDWARE_PROFILES.get(name.lower(), detect_hardware())


def list_hardware_profiles() -> list[str]:
    """List available hardware profile names."""
    return [k for k in HARDWARE_PROFILES.keys() if k != "auto"]


def compute_safe_context_tokens(
    vram_gb: float,
    model_weights_gb: float,
    kv_per_token_mb: float,
    overhead_gb: float = 1.5,
) -> int:
    """Compute safe context tokens based on VRAM and model weights.

    Args:
        vram_gb: Available VRAM in GB
        model_weights_gb: Model weights size in GB
        kv_per_token_mb: KV cache per 1K tokens in MB
        overhead_gb: Reserved overhead for framework + activations

    Returns:
        Safe context token count (minimum 8K)
    """
    available = vram_gb - model_weights_gb - overhead_gb
    if available <= 0:
        return 8192  # Minimum safe context even if tight
    tokens_per_gb = 1024 / kv_per_token_mb
    safe_tokens = int(available * tokens_per_gb)
    # Minimum floor: 8K tokens for any useful context
    return max(safe_tokens, 8192)
