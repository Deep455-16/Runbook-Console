"""
server.py
---------
FastAPI backend for the Runbook RAG Chatbot (HPE hackathon submission #11).

Pipeline for every query:
    query
      -> dense retrieval   (FAISS, cosine sim via local MiniLM-L6-v2 embeddings)
      -> sparse retrieval  (BM25 over the same chunks, catches exact error strings)
      -> Reciprocal Rank Fusion (hybrid re-ranking, no score normalization needed)
      -> top-K chunks become the LLM's *only* allowed context
      -> local Qwen2.5-3B-Instruct (GGUF, via llama-cpp-python) generates the
         answer, streamed token-by-token over SSE, citing [Source N] inline
      -> every claim is traceable back to an exact file + section + line range

If retrieval confidence is below CONFIDENCE_THRESHOLD, the model is not asked
to answer at all -- we return a "no matching runbook" response instead. This
matters a lot in an on-call tool: a fabricated remediation step is worse than
no answer.

Run with:
    uvicorn server:app --reload --port 8000
Then open http://127.0.0.1:8000/
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import List

import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import io
import webbrowser
import threading
import os

os.environ["TRANSFORMERS_OFFLINE"] = "1"

# ------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
EMBED_MODEL_PATH = str(MODELS_DIR / "MiniLM-L6-v2")
LLM_MODEL_PATH = str(MODELS_DIR / "Qwen2.5-3B-Instruct-Q3_K_M.gguf")
INDEX_DIR = BASE_DIR / "index_store"
FAISS_INDEX_PATH = INDEX_DIR / "faiss.index"
BM25_PATH = INDEX_DIR / "bm25.pkl"
CHUNKS_PATH = INDEX_DIR / "chunks.json"
STATIC_DIR = BASE_DIR / "static"

TOP_K_DENSE = 8          # candidates pulled from FAISS before fusion
TOP_K_SPARSE = 8         # candidates pulled from BM25 before fusion
TOP_K_FINAL = 5          # chunks actually handed to the LLM as context
RRF_K = 60                # standard Reciprocal Rank Fusion damping constant

CONFIDENCE_THRESHOLD = 0.32   # min top-1 cosine similarity to attempt an answer
N_CTX = 8192
N_GPU_LAYERS = -1             # -1 = offload every layer to GPU if a CUDA build is installed
MAX_NEW_TOKENS = 512

SYSTEM_PROMPT = (
    "You are an on-call operations assistant. You answer ONLY using the runbook "
    "excerpts provided below, which are labeled [Source 1], [Source 2], etc. "
    "Rules:\n"
    "1. Every factual claim or remediation step must include the matching "
    "[Source N] citation right after it.\n"
    "2. If the excerpts do not contain enough information to answer, say so "
    "plainly and suggest escalating -- never invent steps, commands, thresholds, "
    "or contacts that are not in the excerpts.\n"
    "3. Be concise and use numbered steps for remediation procedures.\n"
)


# ------------------------------------------------------------------------
# Embedding (plain transformers, no sentence-transformers)
# ------------------------------------------------------------------------

class Embedder:
    """Thin wrapper around AutoTokenizer + AutoModel with mean-pooling,
    exposing an `.encode()` method shaped like sentence-transformers'
    so the rest of the file barely has to change."""

    def __init__(self, model_path: str):
        import torch
        from transformers import AutoTokenizer, AutoModel

        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModel.from_pretrained(model_path, local_files_only=True)
        self.model.eval()

    def _mean_pool(self, token_embeddings, attention_mask):
        torch = self._torch
        mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * mask_expanded, 1) / torch.clamp(mask_expanded.sum(1), min=1e-9)

    def encode(self, texts: List[str], batch_size: int = 32, max_length: int = 256,
               normalize_embeddings: bool = True, convert_to_numpy: bool = True) -> np.ndarray:
        torch = self._torch
        all_embeds = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            encoded = self.tokenizer(
                batch, padding=True, truncation=True,
                max_length=max_length, return_tensors="pt",
            )
            with torch.no_grad():
                output = self.model(**encoded)
            pooled = self._mean_pool(output.last_hidden_state, encoded["attention_mask"])
            if normalize_embeddings:
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            all_embeds.append(pooled.cpu().numpy())
        result = np.vstack(all_embeds)
        return result if convert_to_numpy else result.tolist()


# ------------------------------------------------------------------------
# Globals populated at startup
# ------------------------------------------------------------------------

class RetrievalState:
    embedder: Embedder | None = None
    faiss_index = None
    bm25 = None
    chunks: List[dict] = []
    llm = None


state = RetrievalState()
app = FastAPI(title="Runbook RAG Chatbot")


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_\-]*", text.lower())


def load_indexes():
    missing = [p for p in (FAISS_INDEX_PATH, BM25_PATH, CHUNKS_PATH) if not p.exists()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise RuntimeError(
            f"Index files missing ({names}). Run `python ingest.py` first."
        )

    print("[server] loading chunk metadata ...")
    state.chunks = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))

    print("[server] loading FAISS index ...")
    import faiss
    state.faiss_index = faiss.read_index(str(FAISS_INDEX_PATH))

    print("[server] loading BM25 index ...")
    import pickle
    with open(BM25_PATH, "rb") as f:
        state.bm25 = pickle.load(f)

@app.on_event("startup")
def load_everything():
    load_indexes()

    print(f"[server] loading embedding model from {EMBED_MODEL_PATH} ...")
    state.embedder = Embedder(EMBED_MODEL_PATH)

    print(f"[server] loading local LLM from {LLM_MODEL_PATH} ...")
    from llama_cpp import Llama
    state.llm = Llama(
        model_path=LLM_MODEL_PATH,
        n_ctx=N_CTX,
        n_gpu_layers=N_GPU_LAYERS,
        verbose=False,
    )

    print(f"[server] ready. {len(state.chunks)} chunks indexed from "
          f"{len(set(c['file'] for c in state.chunks))} runbook file(s).")
          
    # Open browser automatically on first startup
    if not os.environ.get("BROWSER_OPENED"):
        os.environ["BROWSER_OPENED"] = "1"
        def open_browser():
            webbrowser.open("http://127.0.0.1:8000/")
        threading.Timer(1.5, open_browser).start()


# ------------------------------------------------------------------------
# Retrieval
# ------------------------------------------------------------------------

def dense_search(query: str, top_k: int):
    q_emb = state.embedder.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
    scores, idxs = state.faiss_index.search(q_emb, top_k)
    return [(int(i), float(s)) for i, s in zip(idxs[0], scores[0]) if i != -1]


def sparse_search(query: str, top_k: int):
    tokens = tokenize(query)
    scores = state.bm25.get_scores(tokens)
    top_idxs = np.argsort(scores)[::-1][:top_k]
    return [(int(i), float(scores[i])) for i in top_idxs if scores[i] > 0]


def hybrid_retrieve(query: str):
    """Reciprocal Rank Fusion of dense + sparse results.

    RRF avoids having to normalize BM25 scores (unbounded) against cosine
    scores (bounded [-1, 1]) -- it only needs each retriever's *ranking*,
    which makes the fusion robust across very different score distributions.
    """
    dense_hits = dense_search(query, TOP_K_DENSE)
    sparse_hits = sparse_search(query, TOP_K_SPARSE)

    top1_cosine = dense_hits[0][1] if dense_hits else 0.0

    fused: dict[int, float] = {}
    for rank, (idx, _score) in enumerate(dense_hits):
        fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (idx, _score) in enumerate(sparse_hits):
        fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:TOP_K_FINAL]

    dense_ids = {i for i, _ in dense_hits}
    sparse_ids = {i for i, _ in sparse_hits}

    results = []
    for idx, fused_score in ranked:
        chunk = dict(state.chunks[idx])
        chunk["fused_score"] = fused_score
        chunk["matched_dense"] = idx in dense_ids
        chunk["matched_sparse"] = idx in sparse_ids
        results.append(chunk)
    return results, top1_cosine


def build_prompt(query: str, sources: List[dict]) -> str:
    context_blocks = []
    for n, s in enumerate(sources, start=1):
        header = f"[Source {n}] File: {s['file']} | Section: {s['section']} | Lines {s['start_line']}-{s['end_line']}"
        context_blocks.append(f"{header}\n{s['text']}")
    context = "\n\n".join(context_blocks)

    user_msg = f"Runbook excerpts:\n\n{context}\n\nOn-call engineer's question: {query}"

    # Qwen2.5-Instruct ChatML template, built manually for predictable stop tokens.
    return (
        f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{user_msg}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


# ------------------------------------------------------------------------
# API models
# ------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str


# ------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------

@app.get("/api/stats")
def stats():
    if not state.chunks:
        raise HTTPException(503, "Index not loaded")
    files = sorted(set(c["file"] for c in state.chunks))
    return {
        "chunks_indexed": len(state.chunks),
        "files_indexed": len(files),
        "file_names": files,
        "embed_model": Path(EMBED_MODEL_PATH).name,
        "llm_model": Path(LLM_MODEL_PATH).name,
        "retrieval_mode": "hybrid (FAISS dense + BM25 sparse, RRF fusion)",
    }

@app.post("/api/upload_runbook")
async def upload_runbook(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".md", ".pdf", ".docx")):
        raise HTTPException(400, "Only .md, .pdf, and .docx files are supported")
        
    content = await file.read()
    
    out_path = BASE_DIR / "runbooks" / file.filename
    out_path.write_bytes(content)
        
    # Trigger ingestion
    try:
        subprocess.run(["python", str(BASE_DIR / "ingest.py")], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Ingestion failed: {e.stderr.decode('utf-8', errors='ignore')}")
        
    # Reload the indexes dynamically
    load_indexes()
    
    return {"status": "success", "filename": file.filename}


@app.post("/api/query")
def query(req: QueryRequest):
    q = req.query.strip()
    if not q:
        raise HTTPException(400, "Empty query")
    print(f"Server: Recieved {q}")

    def event_stream():
        t0 = time.time()
        sources, top1_cosine = hybrid_retrieve(q)
        retrieval_ms = int((time.time() - t0) * 1000)

        if not sources or top1_cosine < CONFIDENCE_THRESHOLD:
            payload = {
                "type": "no_match",
                "message": (
                    "No runbook in the index matches this issue with enough "
                    "confidence to answer safely. Recommend escalating to the "
                    "on-call lead or checking the internal wiki for undocumented "
                    "procedures."
                ),
                "top1_cosine": round(top1_cosine, 3),
                "retrieval_ms": retrieval_ms,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            return

        yield f"data: {json.dumps({'type': 'sources', 'sources': sources, 'retrieval_ms': retrieval_ms})}\n\n"

        prompt = build_prompt(q, sources)
        t1 = time.time()
        stream = state.llm(
            prompt,
            max_tokens=MAX_NEW_TOKENS,
            temperature=0.2,
            top_p=0.9,
            stop=["<|im_end|>"],
            stream=True,
        )
        for chunk in stream:
            text = chunk["choices"][0]["text"]
            if text:
                yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"

        generation_ms = int((time.time() - t1) * 1000)
        yield f"data: {json.dumps({'type': 'done', 'generation_ms': generation_ms})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")