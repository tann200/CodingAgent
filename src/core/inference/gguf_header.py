"""Pure-stdlib bounded GGUF v2/v3 header & tensor-table reader (TASK-35).

Provides zero-dependency, DoS-bounded GGUF metadata inspection and exact
memory/KV-cache context fitting so CodingAgent never recommends or launches
an OOM local-model configuration on 16 GB Apple Silicon or GPU workstations.

Security invariants:
- Opens files with ``O_NOFOLLOW`` and verifies ``stat.S_ISREG`` (rejects
  symlinks, FIFOs, device nodes, sockets, and directories).
- Enforces strict byte and count ceilings on all parsed structures:
  ``HEADER_BYTE_BUDGET = 16 MiB``, ``MAX_KV = 4096``, ``MAX_STRING = 1 MiB``,
  ``MAX_ARRAY_ELEMENTS = 1_048_576``, ``MAX_TENSORS = 65_536``.
"""

from __future__ import annotations

import os
import re
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GGUF_MAGIC = b"GGUF"
SUPPORTED_VERSIONS = (2, 3)

# Defensive DoS bounds (TASK-35)
HEADER_BYTE_BUDGET = 16 * 1024 * 1024  # 16 MiB
MAX_KV = 4096
MAX_STRING = 1 * 1024 * 1024  # 1 MiB
MAX_ARRAY_ELEMENTS = 1_048_576
MAX_TENSORS = 65_536
MAX_TENSOR_DIMS = 8
MAX_ARRAY_DEPTH = 4

# GGUF metadata value type IDs (spec v2/v3)
_GGUF_TYPE_UINT8 = 0
_GGUF_TYPE_INT8 = 1
_GGUF_TYPE_UINT16 = 2
_GGUF_TYPE_INT16 = 3
_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_INT32 = 5
_GGUF_TYPE_FLOAT32 = 6
_GGUF_TYPE_BOOL = 7
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9
_GGUF_TYPE_UINT64 = 10
_GGUF_TYPE_INT64 = 11
_GGUF_TYPE_FLOAT64 = 12

_SCALAR_FORMATS: Dict[int, Tuple[str, int]] = {
    _GGUF_TYPE_UINT8: ("<B", 1),
    _GGUF_TYPE_INT8: ("<b", 1),
    _GGUF_TYPE_UINT16: ("<H", 2),
    _GGUF_TYPE_INT16: ("<h", 2),
    _GGUF_TYPE_UINT32: ("<I", 4),
    _GGUF_TYPE_INT32: ("<i", 4),
    _GGUF_TYPE_FLOAT32: ("<f", 4),
    _GGUF_TYPE_BOOL: ("<?", 1),
    _GGUF_TYPE_UINT64: ("<Q", 8),
    _GGUF_TYPE_INT64: ("<q", 8),
    _GGUF_TYPE_FLOAT64: ("<d", 8),
}

# GGML tensor type -> (block_elements, block_bytes)
_GGML_TYPE_BLOCK_SPEC: Dict[int, Tuple[int, int]] = {
    0: (1, 4),      # F32
    1: (1, 2),      # F16
    2: (32, 18),    # Q4_0
    3: (32, 20),    # Q4_1
    6: (32, 22),    # Q5_0
    7: (32, 24),    # Q5_1
    8: (32, 34),    # Q8_0
    9: (32, 36),    # Q8_1
    10: (256, 84),  # Q2_K
    11: (256, 110), # Q3_K
    12: (256, 144), # Q4_K
    13: (256, 176), # Q5_K
    14: (256, 210), # Q6_K
    15: (256, 292), # Q8_K
    16: (256, 66),  # IQ2_XXS
    17: (256, 74),  # IQ2_XS
    18: (256, 110), # IQ3_XXS
    19: (256, 56),  # IQ1_S
    20: (32, 18),   # IQ4_NL
    21: (256, 110), # IQ3_S
    22: (256, 82),  # IQ2_S
    23: (256, 136), # IQ4_XS
    24: (1, 1),     # I8
    25: (1, 2),     # I16
    26: (1, 4),     # I32
    27: (1, 8),     # I64
    28: (1, 8),     # F64
    29: (256, 56),  # IQ1_M
    30: (1, 2),     # BF16
    39: (32, 17),   # MXFP4
}

