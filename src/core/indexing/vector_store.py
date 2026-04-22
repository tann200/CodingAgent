import concurrent.futures
import hashlib
import logging
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

import lancedb
import pandas as pd
import pyarrow as pa
from lancedb.pydantic import LanceModel, Vector, pydantic_to_schema
from pydantic import Field
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Try importing SentenceTransformer, fall back to a deterministic dummy encoder for tests/environments without heavy deps
try:
    from sentence_transformers import SentenceTransformer  # type: ignore[import]

    _HAS_ST_MODEL = True
    _SentenceTransformer = SentenceTransformer
except Exception:
    _HAS_ST_MODEL = False
    _SentenceTransformer = None


class _DummyModel:
    def __init__(self):
        self._dim = 8

    def get_sentence_embedding_dimension(self):
        return self._dim

    def encode(self, texts: List[str]):
        # Deterministic pseudo-embedding using SHA256: produce vector of floats in [-1,1]
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vec = []
            for i in range(self._dim):
                # take two bytes per dim
                b1 = h[(i * 2) % len(h)]
                b2 = h[(i * 2 + 1) % len(h)]
                v = ((b1 << 8) + b2) / 65535.0
                # map to -1..1
                vec.append((v * 2.0) - 1.0)
            out.append(vec)
        return out


