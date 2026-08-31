# Design Spec — Item 1.4: VectorStore Episodic Memory Persistence

## Problem

`VectorStore.add_memory()` (`vector_store.py:302-303`) and `search_memories()` (`vector_store.py:305-307`) are no-op stubs. Two production callers depend on them:

- **Write:** `distiller.py:629` — `_vs.add_memory(_summary_text, metadata=distilled_state)` after each session distillation. Logs "summary persisted to VectorStore" but stores nothing.
- **Read:** `context_builder.py:551` — `_vs.search_memories(query=task, limit=limit)` inside `inject_prior_session_memories` (round 0, `perception_node`). Expects each result to expose `text`/`content` (line 553).

Result: cross-session memory recall is silently non-functional, despite the system prompt claiming `<prior_context>` enrichment.

## Confirmed Constraints (from code)

1. `VectorStore` is intentionally dependency-free: it already persists to disk via `agent_context_path(Path(workdir))/vectorstore/` (used by `index_code`, `vector_store.py:178`). No external vector DBs.
2. A real embedder already exists (`_DummyModel.encode`, `vector_store.py:136-151`) which uses `sentence-transformers` when available and a deterministic SHA-256 fallback otherwise. Reuse it — do not build a new embedding path.
3. `search_memories` result records must contain a `text` or `content` field (consumed at `context_builder.py:553`).
4. `metadata` passed to `add_memory` is a dict containing `current_task`, `current_state`, `next_step`, `_compacted_history`, and other `distilled_state` fields.

## Decisions (this spec fixes the 5 open questions)

### D1 — Storage backend: JSONL file (not SQLite, not session store)
Persist episodic memories to a single JSONL file at `agent_context_path(workdir)/vectorstore/memories.jsonl`.
- **Why JSONL:** matches the repo's existing JSONL convention (`jsonl_session_store.py`), append-friendly, dependency-free, human-inspectable, and trivially crash-safe (each line self-contained).
- **Why not SQLite:** the SymbolGraph/code search already owns the SQLite path; memories are append-only episodic records, not relational.
- **Why not the session store:** memories must persist *across* sessions (that is the entire point) and must be queryable by semantic similarity.

### D2 — Memory record schema
Each JSONL line (JSON object):
```
{
  "id": "<sha256 of text+ts>",
  "text": "<summary text (field consumed by search caller)>",
  "content": "<same as text, for the context_builder consumer fallback>",
  "metadata": { ...distilled_state subset... },
  "created_at": <ISO8601 string>
}
```
- Both `text` and `content` are set to satisfy `context_builder.py:553` (`r.get("text") or r.get("content")`).
- `id` is a dedup key: `add_memory` skips a record whose `id` already exists (idempotency / memory-rot prevention).

### D3 — Embedding strategy
Embed the `text` field via `self._model.encode([text])[0]` (the existing `_DummyModel`).
- When `sentence-transformers` is installed → genuine semantic vectors.
- Otherwise → deterministic SHA-256 stub vectors (still allows cosine similarity + token-overlap fallback).
- Store the vector **inline in the JSONL record** (field `"vector"`) so `search_memories` does not need an external index and is deterministic on re-load. Cap memory/disk cost: embeddings are 384-dim real or 8-dim stub.

### D4 — Search semantics (search_memories)
1. Load all records from `memories.jsonl` (bounded: cap at e.g. 500 most-recent records to bound latency; older dropped from search but retained on disk).
2. If records carry `vector` and a real embedder is present → cosine similarity of the query against stored vectors, return top-`limit`.
3. Fallback → token overlap on `text` (mirroring the existing `search()` token path, `vector_store.py:283-300`).
4. Return records as dicts (vector stripped), matching the existing `search()` contract (`res.pop("vector", None)`).

### D5 — Scope / lifecycle
- **Scope:** per-workspace directory (same `.agent-context/vectorstore/` as code symbols). Memories are project-scoped.
- **Eviction/rot prevention:** on `add_memory`, if the file exceeds a max record count (default 200), drop oldest records (keep newest). This prevents unbounded growth and stale-memory reuse.
- **No automatic deletion** of individual memories; rotation handles bound.

## Edge Cases
- `memories.jsonl` missing/corrupt → `search_memories` returns `[]`; `add_memory` recreates the file (append creates dir).
- Directory creation failure → `add_memory` is a no-op with a warning (matches `index_code` graceful-degradation convention, `vector_store.py:192-193`).
- Concurrent writers → use `atomic_write_json`-style guarded append (single-writer assumption acceptable; distiller runs once per session end / memory_sync).
- `sentence-transformers` load failure at search time → fall to token-overlap path (already the pattern in `_DummyModel.encode`).

## File Changes
- `src/core/indexing/vector_store.py`: replace `add_memory`/`search_memories` stubs with real implementations + private `_load_memories`/`_append_memory`/`_rotate_memories` helpers.
- No changes needed at callers (`distiller.py`, `context_builder.py`) — existing call signatures are honored.

## Impact
- Restores cross-session semantic recall used by `inject_prior_session_memories` (round-0 perception) to actually surface prior-session context.
- Makes the distiller's "summary persisted to VectorStore" log truthful.

## Test Plan
- Unit: `add_memory` creates file with correct schema; `search_memories` returns that record with `text`/`content`.
- Unit: dedup (same `id` not re-appended); rotation cap drops oldest.
- Unit: corrupt/missing file → `[]` without raising.
- Unit: token-overlap fallback with real-embedder forced unavailable.
