"""
Lightweight VectorStore stub

This module intentionally provides a minimal, dependency-free VectorStore API so
the rest of the codebase can opt into semantic-search features when available.
It deliberately does NOT depend on external vector DBs or heavy ML stacks
(pyarrow, pandas, pydantic, sentence-transformers). The implementation is a
safe no-op / in-memory stub.
"""

from __future__ import annotations

from typing import Any, Dict, List
import hashlib
import logging
import json
import re
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)


class _DummyModel:
    """Deterministic lightweight embedder used when a real model is unavailable.

    It produces small fixed-size vectors derived from a SHA256 digest so tests
    and simple in-memory usages behave deterministically without heavy deps.
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
            h = hashlib.sha256(str(t).encode()).digest()
            vec: List[float] = []
            for i in range(self._dim):
                b1 = h[(i * 2) % len(h)]
                b2 = h[(i * 2 + 1) % len(h)]
                v = ((b1 << 8) + b2) / 65535.0
                vec.append((v * 2.0) - 1.0)
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
        base = Path(self.workdir) / ".agent-context" / "vectorstore"
        base.mkdir(parents=True, exist_ok=True)

        symbols = repo_index.get("symbols", []) if isinstance(repo_index, dict) else []
        try:
            symbols_path = base / "symbols.json"
            symbols_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug(
                    "VectorStore: attempting atomic_write_json for %s", symbols_path
                )
                ok = atomic_write_json(symbols_path, symbols, logger=logger)
                if ok:
                    logger.debug(
                        "VectorStore: atomic_write_json succeeded for %s", symbols_path
                    )
                else:
                    logger.warning(
                        "VectorStore: atomic_write_json returned False for %s; falling back to write_text",
                        symbols_path,
                    )
                    try:
                        symbols_path.write_text(
                            json.dumps(symbols, indent=2), encoding="utf-8"
                        )
                    except Exception:
                        logger.exception(
                            "VectorStore: failed to write symbols.json fallback to %s",
                            symbols_path,
                        )
            except Exception:
                logger.debug(
                    "VectorStore: atomic_write_json unavailable or failed for %s; falling back\n%s",
                    symbols_path,
                    traceback.format_exc(),
                )
                try:
                    symbols_path.write_text(
                        json.dumps(symbols, indent=2), encoding="utf-8"
                    )
                except Exception:
                    logger.exception(
                        "VectorStore: failed to write symbols.json to %s", symbols_path
                    )
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