# Bytes per KV element including quantization scale overhead
_KV_QUANT_BYTES: Dict[str, float] = {
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 34.0 / 32.0,
    "q4_0": 18.0 / 32.0,
}

_CTX_LADDER: Tuple[int, ...] = (
    4096,
    8192,
    12288,
    16384,
    24576,
    32768,
    49152,
    65536,
    98304,
    131072,
    196608,
    262144,
)


class GgufHeaderError(ValueError):
    """Raised when a GGUF file is malformed, unsafe, or exceeds parser bounds."""


@dataclass(frozen=True)
class GgufTensorInfo:
    """Single tensor entry parsed from a GGUF tensor table."""

    name: str
    dims: Tuple[int, ...]
    ggml_type: int
    offset: int
    n_bytes: int


@dataclass(frozen=True)
class GgufHeaderInfo:
    """Normalized model metadata extracted from a GGUF v2/v3 file."""

    version: int
    tensor_count: int
    kv_count: int
    file_size_bytes: int
    architecture: str
    model_name: str
    context_length: int
    block_count: int
    attention_kv_layers: int
    head_count: int
    head_count_kv: int
    key_length: int
    value_length: int
    total_weight_bytes: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def weight_gb(self) -> float:
        return self.total_weight_bytes / float(1024**3)

    def kv_bytes_per_token(self, kv_quant: str = "f16") -> float:
        """Return KV-cache bytes consumed per token for *kv_quant*."""
        q = (kv_quant or "f16").strip().lower()
        bpe = _KV_QUANT_BYTES.get(q)
        if bpe is None:
            raise GgufHeaderError(f"Unsupported kv_quant {kv_quant!r}; expected one of {sorted(_KV_QUANT_BYTES)}")
        if self.attention_kv_layers <= 0 or self.head_count_kv <= 0:
            return 0.0
        return float(self.attention_kv_layers * self.head_count_kv * (self.key_length + self.value_length)) * bpe


@dataclass(frozen=True)
class GgufMemoryFit:
    """Exact hardware memory & KV-cache context fit result for a GGUF model."""

    fits: bool
    available_memory_gb: float
    os_reserve_gb: float
    weight_gb: float
    compute_buffer_gb: float
    kv_budget_gb: float
    kv_quant: str
    kv_bytes_per_token: float
    kv_mb_per_1k_tokens: float
    model_max_ctx: int
    recommended_n_ctx: int
    attention_kv_layers: int
    block_count: int
    reason: str


class _BoundedFdReader:
    """Strict byte-budget reader over an already-opened regular-file fd."""

    def __init__(self, fd: int, budget: int = HEADER_BYTE_BUDGET) -> None:
        self._fd = fd
        self._budget = budget
        self._read_total = 0
        self._buf = bytearray()
        self._pos = 0

    @property
    def bytes_consumed(self) -> int:
        return self._read_total - (len(self._buf) - self._pos)

    def _fill(self, needed: int) -> None:
        avail = len(self._buf) - self._pos
        if avail >= needed:
            return
        if self._pos > 0:
            del self._buf[: self._pos]
            self._pos = 0
        while len(self._buf) < needed:
            remaining_budget = self._budget - self._read_total
            if remaining_budget <= 0:
                raise GgufHeaderError(
                    f"GGUF header exceeded {self._budget} byte safety budget"
                )
            chunk_size = min(max(65536, needed - len(self._buf)), remaining_budget)
            chunk = os.read(self._fd, chunk_size)
            if not chunk:
                raise GgufHeaderError("Unexpected EOF while reading GGUF header")
            self._read_total += len(chunk)
            self._buf.extend(chunk)

    def read_exact(self, n: int) -> bytes:
        if n < 0:
            raise GgufHeaderError(f"Negative read size {n}")
        if self.bytes_consumed + n > self._budget:
            raise GgufHeaderError(
                f"GGUF header read of {n} bytes exceeds {self._budget} byte safety budget"
            )
        self._fill(n)
        out = bytes(self._buf[self._pos : self._pos + n])
        self._pos += n
        return out

    def skip_exact(self, n: int) -> None:
        if n < 0:
            raise GgufHeaderError(f"Negative skip size {n}")
        if self.bytes_consumed + n > self._budget:
            raise GgufHeaderError(
                f"GGUF header skip of {n} bytes exceeds {self._budget} byte safety budget"
            )
        remaining = n
        while remaining > 0:
            step = min(remaining, 65536)
            self._fill(step)
            self._pos += step
            remaining -= step

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read_exact(4))[0]

    def read_u64(self) -> int:
        return struct.unpack("<Q", self.read_exact(8))[0]

    def read_string(self) -> str:
        length = self.read_u64()
        if length > MAX_STRING:
            raise GgufHeaderError(
                f"GGUF string length {length} exceeds MAX_STRING ({MAX_STRING})"
            )
        raw = self.read_exact(length)
        return raw.decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        length = self.read_u64()
        if length > MAX_STRING:
            raise GgufHeaderError(
                f"GGUF string length {length} exceeds MAX_STRING ({MAX_STRING})"
            )
        self.skip_exact(length)


