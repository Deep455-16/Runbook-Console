# 🧠 Master Prompt: AI-Powered PDF Chatbot (Local RAG System)

> **Use this document as a build brief.** Paste it into an AI coding assistant (Claude Code, Cursor, etc.) or hand it to your hackathon team as the single source of truth for scope, architecture, and deliverables.

---

## 1. Role & Objective

You are acting as a **full-stack AI engineer** building a **100% offline / local, AI-powered PDF Chatbot**. The system must let a user upload one or more PDFs, then ask natural-language questions about their content and receive accurate, **cited, context-grounded answers** — powered entirely by locally-hosted open-source LLMs (no external API calls, no cloud inference).

The system is a **Retrieval-Augmented Generation (RAG)** application: it does not rely on an LLM's memorized knowledge — it retrieves relevant chunks from the uploaded PDF(s) and grounds every answer in that retrieved context.

---

## 2. Problem Statement

Users often need quick, specific answers buried inside long PDFs (manuals, research papers, contracts, runbooks, textbooks). Manually searching (Ctrl+F) is slow and misses paraphrased or conceptually-related content. This project builds a chatbot that:

- Ingests any PDF (text-based or scanned/image-based via OCR)
- Breaks it into semantically meaningful chunks
- Embeds and indexes those chunks in a local vector store
- Retrieves the most relevant chunks for a user's question
- Generates a clear, cited answer using a local LLM
- Runs fully offline, preserving data privacy (critical for legal, medical, or confidential documents)

---

## 3. Success Criteria

- [ ] User can upload a PDF via drag-and-drop or file picker
- [ ] Backend parses, chunks, embeds, and indexes the PDF within a reasonable time
- [ ] User can ask questions in a chat interface and get accurate, grounded answers
- [ ] Every answer cites the **page number(s) / section** it was derived from
- [ ] Multiple local LLMs are supported and can be switched by the user
- [ ] The entire pipeline runs without any internet/cloud dependency at inference time
- [ ] UI is clean, responsive, and intuitive for a non-technical user
- [ ] System handles edge cases: scanned PDFs, empty PDFs, out-of-context questions, multi-PDF sessions

---

## 4. Tech Stack (Mandatory)

| Layer | Technology |
|---|---|
| Backend | **Python 3.11+ with FastAPI** |
| Frontend | **HTML5, CSS3, vanilla JavaScript** (no heavy frontend framework required) |
| LLM Runtime | **Ollama** or **llama-cpp-python** (for running local GGUF/quantized models) |
| Vector Store | **ChromaDB** (preferred) or **FAISS** |
| Embeddings | **sentence-transformers** (local, CPU-friendly) |
| PDF Parsing | **PyMuPDF (fitz)** + **pdfplumber** + **pytesseract** (OCR fallback) |
| Server | **Uvicorn** (ASGI) |
| Realtime | **WebSockets** for token-by-token streaming responses |

---

## 5. LLM Model Suite & Roles

The system must support **multiple local models**, selectable from the UI, each suited to a different job. Route queries through an **Ollama** (or llama.cpp) backend running quantized (GGUF, Q4/Q5) versions for CPU/low-VRAM feasibility.

| Model | Role in the System |
|---|---|
| **MiniLM** (e.g. `all-MiniLM-L6-v2`) | Embedding model — converts PDF chunks and queries into vectors for semantic search. Not used for generation. |
| **Phi-3-mini** | Default fast-response chat model — lightweight, low-latency Q&A for straightforward factual questions. |
| **SmolLM** | Ultra-lightweight fallback model for low-resource machines or quick summarization tasks. |
| **Qwen3-Coder** | Specialized for PDFs containing code, technical specs, config files, or structured/tabular data — better at preserving formatting and logic in answers. |
| **Nous Hermes** | Conversational/instruction-tuned model for natural, dialogue-style follow-up questions and multi-turn context. |
| **DeepSeek R1** | Reasoning-heavy model for complex, multi-hop, or analytical questions (e.g. "compare section 3 and section 7 and explain the contradiction"). Slower but more accurate on hard queries. |

**Design note:** Implement a simple **model router** — either user-selected via a dropdown, or auto-selected based on query heuristics (keyword/code detection → Qwen3-Coder, long/analytical question → DeepSeek R1, else → Phi-3-mini). Keep MiniLM always active in the background for embeddings regardless of which chat model is selected.

