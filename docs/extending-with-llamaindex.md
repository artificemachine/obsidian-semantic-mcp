# Extending obsidian-semantic-mcp Beyond Markdown with LlamaIndex

*Date: 2026-05-18*

> **Heads-up:** This doc proposed LlamaIndex as the right tool to extend the vault MCP to non-markdown content. For **attachment indexing specifically**, the user ultimately built a separate Rust project (`tessera-mcp`) using `rmcp + LanceDB` instead of extending obsidian-semantic-mcp with LlamaIndex. See `~/DevOpsSec/tessera-mcp/docs/pivot-to-rust.md`.
>
> The advice in this doc is still valid **if** you ever want to extend obsidian-semantic-mcp itself (the markdown-side MCP) to handle additional content types in-process. For the attachments use case, tessera-mcp is the answer.

---

---

## Short answer

**Yes — LlamaIndex is the right way to extend obsidian-semantic-mcp to handle more than `.md` files.**

The reason it is currently markdown-only is that the indexing pipeline knows how to read markdown text and nothing else. LlamaIndex provides ready-made loaders for ~150 other file types, so the extension is mostly "swap the loader, keep the rest".

---

## What you could index in addition to `.md`

Your vault probably contains files the MCP server cannot see today:

| File type | Loader | What you get |
|---|---|---|
| **PDFs** | `PyMuPDFReader`, `PDFReader` | Text from research papers, contracts, bank statements, manuals |
| **Images** | `ImageReader` + vision model | OCR text + visual descriptions of screenshots, diagrams, photos |
| **Audio (voice memos)** | `AudioTranscriber` (Whisper) | Transcripts of `.m4a`, `.mp3`, `.wav` recordings |
| **HTML clippings** | `BeautifulSoupWebReader`, `HTMLTagReader` | Article text from web archives |
| **Code files** | `CodeSplitter`, language-aware loaders | Function-level chunks from `.py`, `.ts`, `.rs`, etc. |
| **CSV / Excel** | `PandasCSVReader`, `PandasExcelReader` | Row/column data searchable as text |
| **JSON** | `JSONReader` | Structured config or export data |
| **PowerPoint / Word** | `UnstructuredReader` | Slide and doc text |
| **Email exports** | `MboxReader`, `OutlookEmailReader` | Searchable email threads |
| **EPUB / Kindle** | `EpubReader` | Book content |

In your vault you have at minimum: PDFs (Obsidian PDF attachments), images (screenshots from yt-intel and other workflows), and likely some HTML clippings. All three become searchable.

---

## Three ways to do this

### Option A — Extend obsidian-semantic-mcp directly

Modify the indexing pipeline in obsidian-semantic-mcp itself to dispatch to different LlamaIndex loaders based on file extension.

**Pros:** one MCP server, one index, unified `search_vault`
**Cons:** you maintain a fork, upstream updates need merging

### Option B — Build a sibling MCP server

Create `obsidian-attachments-mcp` (new project) that handles non-markdown only, alongside the existing obsidian-semantic-mcp.

**Pros:** zero changes to upstream, easier to maintain
**Cons:** two tools to call, Claude has to know which to use for what

### Option C — Hybrid: extend with a plugin system

Refactor obsidian-semantic-mcp so file-type loaders are pluggable. Drop in a LlamaIndex-based plugin for non-markdown.

**Pros:** clean separation, contributable upstream
**Cons:** real refactor work

**Recommendation:** Option A for fastest results, Option C if you want it merged upstream.

---

## Concrete implementation sketch (Option A)

The current indexing logic probably looks something like this (conceptually):

```python
# current state — markdown only
for path in vault.glob("**/*.md"):
    text = path.read_text()
    chunks = chunk_markdown(text)
    embed_and_store(chunks)
```

With LlamaIndex it becomes:

