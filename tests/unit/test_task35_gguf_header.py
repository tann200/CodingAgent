"""TASK-35 tests: pure-stdlib bounded GGUF header reader, memory fitting, and local-launch CLI."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from src.cli.local_launch_cmd import build_local_launch_plan
from src.core.inference.gguf_header import (
    GGUF_MAGIC,
    MAX_ARRAY_ELEMENTS,
    MAX_KV,
    MAX_STRING,
    MAX_TENSORS,
    GgufHeaderError,
    estimate_gguf_memory_fit,
    read_gguf_header,
    read_gguf_tensor_table,
)
from src.core.inference.kv_cache_governor import create_governor_from_gguf
from src.core.inference.local_runtime_autotune import compute_safe_local_defaults
from src.main import main as cli_main


def _pack_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _pack_kv(key: str, vtype: int, val: Any) -> bytes:
    out = _pack_str(key) + struct.pack("<I", vtype)
    if vtype == 4:  # UINT32
        out += struct.pack("<I", int(val))
    elif vtype == 6:  # FLOAT32
        out += struct.pack("<f", float(val))
    elif vtype == 7:  # BOOL
        out += struct.pack("<?", bool(val))
    elif vtype == 8:  # STRING
        out += _pack_str(str(val))
    elif vtype == 9:  # ARRAY (elem_type, items)
        elem_type, items = val
        out += struct.pack("<IQ", elem_type, len(items))
        for item in items:
            if elem_type == 4:
                out += struct.pack("<I", int(item))
            elif elem_type == 8:
                out += _pack_str(str(item))
    else:
        raise ValueError(f"Unsupported test vtype {vtype}")
    return out


def _build_synthetic_gguf(
    path: Path,
    *,
    version: int = 3,
    kvs: List[Tuple[str, int, Any]] | None = None,
    tensors: List[Tuple[str, Tuple[int, ...], int, int]] | None = None,
) -> Path:
    kvs = kvs or []
    tensors = tensors or []
    buf = bytearray()
    buf += GGUF_MAGIC
    buf += struct.pack("<IQQ", version, len(tensors), len(kvs))
    for k, vt, v in kvs:
        buf += _pack_kv(k, vt, v)
    for name, dims, ggml_type, offset in tensors:
        buf += _pack_str(name)
        buf += struct.pack("<I", len(dims))
        for d in dims:
            buf += struct.pack("<Q", d)
        buf += struct.pack("<IQ", ggml_type, offset)
    path.write_bytes(bytes(buf))
    return path


def test_read_valid_gguf_v2_and_v3_and_tensor_table(tmp_path: Path) -> None:
    # Q4_K (type 12): 256 elems -> 144 bytes. 4096 * 4096 = 16,777,216 elems -> 65536 blocks -> 9,437,184 bytes
    for ver in (2, 3):
        p = _build_synthetic_gguf(
            tmp_path / f"model_v{ver}.gguf",
            version=ver,
            kvs=[
                ("general.architecture", 8, "qwen3"),
                ("general.name", 8, "Qwen3-9B-Q4_K_M"),
                ("qwen3.context_length", 4, 131072),
                ("qwen3.block_count", 4, 36),
                ("qwen3.attention.head_count", 4, 32),
                ("qwen3.attention.head_count_kv", 4, 8),
                ("qwen3.attention.key_length", 4, 128),
                ("qwen3.attention.value_length", 4, 128),
                ("tokenizer.ggml.tokens", 9, (8, ["<bos>", "hello", "world"])),
            ],
            tensors=[
                ("blk.0.attn_q.weight", (4096, 4096), 12, 0),
                ("blk.0.attn_k.weight", (1024, 4096), 12, 9437184),
            ],
        )
        info, tensors = read_gguf_tensor_table(p)
        assert info.version == ver
        assert info.architecture == "qwen3"
        assert info.model_name == "Qwen3-9B-Q4_K_M"
        assert info.context_length == 131072
        assert info.block_count == 36
        assert info.attention_kv_layers == 36
        assert info.head_count_kv == 8
        assert info.key_length == 128
        assert info.value_length == 128
        assert len(tensors) == 2
        assert info.total_weight_bytes == 9437184 + 2359296
        # Tokenizer bulk array was skipped cleanly without polluting metadata
        assert "tokenizer.ggml.tokens" not in info.metadata


def test_hybrid_gated_deltanet_kv_layer_counting(tmp_path: Path) -> None:
    # Mode 1: explicit full_attention_interval = 4 on 32 blocks -> 8 attention KV layers
    p_interval = _build_synthetic_gguf(
        tmp_path / "qwen38_hybrid.gguf",
        kvs=[
            ("general.architecture", 8, "qwen35"),
            ("general.name", 8, "Qwen3.8-9B-Distill"),
            ("qwen35.context_length", 4, 131072),
            ("qwen35.block_count", 4, 32),
            ("qwen35.full_attention_interval", 4, 4),
            ("qwen35.attention.head_count", 4, 16),
            ("qwen35.attention.head_count_kv", 4, 4),
            ("qwen35.attention.key_length", 4, 256),
            ("qwen35.attention.value_length", 4, 256),
        ],
    )
    info = read_gguf_header(p_interval)
    assert info.block_count == 32
    assert info.attention_kv_layers == 8
    assert info.head_count_kv == 4
    # 8 layers * 4 heads * (256+256) * 2.0 B (f16) = 32,768 B/token
    assert info.kv_bytes_per_token("f16") == 32768.0

    # Mode 2: per-layer head_count_kv array (zeros on DeltaNet layers, 4 on attention layers)
    per_layer = [0, 0, 0, 4] * 8
    p_arr = _build_synthetic_gguf(
        tmp_path / "qwen38_arr.gguf",
        kvs=[
            ("general.architecture", 8, "qwen35"),
            ("qwen35.block_count", 4, 32),
            ("qwen35.attention.head_count_kv", 9, (4, per_layer)),
            ("qwen35.attention.key_length", 4, 256),
            ("qwen35.attention.value_length", 4, 256),
        ],
    )
    info_arr = read_gguf_header(p_arr)
    assert info_arr.attention_kv_layers == 8
    assert info_arr.head_count_kv == 4

    # Mode 3: sparse blk.*.attn_k.weight + ssm_* tensors
    p_tensors = _build_synthetic_gguf(
        tmp_path / "qwen38_tensors.gguf",
        kvs=[
            ("general.architecture", 8, "qwen35"),
            ("qwen35.block_count", 4, 8),
            ("qwen35.attention.head_count_kv", 4, 4),
        ],
        tensors=[
            ("blk.0.ssm_a.weight", (128,), 0, 0),
            ("blk.3.attn_k.weight", (512, 512), 12, 512),
            ("blk.7.attn_k.weight", (512, 512), 12, 1024),
        ],
    )
    info_t = read_gguf_header(p_tensors)
    assert info_t.attention_kv_layers == 2


def test_dos_bounds_and_malformed_gguf_rejection(tmp_path: Path) -> None:
    # Bad magic
    bad_magic = tmp_path / "bad.gguf"
    bad_magic.write_bytes(b"NOPE" + b"\x00" * 32)
    with pytest.raises(GgufHeaderError, match="Invalid GGUF magic"):
        read_gguf_header(bad_magic)

    # Unsupported version
    bad_ver = tmp_path / "v99.gguf"
    bad_ver.write_bytes(GGUF_MAGIC + struct.pack("<IQQ", 99, 0, 0))
    with pytest.raises(GgufHeaderError, match="Unsupported GGUF version"):
        read_gguf_header(bad_ver)

    # Truncated file
    trunc = tmp_path / "trunc.gguf"
    trunc.write_bytes(GGUF_MAGIC + struct.pack("<IQQ", 3, 0, 1) + _pack_str("general.name"))
    with pytest.raises(GgufHeaderError, match="Unexpected EOF"):
        read_gguf_header(trunc)

    # Oversized KV count
    too_many_kv = tmp_path / "kv_dos.gguf"
    too_many_kv.write_bytes(GGUF_MAGIC + struct.pack("<IQQ", 3, 0, MAX_KV + 1))
    with pytest.raises(GgufHeaderError, match="MAX_KV"):
        read_gguf_header(too_many_kv)

    # Oversized tensor count
    too_many_t = tmp_path / "t_dos.gguf"
    too_many_t.write_bytes(GGUF_MAGIC + struct.pack("<IQQ", 3, MAX_TENSORS + 1, 0))
    with pytest.raises(GgufHeaderError, match="MAX_TENSORS"):
        read_gguf_header(too_many_t)

    # Oversized string length
    big_str = tmp_path / "str_dos.gguf"
    big_str.write_bytes(
        GGUF_MAGIC + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", MAX_STRING + 1)
    )
    with pytest.raises(GgufHeaderError, match="MAX_STRING"):
        read_gguf_header(big_str)

    # Oversized array element count
    big_arr = tmp_path / "arr_dos.gguf"
    big_arr.write_bytes(
        GGUF_MAGIC
        + struct.pack("<IQQ", 3, 0, 1)
        + _pack_str("evil.array")
        + struct.pack("<IIQ", 9, 4, MAX_ARRAY_ELEMENTS + 1)
    )
    with pytest.raises(GgufHeaderError, match="MAX_ARRAY_ELEMENTS"):
        read_gguf_header(big_arr)


def test_refuses_symlinks_and_fifo_special_files(tmp_path: Path) -> None:
    real = _build_synthetic_gguf(tmp_path / "real.gguf", kvs=[("general.architecture", 8, "llama")])
    link = tmp_path / "link.gguf"
    link.symlink_to(real)
    with pytest.raises(GgufHeaderError, match="Cannot safely open"):
        read_gguf_header(link)

    fifo = tmp_path / "pipe.gguf"
    os.mkfifo(fifo)
    with pytest.raises(GgufHeaderError, match="non-regular|Cannot safely open"):
        read_gguf_header(fifo)


def test_memory_fit_16gb_32gb_and_oom_and_autotune_governor(tmp_path: Path) -> None:
    # Build ~5.6 GiB weight model using Q8_0 tensors (32 elems -> 34 bytes)
    # 5.6 GiB = 6,012,954,214 bytes -> ~5.66e9 elems -> 177,000,000 blocks of 32
    p = _build_synthetic_gguf(
        tmp_path / "qwen3_9b_q4.gguf",
        kvs=[
            ("general.architecture", 8, "qwen3"),
            ("general.name", 8, "Qwen3-9B-Q4"),
            ("qwen3.context_length", 4, 131072),
            ("qwen3.block_count", 4, 36),
            ("qwen3.attention.head_count", 4, 32),
            ("qwen3.attention.head_count_kv", 4, 8),
            ("qwen3.attention.key_length", 4, 128),
            ("qwen3.attention.value_length", 4, 128),
        ],
        tensors=[("model.weights", (176_000_000, 32), 8, 0)],
    )
    info = read_gguf_header(p)
    assert 5.4 < info.weight_gb < 5.7

    # 16 GB Apple Silicon: 4.0 GiB OS reserve + ~5.57 GiB weights + ~0.67 GiB compute -> ~5.76 GiB KV budget
    fit16 = estimate_gguf_memory_fit(info, total_memory_gb=16.0, os_name="darwin", kv_quant="auto")
    assert fit16.fits is True
    assert fit16.recommended_n_ctx >= 32768
    assert fit16.kv_quant in ("f16", "q8_0", "q4_0")

    # 32 GB Apple Silicon fits >= 65536 ctx with f16
    fit32 = estimate_gguf_memory_fit(info, total_memory_gb=32.0, os_name="darwin", kv_quant="auto")
    assert fit32.fits is True
    assert fit32.recommended_n_ctx >= 65536
    assert fit32.kv_quant == "f16"

    # 8 GB Apple Silicon -> weights + OS reserve exceed 8 GiB -> fits=False (OOM refusal)
    fit8 = estimate_gguf_memory_fit(info, total_memory_gb=8.0, os_name="darwin", kv_quant="auto")
    assert fit8.fits is False
    assert fit8.recommended_n_ctx == 0

    # Autotune wiring
    defaults = compute_safe_local_defaults(
        ram_gb=16.0, vram_gb=16.0, os_name="darwin", gguf_path=p
    )
    assert defaults["gguf_fit"]["fits"] is True
    assert defaults["recommended_n_ctx"] == fit16.recommended_n_ctx

    # KVCacheGovernor wiring
    gov = create_governor_from_gguf(p, max_vram_gb=16.0, os_name="darwin")
    assert gov is not None
    assert gov.max_tokens >= fit16.recommended_n_ctx
    assert create_governor_from_gguf(p, max_vram_gb=8.0, os_name="darwin") is None


def test_local_launch_cli_script_json_and_oom_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model_file = _build_synthetic_gguf(
        tmp_path / "my model 9b.gguf",
        kvs=[
            ("general.architecture", 8, "qwen3"),
            ("general.name", 8, "My Model 9B"),
            ("qwen3.context_length", 4, 65536),
            ("qwen3.block_count", 4, 32),
            ("qwen3.attention.head_count", 4, 32),
            ("qwen3.attention.head_count_kv", 4, 8),
            ("qwen3.attention.key_length", 4, 128),
            ("qwen3.attention.value_length", 4, 128),
        ],
        tensors=[("model.weights", (160_000_000, 32), 8, 0)],
    )

    # --script output quotes paths with spaces and includes llama-server flags
    rc = cli_main(["local-launch", str(model_file), "--script", "--memory-gb", "16", "--kv-quant", "q8_0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#!/usr/bin/env bash" in out
    assert "--jinja" in out
    assert "--cache-reuse 256" in out
    assert "-fa on" in out
    assert "-ctk q8_0" in out
    assert "'my model 9b.gguf'" in out or "my model 9b.gguf'" in out

    # --json output
    rc_json = cli_main(["local-launch", str(model_file), "--json", "--memory-gb", "32"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fits"] is True
    assert payload["recommended_n_ctx"] >= 32768
    assert payload["architecture"] == "qwen3"

    # --autostart refuses OOM configuration without launching anything
    rc_oom = cli_main(["local-launch", str(model_file), "--autostart", "--memory-gb", "6"])
    assert rc_oom == 1
    err = capsys.readouterr().err
    assert "refusing to autostart OOM" in err

    # Direct helper check
    plan = build_local_launch_plan(model_file, memory_gb=16.0, os_name="darwin")
    assert plan["fits"] is True
    assert "--ctx-size" in plan["command"]
