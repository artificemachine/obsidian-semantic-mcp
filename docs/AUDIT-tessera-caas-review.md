# Tessera CaaS Review

## Verdict

`tessera-mcp` is a useful attachment-ingestion component, but it should not become the CaaS query core. Keep its loaders and make Tessera push normalized documents into a separate tenant-aware CaaS service.

## Key findings

- **High:** Image indexing calls Tokio `Handle::block_on` from async indexing paths, which can panic at runtime. [`image.rs`](../../tessera-mcp/src/indexer/loaders/image.rs#L37)
- **High:** Re-indexing deletes live chunks before replacement is safely committed. MCP indexing also suppresses database errors, records manifest success, and may permanently skip an incomplete document. [`pipeline.rs`](../../tessera-mcp/src/indexer/pipeline.rs#L121), [`server.rs`](../../tessera-mcp/src/server.rs#L224)
- **Medium:** `TESSERA_VISION_MODEL`, `TESSERA_WHISPER_MODEL`, and the configured Ollama host are ignored by loader dispatch; hardcoded defaults are used. [`mod.rs`](../../tessera-mcp/src/indexer/loaders/mod.rs#L35)
- **Medium:** Embedding model and dimension are not stored or validated. Changing models silently compares incompatible vectors using truncated `zip()` calculations. [`vector.rs`](../../tessera-mcp/src/store/vector.rs#L145)
- **Medium:** Full-document retrieval orders chunks by hashed ID rather than page/segment ordinal, so content can be returned scrambled. [`vector.rs`](../../tessera-mcp/src/store/vector.rs#L243)
- **Low:** Incremental indexing uses only mtime despite the plan promising a content hash gate. [`manifest.rs`](../../tessera-mcp/src/indexer/manifest.rs#L43)
- **Low:** Filtered searches leak dynamically constructed SQL strings through `Box::leak`. [`vector.rs`](../../tessera-mcp/src/store/vector.rs#L116)

## Recommended architecture

Extract Tessera's `Loader` implementations into an ingestion adapter that calls CaaS `POST /v1/documents`. Let CaaS own Postgres/pgvector, tenant isolation, transactional swaps, hybrid retrieval, reranking, token packing, caching, and traces.

This was a static review; no changes were made to `tessera-mcp`, and no tests were run because project policy requires prior approval.
