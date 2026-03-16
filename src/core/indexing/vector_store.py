import lancedb
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any
from pydantic import Field
from lancedb.pydantic import LanceModel, vector, pydantic_to_schema
from tqdm import tqdm

import hashlib

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
        if _HAS_ST_MODEL:
            try:
                self.model = SentenceTransformer('all-MiniLM-L6-v2')
            except Exception:
                # fallback if loading model fails
                self.model = _DummyModel()
        else:
            self.model = _DummyModel()

    def _get_or_create_table(self, table_name: str, schema: Any):
        try:
            return self.db.open_table(table_name)
        except (FileNotFoundError, ValueError):
            return self.db.create_table(table_name, schema=schema, mode="overwrite")

    def index_code(self, repo_index: Dict[str, Any]):
        table_name = "code_symbols"
        
        data = []
        for symbol in repo_index["symbols"]:
            text_to_embed = f"File: {symbol['file_path']}\nType: {symbol['symbol_type']}\nName: {symbol['symbol_name']}\nDocstring: {symbol['docstring'] or 'N/A'}"
            # Create a stable hash of the content to be embedded
            content_hash = hashlib.sha256(text_to_embed.encode()).hexdigest()
            
            data.append({
                "text": text_to_embed,
                "file_path": symbol["file_path"],
                "symbol_name": symbol["symbol_name"],
                "symbol_type": symbol["symbol_type"],
                "start_line": symbol["start_line"],
                "hash": content_hash
            })
            
        if not data:
            return
            
        df = pd.DataFrame(data)
        
        embedding_dim = self.model.get_sentence_embedding_dimension()
        if not isinstance(embedding_dim, int):
            raise TypeError("Could not determine sentence embedding dimension.")

        class CodeSymbol(LanceModel):
            text: str = Field(default=None)
            vector: vector(embedding_dim)
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
        for i in tqdm(range(0, len(df_new), batch_size), desc="Embedding new symbols"):
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
        results = tbl.search(query_vector).limit(limit).to_df()
        return results.to_dict("records")

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