---

## 6. System Architecture

```
┌─────────────────────────┐
│   Frontend (HTML/CSS/JS) │
│  - Upload UI             │
│  - Chat UI                │
│  - PDF preview pane       │
│  - Model selector         │
└───────────┬──────────────┘
            │ REST + WebSocket
┌───────────▼──────────────┐
│     FastAPI Backend       │
│  ┌──────────────────────┐ │
│  │ PDF Ingestion Service │ │  → PyMuPDF / pdfplumber / OCR
│  ├──────────────────────┤ │
│  │ Chunking Service      │ │  → recursive/semantic chunking
│  ├──────────────────────┤ │
│  │ Embedding Service      │ │  → sentence-transformers (MiniLM)
│  ├──────────────────────┤ │
│  │ Vector Store (Chroma) │ │  → per-document collections
│  ├──────────────────────┤ │
│  │ Retriever              │ │  → top-k similarity + reranking
│  ├──────────────────────┤ │
│  │ Model Router           │ │  → picks LLM per query
│  ├──────────────────────┤ │
│  │ LLM Inference (Ollama) │ │  → local generation + streaming
│  ├──────────────────────┤ │
│  │ Chat History Store     │ │  → SQLite/JSON per session
│  └──────────────────────┘ │
└───────────────────────────┘
```

---

## 7. RAG Pipeline — Step-by-Step Requirements

1. **Upload & Parse**
   - Accept PDF via `multipart/form-data`
   - Extract text with PyMuPDF; if a page has near-zero extractable text, treat as scanned and run pytesseract OCR
   - Preserve page numbers for citation

2. **Chunking**
   - Recursive character/token-based chunking (~500–800 tokens per chunk, ~10–15% overlap)
   - Store metadata: `{doc_id, page_number, chunk_index, source_filename}`

3. **Embedding & Indexing**
   - Embed each chunk using MiniLM
   - Store vectors + metadata in ChromaDB collection scoped to the document/session

4. **Query Handling**
   - Embed the user's question with the same MiniLM model
   - Retrieve top-k (e.g. k=4–6) relevant chunks via cosine similarity
   - Optional: rerank retrieved chunks before passing to the LLM

5. **Answer Generation**
   - Construct a grounded prompt: system instructions + retrieved chunks + chat history + user question
   - Explicitly instruct the model to **answer only from provided context** and say "not found in document" if the answer isn't present
   - Stream the response token-by-token to the frontend via WebSocket

6. **Citation**
   - Return page numbers / chunk references alongside the answer
   - Frontend highlights or links back to the relevant PDF page

---

## 8. Backend Requirements (FastAPI)

- Modular structure — separate routers for `upload`, `chat`, `documents`, `models`
- Async endpoints wherever I/O-bound (file handling, LLM calls)
- Pydantic models for strict request/response validation
- CORS enabled for local frontend dev
- Centralized error handling with meaningful HTTP status codes
- Session management (per-upload or per-user session ID) so multiple documents don't cross-contaminate context

### Suggested API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/upload` | Upload and process a PDF |
| `GET` | `/api/documents` | List uploaded/indexed documents |
| `DELETE` | `/api/documents/{doc_id}` | Remove a document and its vectors |
| `POST` | `/api/chat` | Send a question, get a full (non-streamed) answer |
| `WS` | `/ws/chat/{session_id}` | Streaming chat responses |
| `GET` | `/api/models` | List available local models and their status |
| `POST` | `/api/models/select` | Switch active generation model |
| `GET` | `/api/history/{session_id}` | Retrieve chat history |
| `GET` | `/api/health` | Health check for backend + Ollama runtime |

---

## 9. Frontend Requirements (HTML/CSS/JS)

**Layout:** Two-pane layout — PDF viewer/upload panel on one side, chat interface on the other.

- **Upload zone:** drag-and-drop + file picker, upload progress bar, list of indexed documents with delete option
- **Chat window:** message bubbles (user vs. assistant), streaming text rendering, markdown rendering for answers (tables, code blocks, bullet points)
- **Citations:** each answer shows clickable "Source: Page X" tags; clicking scrolls/jumps the PDF viewer to that page
- **Model selector:** dropdown to switch between Phi-3-mini / SmolLM / Qwen3-Coder / Nous Hermes / DeepSeek R1, with a short tooltip describing when to use each
- **Status indicators:** "Indexing document…", "Model thinking…", "Model loaded locally ✅"
- **Responsive design:** works on desktop and tablet widths
- **Theme:** clean, modern, minimal — light/dark mode toggle
- **Empty/error states:** friendly messaging for unsupported files, empty PDFs, no-answer-found cases

