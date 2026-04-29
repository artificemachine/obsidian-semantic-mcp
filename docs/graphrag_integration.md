# GraphRAG Integration — Research & Scoping

## Why Consider GraphRAG?

Semantic search (vector similarity) covers the most common vault query: "find notes about X."
It fails at a specific and high-value case: **discovering connections you missed when writing.**

When knowledge is built over months in isolated sessions, conceptually related notes never get
linked because you weren't thinking about both at the same time. Vector search won't bridge
this reliably — the surface text is too different. A graph connecting shared entities will.

Example: a note on "distributed systems fault tolerance" and a note on "biological immune system
resilience" share the entity "resilience patterns" but are semantically distant in text.
GraphRAG extracts that shared entity and surfaces the connection.

---

## Two Integration Paths

### Path A — Wikilink Graph Augmentation (recommended first step)

Use Obsidian's existing `[[wikilinks]]` as the graph. After semantic search returns top-K notes,
expand results by traversing their link adjacency. No LLM entity extraction, no heavy indexing.

**What it unlocks:**
- "What notes are linked to this result that I never searched for?"
- Multi-hop traversal: A → B → C where B is the bridge you never thought to query
- Near-zero indexing cost — parse markdown, build adjacency map, done

**Complexity:** Low. Surgical modification to the retrieval step only.

### Path B — Full GraphRAG (LLM entity extraction)

Run LLM-based entity/relationship extraction during indexing. Build a proper knowledge graph
with typed edges. Use community detection and summary generation for global thematic queries.

**What it unlocks:**
- Implicit connections not captured by any wikilink
- "What are the main themes across my entire vault?"
- Highest-confidence missed-connection discovery

**Complexity:** High. Near-rewrite of the indexing pipeline. Significant LLM cost at index time.

---

## Recommendation

**Start with Path A.** It uses your own curated link structure — which is already more accurate
than LLM-extracted entities — and costs almost nothing to build. Validate it surfaces enough
missed connections before investing in Path B.

If Path A misses connections that only exist implicitly (no wikilinks between related notes),
layer Path B entity extraction on top.

---

## Honest Caveats

- Path B quality depends on writing consistency. Dense, well-written notes produce good entities.
  Rough scratch notes produce noisy, low-confidence edges.
- Path B adds LLM calls at index time — cost scales with vault size and re-index frequency.
- Neither path replaces semantic search. Both augment it at the retrieval expansion step.

---

## Next Step

Scope out Path A implementation: see `graphrag_path_a_scope.md` (to be written after this doc).
