# RAG Explained Via obsidian-semantic-mcp

*Date: 2026-05-18*

---

## Short answer

**Yes — obsidian-semantic-mcp IS basically RAG for your Obsidian vault, exposed through the MCP protocol.**

You have been using RAG every day without calling it that.

---

## The mapping

Every RAG system has the same five parts. obsidian-semantic-mcp has all of them:

| RAG concept | obsidian-semantic-mcp equivalent |
|---|---|
| **Documents** | Your `.md` notes in the vault |
| **Chunking** | The server splits notes into searchable pieces (paragraphs, sections, blocks) |
| **Embedding** | Each chunk gets converted to a vector for semantic similarity |
| **Vector store** | An index file the server maintains (you have seen `reindex_vault` rebuild this) |
| **Retrieval** | `search_vault` returns the chunks most relevant to your query |
| **Generation** | Claude reads those retrieved chunks and answers your question |

The `search_vault` tool you call all the time? That is the **R** in RAG.
The fact that Claude then uses those notes to answer? That is the **AG** in RAG.

---

## Concrete example you have already done

You ask Claude: *"What did I decide last week about the Ozarys A2A protocol?"*

What actually happens:

```
1. Claude calls search_vault("Ozarys A2A protocol decision")
                ↓
2. obsidian-semantic-mcp converts that query to a vector
                ↓
3. It searches your vault index for chunks with similar vectors
                ↓
4. It returns the top N matching note chunks
                ↓
5. Claude reads those chunks and answers your question
```

That is the textbook RAG flow. The MCP server is the retrieval layer. Claude is the generation layer.

---

## Where the comparison ends

obsidian-semantic-mcp is a **purpose-built RAG service** for one specific corpus (your vault) exposed through one specific protocol (MCP).

LlamaIndex is a **general-purpose RAG library** that lets you build something like obsidian-semantic-mcp for any corpus (PDFs, Confluence pages, contracts, code, anything).

| | obsidian-semantic-mcp | LlamaIndex |
|---|---|---|
| What it indexes | Just your Obsidian vault | Anything you point it at (PDFs, web, DBs, code, ...) |
| How you use it | MCP server, Claude calls it natively | Python library, you import it |
| Customization | Limited — it does what the author built | Full control — you choose embedding model, chunking strategy, vector store, retrieval algorithm |
| Setup cost | Zero — `claude mcp add` and go | Real work — you build the indexing pipeline |
| Best for | Querying your existing vault | Building a new RAG system from scratch |

---

## So when would you need LlamaIndex?

Only when obsidian-semantic-mcp is not enough. Examples:

- You want to RAG over **non-Obsidian** content — PDFs of bank statements, contract files, the trayzury invoice corpus
- You want to RAG over **a website** (scraped pages)
- You want to RAG over **a codebase** with code-aware chunking
- You want to RAG over **mixed sources** (vault + PDFs + JSON exports)
- You want to use a **specific embedding model** (e.g. local nomic-embed-text via Ollama for privacy)
- You want **custom retrieval logic** (e.g. boost recent docs, filter by tag)

In those cases, you would build it with LlamaIndex the same way someone built obsidian-semantic-mcp for the vault.

---

## How this connects to MAF

Three layers, three jobs:

```
┌───────────────────────────────────────────────────────────┐
│  MAF                                                      │
│  Agent orchestration — workflow, memory, routing          │
│  "Do step A, then B, then C; remember across runs"        │
└───────────────────────────────────────────────────────────┘
                           │
                           │ calls tools
                           ▼
┌───────────────────────────────────────────────────────────┐
│  LlamaIndex (or obsidian-semantic-mcp)                    │
│  Retrieval — "find the relevant chunks for this query"    │
└───────────────────────────────────────────────────────────┘
                           │
                           │ reads from
                           ▼
┌───────────────────────────────────────────────────────────┐
│  Your data                                                │
│  Vault notes, PDFs, code, contracts                       │
└───────────────────────────────────────────────────────────┘
```

In a real MAF agent, the RAG layer becomes a **tool** the agent calls:

```python
from agent_framework import ChatAgent
from agent_framework.anthropic import AnthropicChatClient

def search_vault(query: str) -> str:
    """Search the Obsidian vault for relevant notes."""
    # could call obsidian-semantic-mcp here, or use LlamaIndex
    return retrieved_chunks

agent = ChatAgent(
    instructions="Answer using the vault as ground truth.",
    tools=[search_vault],
    chat_client=AnthropicChatClient(model="claude-sonnet-4-6"),
)
```

The agent decides **when** to search. The RAG layer handles **how** to search. MAF handles the orchestration around both.

---

## TL;DR

| Question | Answer |
|---|---|
| Is obsidian-semantic-mcp doing RAG? | Yes — it is RAG-as-a-service for your vault |
| Do I need LlamaIndex if I already have obsidian-semantic-mcp? | Only if you need to RAG over non-vault content |
| Where does MAF fit? | MAF orchestrates the agent that calls the RAG layer as a tool |
| Have I been doing RAG all along? | Yes — every `search_vault` call is the R in RAG |