```python
from llama_index.core import SimpleDirectoryReader
from llama_index.readers.file import (
    PyMuPDFReader,        # PDFs
    ImageReader,          # images (with vision model for OCR + description)
    UnstructuredReader,   # generic fallback
)

# extension → loader
file_extractor = {
    ".pdf": PyMuPDFReader(),
    ".png": ImageReader(),
    ".jpg": ImageReader(),
    ".jpeg": ImageReader(),
    ".html": UnstructuredReader(),
    ".csv": UnstructuredReader(),
    ".epub": UnstructuredReader(),
    # .md stays on the existing markdown chunker
}

# load everything in one shot
docs = SimpleDirectoryReader(
    input_dir=str(vault_path),
    recursive=True,
    file_extractor=file_extractor,
    required_exts=[".md", ".pdf", ".png", ".jpg", ".html", ".csv", ".epub"],
).load_data()

# rest of the pipeline (embed + store) stays the same
for doc in docs:
    embed_and_store(doc.text, metadata=doc.metadata)
```

That is the entire change in principle. The hard parts are:
1. **Picking the right chunking strategy per type** (PDFs need different chunking than images)
2. **Handling vision/audio costs** (calling Whisper or GPT-4V for every image is not free)
3. **Re-indexing strategy** (skip re-processing unchanged PDFs)
4. **Privacy** (if using cloud vision/transcription, your vault content leaves the box — use local Ollama vision + local Whisper to stay private)

---

## Privacy-first variant (your style)

Stay 100% local:

| Need | Local replacement |
|---|---|
| Embedding | `nomic-embed-text` via Ollama (you already use this in trayzury) |
| PDF text extraction | PyMuPDF (no LLM needed) |
| Image OCR + description | `llama3.2-vision:11b` via Ollama (you already use this in trayzury) |
| Audio transcription | `whisper.cpp` or `faster-whisper` locally |
| HTML parsing | BeautifulSoup (no LLM needed) |
| Vector store | ChromaDB or sqlite-vec (you already use these) |

Result: vault still never leaves your machine. Just becomes a much richer knowledge base.

---

## What this would unlock in practice

Today: "what did I write about X" → only finds your `.md` notes.

After extension:
- "What does the bank statement from March say about the recurring charge?" → finds it in the PDF attachment
- "Show me the diagram I screenshotted from that paper" → finds it in the image with OCR
- "What did I record in that voice memo about Ozarys?" → finds it in the transcribed audio
- "Find every CSV that mentions wallet 0xabc..." → finds it across all CSV attachments

Your vault becomes a true second-brain knowledge base, not just a markdown library.

---

## Effort estimate

Realistic build time for Option A:

| Task | Time |
|---|---|
| Add LlamaIndex dependency, hook up `SimpleDirectoryReader` | 1-2 hours |
| Wire PDF + image + HTML loaders | 2-3 hours |
| Set up local Whisper + Ollama vision for privacy | 2-4 hours |
| Update reindex_vault to handle attachments | 1-2 hours |
| Test on real vault, tune chunking | 3-5 hours |

**Total: ~1-2 focused days for a working v1.**

---

## Should you do this?

Probably yes, eventually. The cost is low (1-2 days), the unlock is high (your entire vault including attachments becomes searchable), and you already have the local AI stack to keep it private.

The one warning: re-indexing time grows fast with non-text content. A vault with 100 PDFs + 500 images could take 30-60 minutes to fully re-index the first time, because every image goes through a vision model. After the first index, only changed files re-process.

---

## TL;DR

| Question | Answer |
|---|---|
| Can LlamaIndex extend obsidian-semantic-mcp to non-md? | Yes, directly. |
| What new file types could I search? | PDFs, images, audio, HTML, CSV, code, EPUB |
| Can I stay 100% local? | Yes — use Ollama for embedding/vision, local Whisper for audio |
| How hard? | 1-2 days for a working version |
| Recommended approach? | Extend obsidian-semantic-mcp directly using `SimpleDirectoryReader` + `file_extractor` |