class VectorStore:
    def __init__(self, workdir: str):
        self.workdir = Path(workdir)
        self.db_path = self.workdir / ".agent-context" / "lancedb"
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        # Lazy-load SentenceTransformer on first use (#34 fix — avoids ~2s startup penalty)
        self._model: Any = None

    @property
    def model(self) -> Any:
        if self._model is None:
            if _HAS_ST_MODEL:
                try:
                    self._model = _SentenceTransformer("all-MiniLM-L6-v2")  # type: ignore[misc]
                except Exception:
                    self._model = _DummyModel()
            else:
                self._model = _DummyModel()
        return self._model

    def _get_or_create_table(self, table_name: str, schema: Any):
        """Open an existing table or create it.

        If an existing table is found but the schema is incompatible in a way that
        will cause Arrow casting errors (for example an existing scalar column where
        the desired schema expects a list/vector column), attempt to recreate the
        table using mode="overwrite" so we avoid crashes from mismatched schemas.
        """
        try:
            tbl = self.db.open_table(table_name)
        except (FileNotFoundError, ValueError):
            return self.db.create_table(table_name, schema=schema, mode="overwrite")

        # If we have an explicit pyarrow schema for the desired layout, and the
        # existing table has a conflicting scalar vs list/vector typing for the
        # "vector" column, prefer to recreate the table. This handles cases where
        # older runs created a table with a different vector shape.
        try:
            desired_schema = schema
            if hasattr(schema, "names"):
                desired_schema = schema
            else:
                # pydantic_to_schema may sometimes return a non-pa.Schema; leave it
                # to the lower-level create_table call in that case.
                desired_schema = None

            if desired_schema is not None and "vector" in desired_schema.names:
                existing_schema = tbl.schema

                def _is_list_type(t: pa.DataType) -> bool:
                    return (
                        pa.types.is_list(t)
                        or pa.types.is_fixed_size_list(t)
                        or pa.types.is_large_list(t)
                    )

                try:
                    existing_field = existing_schema.field("vector")
                    desired_field = desired_schema.field("vector")
                    existing_is_list = _is_list_type(existing_field.type)
                    desired_is_list = _is_list_type(desired_field.type)
                    # If one is a scalar and the other is list-like, recreate table
                    if existing_is_list != desired_is_list:
                        logger.warning(
                            "Recreating LanceDB table '%s' due to incompatible 'vector' column type",
                            table_name,
                        )
                        return self.db.create_table(
                            table_name, schema=schema, mode="overwrite"
                        )
                except Exception:
                    # If introspection fails, fall back to returning the opened table
                    return tbl
        except Exception:
            # any unexpected error: return opened table to avoid blocking startup
            return tbl

        return tbl

    def index_code(self, repo_index: Dict[str, Any]):
        table_name = "code_symbols"

        data = []
        for symbol in repo_index["symbols"]:
            docstring = symbol.get("docstring") or "N/A"
            text_to_embed = f"File: {symbol['file_path']}\nType: {symbol['symbol_type']}\nName: {symbol['symbol_name']}\nDocstring: {docstring}"
            # Create a stable hash of the content to be embedded
            content_hash = hashlib.sha256(text_to_embed.encode()).hexdigest()

            data.append(
                {
                    "text": text_to_embed,
                    "file_path": symbol["file_path"],
                    "symbol_name": symbol["symbol_name"],
                    "symbol_type": symbol["symbol_type"],
                    "start_line": symbol.get("start_line", 0),
                    "hash": content_hash,
                }
            )

        if not data:
            return

        df = pd.DataFrame(data)

        embedding_dim = self.model.get_sentence_embedding_dimension()
        if not isinstance(embedding_dim, int):
            raise TypeError("Could not determine sentence embedding dimension.")

        class CodeSymbol(LanceModel):
            text: Optional[str] = Field(default=None)
            vector: Vector(embedding_dim)  # type: ignore[valid-type]
            file_path: str
            symbol_name: str
            symbol_type: str
            start_line: int
            hash: str

        desired_schema = pydantic_to_schema(CodeSymbol)
        tbl = self._get_or_create_table(table_name, desired_schema)

        # Check for existing hashes to avoid re-embedding
        existing_hashes = set()
        try:
            if tbl.count_rows() > 0:
                existing_hashes = set(tbl.to_pandas()["hash"].tolist())
        except Exception:
            existing_hashes = set()

        df_new = df[~df["hash"].isin(list(existing_hashes))]

        if df_new.empty:
            return  # Nothing to index

        # Process in batches
        batch_size = 128
        for i in tqdm(
            range(0, len(df_new), batch_size),
            desc="Embedding new symbols",
            disable=not sys.stdout.isatty(),
        ):
            batch_df = df_new.iloc[i : i + batch_size].copy()

            embeddings = self.model.encode(batch_df["text"].tolist())
            batch_df["vector"] = list(embeddings)

            # Try adding and recreate the table if ArrowInvalid occurs.
            new_tbl = self._add_with_recreate(table_name, tbl, desired_schema, batch_df)
            if new_tbl is None:
                # failed to add even after recreate; skip this batch
                continue
            tbl = new_tbl

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        table_name = "code_symbols"
        try:
            tbl = self.db.open_table(table_name)
        except (FileNotFoundError, ValueError):
            return []

        query_vector = self.model.encode(query)
        # SentenceTransformer.encode returns 2D array for a list input; flatten to 1D for LanceDB
        if hasattr(query_vector, "ndim") and query_vector.ndim > 1:
            query_vector = query_vector.flatten()
        # Also handle numpy array -> list conversion for LanceDB compatibility
        if hasattr(query_vector, "tolist"):
            query_vector = query_vector.tolist()

        # Run the blocking LanceDB call in a thread so we can enforce a timeout (NEW-26).
        # On a large index or slow disk the query can block analysis_node indefinitely.
        def _do_search() -> pd.DataFrame:
            return tbl.search(query_vector).limit(limit).to_pandas()

        try:
            import contextvars as _cv

            _ctx = _cv.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                results = _ex.submit(_ctx.run, _do_search).result(timeout=10)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "VectorStore.search timed out after 10 s — returning empty results"
            )
            return []
        # Drop the raw embedding column — it's large and causes JSON serialisation failures (NEW-22)
        return results.drop(columns=["vector"], errors="ignore").to_dict("records")

    def add_memory(self, text: str, metadata: Dict[str, Any]):
        """Add a memory entry (e.g., session summary) to the vector store.

        Args:
            text: The text content to embed and store
            metadata: Additional metadata (e.g., {"type": "session", "session_id": "..."})
        """
        table_name = "memories"

        embedding_dim = self.model.get_sentence_embedding_dimension()
        if not isinstance(embedding_dim, int):
            embedding_dim = 8  # Fallback for dummy model

        # Use a flexible vector type for compatibility
        class MemoryEntry(LanceModel):
            text: Optional[str] = Field(default=None)
            vector: List[float] = Field(default_factory=lambda: [0.0] * embedding_dim)
            type: str
            session_id: str

        try:
            desired_schema = pydantic_to_schema(MemoryEntry)
            tbl = self._get_or_create_table(table_name, desired_schema)
        except Exception:
            return

        # Create embedding
        embedding = self.model.encode(text)
        if hasattr(embedding, "ndim") and embedding.ndim > 1:
            embedding = embedding.flatten()
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()

        # Add to table
        data = [
            {
                "text": text,
                "vector": embedding,
                "type": metadata.get("type", "unknown"),
                "session_id": metadata.get("session_id", "unknown"),
            }
        ]

        # Try adding and recreate the table if ArrowInvalid occurs.
        new_tbl = self._add_with_recreate(table_name, tbl, desired_schema, data)
        if new_tbl is None:
            logger.error("Failed to add memory after attempting to recreate the table.")
            return
        logger.info(f"Added memory to vector store: {metadata.get('session_id')}")

    def _add_with_recreate(
        self, table_name: str, tbl: Any, schema: Any, data
    ) -> Optional[Any]:
        """Attempt to tbl.add(data); on ArrowInvalid, recreate the table and retry once.

        Returns the table used for the successful add, or None if the add ultimately failed.
        """
        try:
            tbl.add(data=data)
            return tbl
        except pa.ArrowInvalid as e:
            logger.warning(
                "ArrowInvalid when adding to LanceDB table '%s': %s. Attempting to recreate the table and retry.",
                table_name,
                e,
            )
            try:
                new_tbl = self.db.create_table(
                    table_name, schema=schema, mode="overwrite"
                )
                new_tbl.add(data=data)
                logger.info(
                    "Recreated LanceDB table '%s' and retried add successfully.",
                    table_name,
                )
                return new_tbl
            except Exception as e2:
                logger.error(
                    "Failed to recreate or add to recreated LanceDB table '%s': %s",
                    table_name,
                    e2,
                )
                return None
        except Exception as e:
            # Non-Arrow error — log and do not attempt risky recreation.
            logger.error(
                "Unexpected error when adding to LanceDB table '%s': %s", table_name, e
            )
            return None

    def search_memories(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search memories (e.g., session summaries) in the vector store."""
        table_name = "memories"
        try:
            tbl = self.db.open_table(table_name)
        except (FileNotFoundError, ValueError):
            return []

        query_vector = self.model.encode(query)
        if hasattr(query_vector, "ndim") and query_vector.ndim > 1:
            query_vector = query_vector.flatten()
        if hasattr(query_vector, "tolist"):
            query_vector = query_vector.tolist()

        def _do_search() -> pd.DataFrame:
            return tbl.search(query_vector).limit(limit).to_pandas()

        try:
            import contextvars as _cv

            _ctx = _cv.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                results = _ex.submit(_ctx.run, _do_search).result(timeout=10)
        except concurrent.futures.TimeoutError:
            return []

        return results.drop(columns=["vector"], errors="ignore").to_dict("records")


if __name__ == "__main__":
    import json

    workdir = "."

    index_path = Path(workdir) / ".agent-context" / "repo_index.json"
    if not index_path.exists():
        from repo_indexer import index_repository

        print("Generating repo index...")
        index_repository(workdir)

    with open(index_path, "r") as f:
        repo_index = json.load(f)

    vs = VectorStore(workdir)
    print("Indexing code symbols into LanceDB...")
    vs.index_code(repo_index)

    print("\nSearching for 'read file':")
    search_results = vs.search("read file")
    for res in search_results:
        print(
            f"- {res['symbol_name']} in {res['file_path']} (Score: {res['_distance']:.2f})"
        )
