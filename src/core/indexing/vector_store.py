"""
Lightweight VectorStore stub

This module intentionally provides a minimal, dependency-free VectorStore API so
the rest of the codebase can opt into semantic-search features when available.
It deliberately does NOT depend on external vector DBs or heavy ML stacks
(pyarrow, pandas, pydantic). When ``sentence-transformers`` is installed it is
used automatically for real semantic search; otherwise a fast SHA-256 stub is
used as a graceful fallback.

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

# ---------------------------------------------------------------------------
# sentence-transformers lazy import — real semantic model when available
# ---------------------------------------------------------------------------

_ST_MODEL: Any = None  # cached SentenceTransformer instance or None
_ST_AVAILABLE: bool | None = None  # None = not yet probed


def _get_st_model() -> Any:
    """Return a SentenceTransformer model on first call; None if unavailable.

    Uses 'all-MiniLM-L6-v2' — a small (80 MB), fast, OS/stack agnostic model
    that runs on CPU without a GPU.  The result is module-level cached so the
    expensive load happens at most once per process.
    """
    global _ST_MODEL, _ST_AVAILABLE
    if _ST_AVAILABLE is not None:
        return _ST_MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        _ST_AVAILABLE = True
        logger.info("VectorStore: sentence-transformers loaded (all-MiniLM-L6-v2)")
    except Exception as exc:
        _ST_AVAILABLE = False
        logger.debug("VectorStore: sentence-transformers unavailable (%s), using SHA-256 stub", exc)
    return _ST_MODEL

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
        # Prefer real sentence-transformers model when available
        st = _get_st_model()
        if st is not None:
            try:
                embeddings = st.encode(list(texts), convert_to_numpy=False)
                return [list(map(float, e)) for e in embeddings]
            except Exception as exc:
                logger.debug("_DummyModel.encode: ST model failed (%s), falling back to stub", exc)
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
        """Search over persisted symbols using semantic or token similarity.

        When ``sentence-transformers`` is available, cosine similarity is used
        for genuine semantic search.  Otherwise falls back to token overlap.

        NOTE: the 'vector' column is always stripped from returned results.
        """
        # Ensure in-memory cache populated (lazy load from disk)
        if not getattr(self, "_symbols", None):
            try:
                try:
                    from src.tools.tools_config import agent_context_path
                    ctx = agent_context_path(Path(self.workdir))
                except Exception:
                    ctx = Path(self.workdir) / ".codingAgent"
                path = ctx / "vectorstore" / "symbols.json"
                if path.exists():
                    self._symbols = json.loads(path.read_text())
                else:
                    self._symbols = []
            except Exception:
                logger.exception(
                    "Failed to load persisted symbols for VectorStore.search"
                )
                self._symbols = []

        symbols = self._symbols or []
        if not symbols:
            return []

        # --- Semantic search path (sentence-transformers available) ---
        st = _get_st_model()
        if st is not None:
            try:
                import math

                q_vec = self._model.encode(query)[0]
                q_norm = math.sqrt(sum(x * x for x in q_vec)) or 1.0

                scored: List[tuple[float, Dict[str, Any]]] = []
                for sym in symbols:
                    s_vec = sym.get("vector")
                    if not s_vec:
                        # Embed on demand (first time after index_code without vectors)
                        text = " ".join(filter(None, [
                            sym.get("symbol_name") or sym.get("name"),
                            sym.get("file_path"),
                            sym.get("docstring") or sym.get("summary"),
                        ]))
                        s_vec = self._model.encode(text)[0]
                    s_norm = math.sqrt(sum(x * x for x in s_vec)) or 1.0
                    cosine = sum(a * b for a, b in zip(q_vec, s_vec)) / (q_norm * s_norm)
                    scored.append((cosine, sym))
                scored.sort(key=lambda t: t[0], reverse=True)
                results = []
                for _, sym in scored[:limit]:
                    res = dict(sym)
                    res.pop("vector", None)
                    results.append(res)
                logger.debug("VectorStore.search(%r) semantic -> %d results", query, len(results))
                return results
            except Exception as exc:
                logger.debug("VectorStore.search: semantic path failed (%s), falling back", exc)

        # --- Token-based fallback ---
        q = (query or "").lower()
        tokens = [t for t in re.split(r"[\s_\-/\\]+", q) if t]

        results = []
        for sym in symbols:
            name = (sym.get("symbol_name") or sym.get("name") or "").lower()
            file_path = (sym.get("file_path") or "").lower()
            match = not tokens or any(tok in name or tok in file_path for tok in tokens)
            if match:
                res = dict(sym)
                res.pop("vector", None)
                results.append(res)
                if len(results) >= limit:
                    break

        logger.debug("VectorStore.search(%r) token -> %d results", query, len(results))
        return results

    def add_memory(self, text: str, metadata: Dict[str, Any]) -> None:
        logger.debug("VectorStore.add_memory called (no-op in stub)")

    def search_memories(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        logger.debug("VectorStore.search_memories called (returns empty list in stub)")
        return []


__all__ = ["VectorStore"]