def _open_regular_gguf(path: str | Path) -> Tuple[int, int]:
    """Open *path* safely using O_NOFOLLOW and verify it is a regular file."""
    p = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(str(p), flags)
    except OSError as exc:
        raise GgufHeaderError(f"Cannot safely open GGUF file {p}: {exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise GgufHeaderError(
                f"Refusing non-regular GGUF path {p} (mode={oct(st.st_mode)})"
            )
        return fd, int(st.st_size)
    except Exception:
        os.close(fd)
        raise


def _read_value(
    reader: _BoundedFdReader,
    vtype: int,
    *,
    skip_bulk: bool = False,
    depth: int = 0,
) -> Any:
    if vtype in _SCALAR_FORMATS:
        fmt, size = _SCALAR_FORMATS[vtype]
        raw = reader.read_exact(size)
        return struct.unpack(fmt, raw)[0]
    if vtype == _GGUF_TYPE_STRING:
        if skip_bulk:
            reader.skip_string()
            return None
        return reader.read_string()
    if vtype == _GGUF_TYPE_ARRAY:
        if depth >= MAX_ARRAY_DEPTH:
            raise GgufHeaderError(f"GGUF nested array depth exceeded {MAX_ARRAY_DEPTH}")
        elem_type = reader.read_u32()
        count = reader.read_u64()
        if count > MAX_ARRAY_ELEMENTS:
            raise GgufHeaderError(
                f"GGUF array element count {count} exceeds MAX_ARRAY_ELEMENTS ({MAX_ARRAY_ELEMENTS})"
            )
        if elem_type in _SCALAR_FORMATS:
            fmt, size = _SCALAR_FORMATS[elem_type]
            total_bytes = count * size
            if skip_bulk or count > 256:
                reader.skip_exact(total_bytes)
                return {"_skipped_array": True, "elem_type": elem_type, "count": count}
            raw = reader.read_exact(total_bytes)
            return [struct.unpack_from(fmt, raw, i * size)[0] for i in range(count)]
        if elem_type == _GGUF_TYPE_STRING:
            if skip_bulk or count > 256:
                for _ in range(count):
                    reader.skip_string()
                return {"_skipped_array": True, "elem_type": elem_type, "count": count}
            return [reader.read_string() for _ in range(count)]
        if elem_type == _GGUF_TYPE_ARRAY:
            items = [
                _read_value(reader, elem_type, skip_bulk=skip_bulk, depth=depth + 1)
                for _ in range(count)
            ]
            return None if skip_bulk else items
        raise GgufHeaderError(f"Unknown GGUF array element type {elem_type}")
    raise GgufHeaderError(f"Unknown GGUF value type {vtype}")


def _tensor_byte_size(dims: Tuple[int, ...], ggml_type: int) -> int:
    spec = _GGML_TYPE_BLOCK_SPEC.get(ggml_type)
    if spec is None or not dims:
        return 0
    block_elems, block_bytes = spec
    n_elems = 1
    for d in dims:
        n_elems *= d
    blocks = (n_elems + block_elems - 1) // block_elems
    return blocks * block_bytes


def _as_int(val: Any, default: int = 0) -> int:
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(val)
    return default


