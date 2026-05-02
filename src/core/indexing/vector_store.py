"""
Lightweight VectorStore stub

This module intentionally provides a minimal, dependency-free VectorStore API so
the rest of the codebase can opt into semantic-search features when available.
It deliberately does NOT depend on external vector DBs or heavy ML stacks
(pyarrow, pandas, pydantic, sentence-transformers). The implementation is a
safe no-op / in-memory stub.

v2 Phase 3: RAM-optimized embedding cache for 64GB systems.
"""

from __future__ import annotations

from typing import Any, Dict, List
import hashlib
import logging
import json
import re
from collections import OrderedDict
from pathlib import Path

from src.tools.tools_config import agent_context_path

logger = logging.getLogger(__name__)

try:
    from src.core.io_utils import atomic_write_json as _atomic_write_json
except Exception:
    _atomic_write_json = None  # type: ignore[assignment]


# v2 Phase 3: LRU cache for embeddings (RAM optimization)
_EMBEDDING_CACHE: OrderedDict[str, List[float]] = OrderedDict()
_EMBEDDING_CACHE_LIMIT = 10000  # Max embeddings to cache


def _get_cached_embedding(text: str, dim: int = 8) -> List[float]:
    """Get cached embedding or compute and cache it.

    For 64GB RAM systems, caching up to 10K embeddings is safe
    (~10K * 8 * 4 bytes = 320KB for float32).
    """
    cache_key = f"{text[:256]}"  # Truncate for cache key
    if cache_key in _EMBEDDING_CACHE:
        _EMBEDDING_CACHE.move_to_end(cache_key)
        return _EMBEDDING_CACHE[cache_key]

    # Compute embedding
    h = hashlib.sha256(str(text).encode()).digest()
    vec: List[float] = []
    for i in range(dim):
        b1 = h[(i * 2) % len(h)]
        b2 = h[(i * 2 + 1) % len(h)]
        v = ((b1 << 8) + b2) / 65535.0
        vec.append((v * 2.0) - 1.0)

    # Cache it
    _EMBEDDING_CACHE[cache_key] = vec
    if len(_EMBEDDING_CACHE) > _EMBEDDING_CACHE_LIMIT:
        _EMBEDDING_CACHE.popitem(last=False)  # LRU eviction

    return vec


def clear_embedding_cache() -> None:
    """Clear the embedding cache (call after memory pressure)."""
    _EMBEDDING_CACHE.clear()


def get_embedding_cache_size() -> int:
    """Return number of cached embeddings."""
    return len(_EMBEDDING_CACHE)


class _DummyModel:
    """Deterministic lightweight embedder used when a real model is unavailable.

    It produces small fixed-size vectors derived from a SHA256 digest so tests
    and simple in-memory usages behave deterministically without heavy deps.

    v2 Phase 3: Uses LRU-cached embeddings for 64GB RAM optimization.
    """

    def __init__(self, dim: int = 8) -> None:
        self._dim = int(dim)

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, texts: Any) -> List[List[float]]:
        if isinstance(texts, str):
            texts = [texts]
        out: List[List[float]] = []
        for t in texts:
            vec = _get_cached_embedding(str(t), self._dim)
            out.append(vec)
        return out


class VectorStore:
    """Minimal VectorStore API (no external dependencies).

    This stub provides the methods used across the codebase but implements them
    as in-memory or no-op operations so removing external vector DB backends is safe.
    """

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self._model = _DummyModel()

    @property
    def model(self) -> _DummyModel:
        return self._model

    def index_code(self, repo_index: Dict[str, Any]) -> None:
        """Persist a minimal on-disk index of symbols so searches work in the
        stub implementation.

        This creates a lightweight directory under .agent-context/vectorstore and
        writes a symbols.json file containing the flattened repo index symbols
        (the minimal data other modules/tests expect). The implementation is
        intentionally simple and dependency-free.
        """
        base = agent_context_path(Path(self.workdir)) / "vectorstore"
        base.mkdir(parents=True, exist_ok=True)

        symbols = repo_index.get("symbols", []) if isinstance(repo_index, dict) else []
        try:
            symbols_path = base / "symbols.json"
            symbols_path.parent.mkdir(parents=True, exist_ok=True)
            if _atomic_write_json is not None:
                if not _atomic_write_json(symbols_path, symbols, logger=logger):
                    logger.warning(
                        "VectorStore: failed to write symbols.json to %s", symbols_path
                    )
            else:
                symbols_path.write_text(json.dumps(symbols, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to write vectorstore symbols.json")

        # Keep an in-memory cache for faster subsequent searches in the same
        # process.
        self._symbols = list(symbols)
        logger.debug("VectorStore.index_code persisted %d symbols", len(self._symbols))

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Basic token-based search over the persisted symbols.

        NOTE: This stub intentionally performs a light-weight substring/token
        match rather than vector similarity. It also removes any heavy binary
        "vector" payload from results before returning to callers (the full
        backend implementations typically drop the vector column to avoid
        memory/serialization issues).

        The comment below documents the behavior: drop the 'vector' column
        before returning results.
        """
        # drop 'vector' column before returning results

        # Ensure in-memory cache populated (lazy load from disk)
        if not getattr(self, "_symbols", None):
            try:
                path = (
                    Path(self.workdir)
                    / ".agent-context"
                    / "vectorstore"
                    / "symbols.json"
                )
                if path.exists():
                    self._symbols = json.loads(path.read_text())
                else:
                    self._symbols = []
            except Exception:
                logger.exception(
                    "Failed to load persisted symbols for VectorStore.search"
                )
                self._symbols = []

        q = (query or "").lower()
        tokens = [t for t in re.split(r"[\s_\-/\\]+", q) if t]

        results: List[Dict[str, Any]] = []
        for sym in self._symbols or []:
            name = (sym.get("symbol_name") or sym.get("name") or "").lower()
            file_path = (sym.get("file_path") or "").lower()

            if not tokens:
                match = True
            else:
                match = any(tok in name or tok in file_path for tok in tokens)

            if match:
                # copy minimal fields and ensure vector payload removed
                res = dict(sym)
                # Remove any large binary vector payloads if present
                res.pop("vector", None)
                results.append(res)
                if len(results) >= limit:
                    break

        logger.debug("VectorStore.search(%r) -> %d results", query, len(results))
        return results

    def add_memory(self, text: str, metadata: Dict[str, Any]) -> None:
        logger.debug("VectorStore.add_memory called (no-op in stub)")

    def search_memories(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        logger.debug("VectorStore.search_memories called (returns empty list in stub)")
        return []


__all__ = ["VectorStore"]