**No frontend framework is required** — use vanilla JS with `fetch`/`WebSocket` APIs to keep the stack lightweight and dependency-free, which is easier to demo and judge in a hackathon setting.

---

## 10. Required Python Libraries

```txt
# --- Backend framework ---
fastapi
uvicorn[standard]
python-multipart
pydantic
aiofiles
websockets

# --- PDF processing ---
PyMuPDF        # fitz - fast text/page extraction
pdfplumber      # table/layout-aware extraction
pytesseract     # OCR for scanned PDFs
pdf2image       # convert PDF pages to images for OCR
Pillow          # image handling

# --- RAG / embeddings / vector store ---
sentence-transformers
chromadb        # or: faiss-cpu
langchain       # or llama-index, for chunking/orchestration utilities
tiktoken

# --- Local LLM inference ---
ollama          # Python client for local Ollama server
# OR
llama-cpp-python   # if running GGUF models without Ollama

# --- Utilities ---
python-dotenv
numpy
pandas
sqlalchemy      # chat history persistence (SQLite)
loguru          # structured logging

# --- Dev/testing ---
pytest
httpx
```

> **Note:** `torch` and `transformers` are only needed if running Hugging Face models directly instead of via Ollama/llama.cpp. For a hackathon, **Ollama is strongly recommended** — it handles model quantization, memory management, and serving with minimal setup (`ollama pull phi3`, `ollama pull deepseek-r1`, etc.).

---

## 11. Suggested Folder Structure

```
pdf-chatbot/
├── backend/
│   ├── main.py
│   ├── routers/
│   │   ├── upload.py
│   │   ├── chat.py
│   │   ├── documents.py
│   │   └── models.py
│   ├── services/
│   │   ├── pdf_parser.py
│   │   ├── chunker.py
│   │   ├── embedder.py
│   │   ├── vector_store.py
│   │   ├── retriever.py
│   │   ├── model_router.py
│   │   └── llm_client.py
│   ├── models/
│   │   └── schemas.py
│   ├── db/
│   │   └── chat_history.py
│   └── config.py
├── frontend/
│   ├── index.html
│   ├── css/
│   │   └── style.css
│   └── js/
│       ├── upload.js
│       ├── chat.js
│       └── pdfViewer.js
├── data/
│   ├── uploads/
│   └── vector_store/
├── requirements.txt
└── README.md
```

---

## 12. Non-Functional Requirements

- **Privacy:** No document content or query ever leaves the local machine
- **Performance:** First-token latency should be reasonable on a mid-range laptop (CPU-only fallback must work, even if slower)
- **Robustness:** Handle malformed PDFs, huge PDFs (chunked processing), and duplicate uploads gracefully
- **Extensibility:** Model router and vector store should be swappable with minimal code changes
- **Observability:** Log ingestion time, retrieval scores, and generation latency for demo/debugging purposes

---

## 13. Stretch Goals (If Time Permits)

- Multi-document cross-referencing ("compare Document A and Document B")
- Highlight the exact retrieved text directly on the PDF page in the viewer
- Voice input for queries
- Export chat + citations as a summary report (PDF/Markdown)
- Confidence score displayed alongside each answer
- Auto-select the best model per query instead of manual switching

---

## 14. Hackathon Deliverables Checklist

- [ ] Working end-to-end demo (upload → ask → cited answer)
- [ ] `README.md` with setup instructions (Ollama model pulls, `pip install -r requirements.txt`, run commands)
- [ ] Short architecture diagram (can reuse Section 6 of this doc)
- [ ] 2–3 minute demo video or live walkthrough
- [ ] Note on offline/privacy guarantees and any known exceptions (e.g., one-time model download)

---

### Final Instruction to the AI Assistant / Dev Team

Build this system incrementally: **(1)** PDF upload + parsing → **(2)** chunking + embedding + vector store → **(3)** retrieval + single-model Q&A working end-to-end → **(4)** add streaming → **(5)** add multi-model routing and selector UI → **(6)** polish frontend UX and citations. Get the core RAG loop working with one model (Phi-3-mini) first before wiring in the rest of the model suite.