def _infer_attention_kv_layers(
    arch: str,
    metadata: Dict[str, Any],
    tensors: List[GgufTensorInfo],
    block_count: int,
) -> Tuple[int, int]:
    """Return ``(attention_kv_layers, effective_head_count_kv)``.

    Handles hybrid Gated DeltaNet / Mamba / sliding-window architectures
    (such as Qwen3.5 / Qwen3.8) where only a subset of transformer blocks carry
    standard KV-cache attention layers.
    """
    kv_raw = metadata.get(f"{arch}.attention.head_count_kv")
    if isinstance(kv_raw, list):
        non_zero = [_as_int(v, 0) for v in kv_raw if _as_int(v, 0) > 0]
        if non_zero:
            return len(non_zero), max(non_zero)
        return 0, 0

    head_count = _as_int(metadata.get(f"{arch}.attention.head_count"), 0)
    head_count_kv = _as_int(kv_raw, head_count)

    # Check explicit full-attention interval (used by Qwen3.5 / Qwen3.8 DeltaNet hybrids)
    interval = _as_int(metadata.get(f"{arch}.full_attention_interval"), 0)
    if interval > 1 and block_count > 0:
        return max(1, block_count // interval), head_count_kv

    # Check tensor names: only down-count KV layers when hybrid SSM/DeltaNet tensors
    # are actually present (never misclassify a split multi-shard GGUF as hybrid).
    attn_k_blocks = set()
    has_ssm_or_deltanet = False
    for t in tensors:
        name = t.name
        if "ssm_" in name or "deltanet" in name or "conv1d" in name or "attn_qkv" in name:
            has_ssm_or_deltanet = True
        if name.startswith("blk.") and ".attn_k." in name:
            parts = name.split(".")
            if len(parts) >= 3 and parts[1].isdigit():
                attn_k_blocks.add(int(parts[1]))

    if attn_k_blocks and has_ssm_or_deltanet:
        return len(attn_k_blocks), head_count_kv

    return max(0, block_count), head_count_kv


def _parse_gguf(path: str | Path, *, collect_tensors: bool) -> Tuple[GgufHeaderInfo, List[GgufTensorInfo]]:
    fd, file_size = _open_regular_gguf(path)
    try:
        reader = _BoundedFdReader(fd, budget=HEADER_BYTE_BUDGET)
        magic = reader.read_exact(4)
        if magic != GGUF_MAGIC:
            raise GgufHeaderError(f"Invalid GGUF magic {magic!r}; expected {GGUF_MAGIC!r}")

        version = reader.read_u32()
        if version not in SUPPORTED_VERSIONS:
            raise GgufHeaderError(
                f"Unsupported GGUF version {version}; supported versions are {SUPPORTED_VERSIONS}"
            )

        tensor_count = reader.read_u64()
        kv_count = reader.read_u64()
        if tensor_count > MAX_TENSORS:
            raise GgufHeaderError(
                f"GGUF tensor_count {tensor_count} exceeds MAX_TENSORS ({MAX_TENSORS})"
            )
        if kv_count > MAX_KV:
            raise GgufHeaderError(
                f"GGUF kv_count {kv_count} exceeds MAX_KV ({MAX_KV})"
            )

        metadata: Dict[str, Any] = {}
        for _ in range(kv_count):
            key = reader.read_string()
            vtype = reader.read_u32()
            skip_bulk = key.startswith("tokenizer.")
            val = _read_value(reader, vtype, skip_bulk=skip_bulk, depth=0)
            if not skip_bulk:
                metadata[key] = val

        tensors: List[GgufTensorInfo] = []
        known_weight_bytes = 0
        unknown_type_seen = False

        for _ in range(tensor_count):
            name = reader.read_string()
            n_dims = reader.read_u32()
            if n_dims > MAX_TENSOR_DIMS:
                raise GgufHeaderError(
                    f"Tensor {name!r} has {n_dims} dims > MAX_TENSOR_DIMS ({MAX_TENSOR_DIMS})"
                )
            dims = tuple(reader.read_u64() for _ in range(n_dims))
            ggml_type = reader.read_u32()
            offset = reader.read_u64()
            n_bytes = _tensor_byte_size(dims, ggml_type)
            if n_bytes == 0 and dims:
                unknown_type_seen = True
            known_weight_bytes += n_bytes
            if collect_tensors or (name.startswith("blk.") and (".attn_k." in name or "ssm_" in name or "deltanet" in name or "conv1d" in name or "attn_qkv" in name)):
                tensors.append(
                    GgufTensorInfo(
                        name=name,
                        dims=dims,
                        ggml_type=ggml_type,
                        offset=offset,
                        n_bytes=n_bytes,
                    )
                )

        # Fallback to file size minus header when tensor table omits sizes or uses future quant IDs
        data_bytes_fallback = max(0, file_size - reader.bytes_consumed)
        total_weight_bytes = (
            data_bytes_fallback
            if (unknown_type_seen or known_weight_bytes == 0) and data_bytes_fallback > 0
            else known_weight_bytes
        )

        def _sanitize_meta_str(raw_val: Any, fallback: str) -> str:
            cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", str(raw_val or "")).strip()
            return cleaned or fallback

        arch = _sanitize_meta_str(metadata.get("general.architecture"), "llama")
        model_name = _sanitize_meta_str(metadata.get("general.name"), Path(path).stem)
        context_length = _as_int(metadata.get(f"{arch}.context_length"), 32768)
        block_count = _as_int(metadata.get(f"{arch}.block_count"), 0)
        head_count = _as_int(metadata.get(f"{arch}.attention.head_count"), 0)
        embd = _as_int(metadata.get(f"{arch}.embedding_length"), 0)
        default_head_dim = (embd // head_count) if (embd > 0 and head_count > 0) else 128
        key_length = _as_int(metadata.get(f"{arch}.attention.key_length"), default_head_dim)
        value_length = _as_int(metadata.get(f"{arch}.attention.value_length"), key_length)

        attention_kv_layers, head_count_kv = _infer_attention_kv_layers(
            arch, metadata, tensors, block_count
        )

        info = GgufHeaderInfo(
            version=version,
            tensor_count=tensor_count,
            kv_count=kv_count,
            file_size_bytes=file_size,
            architecture=arch,
            model_name=model_name,
            context_length=context_length,
            block_count=block_count,
            attention_kv_layers=attention_kv_layers,
            head_count=head_count,
            head_count_kv=head_count_kv,
            key_length=key_length,
            value_length=value_length,
            total_weight_bytes=total_weight_bytes,
            metadata=metadata,
        )
        return info, (tensors if collect_tensors else [])
    finally:
        os.close(fd)


def read_gguf_header(path: str | Path) -> GgufHeaderInfo:
    """Read and validate a GGUF v2/v3 header and summarize weights & KV layers."""
    info, _ = _parse_gguf(path, collect_tensors=False)
    return info


def read_gguf_tensor_table(path: str | Path) -> Tuple[GgufHeaderInfo, List[GgufTensorInfo]]:
    """Read and validate a GGUF v2/v3 header plus its full tensor table."""
    return _parse_gguf(path, collect_tensors=True)


def _snap_context_length(raw_tokens: int, max_ctx: int) -> int:
    capped = min(max(0, raw_tokens), max(4096, max_ctx))
    if capped < _CTX_LADDER[0]:
        return max(0, (capped // 512) * 512)
    best = _CTX_LADDER[0]
    for step in _CTX_LADDER:
        if step <= capped:
            best = step
        else:
            break
    return min(best, max_ctx) if max_ctx > 0 else best


def estimate_gguf_memory_fit(
    info: GgufHeaderInfo,
    *,
    total_memory_gb: float,
    os_name: str = "darwin",
    kv_quant: str = "auto",
    target_n_ctx: Optional[int] = None,
    os_reserve_gb: Optional[float] = None,
) -> GgufMemoryFit:
    """Compute exact fitted ``n_ctx`` and KV quantization for *info* on host RAM/VRAM."""
    mem_gb = max(0.0, float(total_memory_gb or 0.0))
    if os_reserve_gb is not None:
        reserve = max(0.0, float(os_reserve_gb))
    elif (os_name or "").lower() == "darwin":
        # Apple Silicon unified memory reserves ~25% (min 4 GiB) for macOS + WindowServer
        reserve = round(max(4.0, mem_gb * 0.25), 2) if mem_gb > 0 else 4.0
    else:
        # Discrete VRAM only needs driver/display overhead
        reserve = round(max(1.0, min(2.0, mem_gb * 0.08)), 2) if mem_gb > 0 else 1.0

    weight_gb = info.weight_gb
    # Compute/activation scratch buffer scales gently with model weights (0.4 - 1.5 GiB)
    compute_buf_gb = round(max(0.4, min(1.5, weight_gb * 0.12)), 2)
    kv_budget_gb = round(max(0.0, mem_gb - reserve - weight_gb - compute_buf_gb), 3)

    requested_q = (kv_quant or "auto").strip().lower()
    if requested_q == "auto":
        # Prefer f16 if 32k+ fits comfortably, else q8_0, else q4_0
        candidates = ("f16", "q8_0", "q4_0")
        desired_floor = target_n_ctx or min(32768, info.context_length or 32768)
        chosen_q = "q4_0"
        for q in candidates:
            bpt = info.kv_bytes_per_token(q)
            if bpt <= 0:
                chosen_q = q
                break
            max_tok = int((kv_budget_gb * (1024**3)) / bpt)
            if max_tok >= desired_floor:
                chosen_q = q
                break
    else:
        if requested_q not in _KV_QUANT_BYTES:
            raise GgufHeaderError(
                f"Unsupported kv_quant {kv_quant!r}; expected auto or one of {sorted(_KV_QUANT_BYTES)}"
            )
        chosen_q = requested_q

    bpt = info.kv_bytes_per_token(chosen_q)
    kv_mb_1k = (bpt * 1000.0) / (1024.0 * 1024.0)
    model_max_ctx = info.context_length if info.context_length > 0 else 32768

    if kv_budget_gb <= 0.0:
        return GgufMemoryFit(
            fits=False,
            available_memory_gb=round(mem_gb, 2),
            os_reserve_gb=reserve,
            weight_gb=round(weight_gb, 3),
            compute_buffer_gb=compute_buf_gb,
            kv_budget_gb=0.0,
            kv_quant=chosen_q,
            kv_bytes_per_token=round(bpt, 2),
            kv_mb_per_1k_tokens=round(kv_mb_1k, 3),
            model_max_ctx=model_max_ctx,
            recommended_n_ctx=0,
            attention_kv_layers=info.attention_kv_layers,
            block_count=info.block_count,
            reason=(
                f"Model weights ({weight_gb:.2f} GiB) + compute ({compute_buf_gb:.2f} GiB) "
                f"+ OS reserve ({reserve:.2f} GiB) exceed {mem_gb:.1f} GiB available memory."
            ),
        )

    if bpt > 0:
        raw_max_tokens = int((kv_budget_gb * (1024**3)) / bpt)
    else:
        raw_max_tokens = model_max_ctx

    rec_ctx = _snap_context_length(raw_max_tokens, model_max_ctx)
    if target_n_ctx is not None and target_n_ctx > 0:
        rec_ctx = min(rec_ctx, int(target_n_ctx))

    fits = rec_ctx >= 4096
    reason = (
        f"Fits {rec_ctx:,} ctx with {chosen_q} KV cache "
        f"({kv_budget_gb:.2f} GiB KV budget, {info.attention_kv_layers}/{info.block_count} KV layers)"
        if fits
        else f"Insufficient KV budget ({kv_budget_gb:.2f} GiB) for minimum 4096 context (max ~{raw_max_tokens} tokens)."
    )

    return GgufMemoryFit(
        fits=fits,
        available_memory_gb=round(mem_gb, 2),
        os_reserve_gb=reserve,
        weight_gb=round(weight_gb, 3),
        compute_buffer_gb=compute_buf_gb,
        kv_budget_gb=kv_budget_gb,
        kv_quant=chosen_q,
        kv_bytes_per_token=round(bpt, 2),
        kv_mb_per_1k_tokens=round(kv_mb_1k, 3),
        model_max_ctx=model_max_ctx,
        recommended_n_ctx=rec_ctx,
        attention_kv_layers=info.attention_kv_layers,
        block_count=info.block_count,
        reason=reason,
    )


__all__ = [
    "GGUF_MAGIC",
    "HEADER_BYTE_BUDGET",
    "MAX_ARRAY_ELEMENTS",
    "MAX_KV",
    "MAX_STRING",
    "MAX_TENSORS",
    "SUPPORTED_VERSIONS",
    "GgufHeaderError",
    "GgufHeaderInfo",
    "GgufMemoryFit",
    "GgufTensorInfo",
    "estimate_gguf_memory_fit",
    "read_gguf_header",
    "read_gguf_tensor_table",
]
