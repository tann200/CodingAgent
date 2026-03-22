import concurrent.futures
import hashlib
import logging
import sys
from pathlib import Path
from typing import List, Dict, Any

import lancedb
import pandas as pd
from lancedb.pydantic import LanceModel, Vector, pydantic_to_schema
from pydantic import Field
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Try importing SentenceTransformer, fall back to a deterministic dummy encoder for tests/environments without heavy deps
try:
    from sentence_transformers import SentenceTransformer

    _HAS_ST_MODEL = True
except Exception:
    _HAS_ST_MODEL = False


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
                    self._model = SentenceTransformer('all-MiniLM-L6-v2')
                except Exception:
                    self._model = _DummyModel()
            else:
                self._model = _DummyModel()
        return self._model

    def _get_or_create_table(self, table_name: str, schema: Any):
        try:
            return self.db.open_table(table_name)
        except (FileNotFoundError, ValueError):
            return self.db.create_table(table_name, schema=schema, mode="overwrite")

    def index_code(self, repo_index: Dict[str, Any]):
        table_name = "code_symbols"
        
        data = []
        for symbol in repo_index["symbols"]:
            docstring = symbol.get("docstring") or "N/A"
            text_to_embed = f"File: {symbol['file_path']}\nType: {symbol['symbol_type']}\nName: {symbol['symbol_name']}\nDocstring: {docstring}"
            # Create a stable hash of the content to be embedded
            content_hash = hashlib.sha256(text_to_embed.encode()).hexdigest()

            data.append({
                "text": text_to_embed,
                "file_path": symbol["file_path"],
                "symbol_name": symbol["symbol_name"],
                "symbol_type": symbol["symbol_type"],
                "start_line": symbol.get("start_line", 0),
                "hash": content_hash,
            })
            
        if not data:
            return
            
        df = pd.DataFrame(data)
        
        embedding_dim = self.model.get_sentence_embedding_dimension()
        if not isinstance(embedding_dim, int):
            raise TypeError("Could not determine sentence embedding dimension.")

        class CodeSymbol(LanceModel):
            text: str = Field(default=None)
            vector: Vector(embedding_dim)
            file_path: str
            symbol_name: str
            symbol_type: str
            start_line: int
            hash: str

        tbl = self._get_or_create_table(table_name, pydantic_to_schema(CodeSymbol))
        
        # Check for existing hashes to avoid re-embedding
        existing_hashes = set()
        try:
            if tbl.count_rows() > 0:
                existing_hashes = set(tbl.to_pandas()['hash'].tolist())
        except Exception:
            existing_hashes = set()

        df_new = df[~df['hash'].isin(existing_hashes)]
        
        if df_new.empty:
            return # Nothing to index

        # Process in batches
        batch_size = 128
        for i in tqdm(range(0, len(df_new), batch_size), desc="Embedding new symbols", disable=not sys.stdout.isatty()):
            batch_df = df_new.iloc[i:i+batch_size].copy()
            
            embeddings = self.model.encode(batch_df['text'].tolist())
            batch_df['vector'] = list(embeddings)
            
            tbl.add(data=batch_df)

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
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                results = _ex.submit(_do_search).result(timeout=10)
        except concurrent.futures.TimeoutError:
            logger.warning("VectorStore.search timed out after 10 s — returning empty results")
            return []
        # Drop the raw embedding column — it's large and causes JSON serialisation failures (NEW-22)
        return results.drop(columns=["vector"], errors="ignore").to_dict("records")

if __name__ == "__main__":
    import json
    workdir = '.'
    
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
        print(f"- {res['symbol_name']} in {res['file_path']} (Score: {res['_distance']:.2f})")
