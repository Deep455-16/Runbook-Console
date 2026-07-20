"""
ingest.py
---------
Builds the retrieval indexes for the Runbook RAG Chatbot.

What it does:
  1. Walks the `runbooks/` directory for Markdown (.md) runbook files.
  2. Splits each file into citable chunks using its own heading structure
     (H1 = doc title, H2 = section) instead of blind fixed-size windows, so a
     citation always points at a real, meaningful section of a real runbook.
  3. Embeds every chunk locally with a `transformers` AutoModel at
     EMBED_MODEL_PATH (no network calls, no API keys, no sentence-transformers).
  4. Builds a FAISS flat inner-product index over the (L2-normalized)
     embeddings -> exact cosine similarity search.
  5. Builds a BM25 (rank_bm25) index over the same chunks for lexical /
     keyword matching -> this is what makes exact error strings like
     "ECONNREFUSED" or "PSQLException" retrievable even when the embedding
     model doesn't line them up semantically.
  6. Persists everything to `index_store/` so server.py can load it instantly
     at startup without re-embedding anything.

Run this once after adding/editing files in runbooks/, then (re)start server.py.

Usage:
    python ingest.py
"""

from __future__ import annotations

import json
import os
import pickle
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

import numpy as np

os.environ["TRANSFORMERS_OFFLINE"] = "1"

# ------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
EMBED_MODEL_PATH = str(MODELS_DIR / "MiniLM-L6-v2")

RUNBOOKS_DIR = BASE_DIR / "runbooks"
INDEX_DIR = BASE_DIR / "index_store"

FAISS_INDEX_PATH = INDEX_DIR / "faiss.index"
BM25_PATH = INDEX_DIR / "bm25.pkl"
CHUNKS_PATH = INDEX_DIR / "chunks.json"

# Sub-chunking: if a section under a single H2 heading is longer than this
# many words, we split it further with overlap so no single chunk is too
# large for the LLM's context window or too coarse for precise citation.
MAX_CHUNK_WORDS = 180
CHUNK_OVERLAP_WORDS = 40


# ------------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------------

@dataclass
class Chunk:
    id: int
    file: str  # filename relative to runbooks/, used as the citation source
    doc_title: str  # H1 title of the runbook
    section: str  # H2 heading this chunk falls under
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    text: str


# ------------------------------------------------------------------------
# Markdown -> chunks
# ------------------------------------------------------------------------

H1_RE = re.compile(r"^#\s+(.*)")
H2_RE = re.compile(r"^##\s+(.*)")


def split_words_with_overlap(words: list[str], max_words: int, overlap: int) -> list[list[str]]:
    """Greedy sliding window split, used only for oversized sections."""
    if len(words) <= max_words:
        return [words]
    windows = []
    step = max(max_words - overlap, 1)
    for start in range(0, len(words), step):
        window = words[start:start + max_words]
        if not window:
            break
        windows.append(window)
        if start + max_words >= len(words):
            break
    return windows


def chunk_file(path: Path, start_id: int) -> list[Chunk]:
    """Parse one runbook markdown file into a list of Chunk objects.

    Sections are delimited by H2 (##) headings. The H1 (#) heading, if
    present, is captured as the doc title and attached as context to every
    chunk from that file. Oversized sections are further split with a word
    overlap so retrieval precision doesn't degrade on long runbooks.
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    doc_title = path.stem.replace("-", " ").replace("_", " ").title()
    for line in lines:
        m = H1_RE.match(line)
        if m:
            doc_title = m.group(1).strip()
            break

    # Collect (section_name, start_line, end_line, text) for each H2 block.
    # Anything before the first H2 (e.g. metadata under the H1) is grouped
    # under an "Overview" pseudo-section so it's still retrievable.
    sections: list[tuple[str, int, int, list[str]]] = []
    current_name = "Overview"
    current_start = 1
    current_lines: list[str] = []

    def flush(end_line: int):
        if any(l.strip() for l in current_lines):
            sections.append((current_name, current_start, end_line, current_lines[:]))

    for i, line in enumerate(lines, start=1):
        h2 = H2_RE.match(line)
        if h2:
            flush(i - 1)
            current_name = h2.group(1).strip()
            current_start = i
            current_lines = []
        else:
            current_lines.append(line)
    flush(len(lines))

    chunks: list[Chunk] = []
    next_id = start_id
    for section_name, sec_start, sec_end, sec_lines in sections:
        text = "\n".join(sec_lines).strip()
        if not text:
            continue
        words = text.split()
        word_windows = split_words_with_overlap(words, MAX_CHUNK_WORDS, CHUNK_OVERLAP_WORDS)

        if len(word_windows) == 1:
            chunks.append(Chunk(
                id=next_id, file=path.name, doc_title=doc_title,
                section=section_name, start_line=sec_start, end_line=sec_end,
                text=text,
            ))
            next_id += 1
        else:
            # Approximate sub-chunk line ranges proportionally to word position.
            total_words = len(words)
            total_lines = max(sec_end - sec_start, 1)
            for w_start_idx, window in enumerate(word_windows):
                # Find where this window starts among `words` to estimate lines.
                offset = sum(len(w) for w in word_windows[:w_start_idx]) if w_start_idx else 0
                frac_start = offset / max(total_words, 1)
                frac_end = min((offset + len(window)) / max(total_words, 1), 1.0)
                approx_start = sec_start + int(frac_start * total_lines)
                approx_end = sec_start + int(frac_end * total_lines)
                label = f"{section_name} (part {w_start_idx + 1}/{len(word_windows)})"
                chunks.append(Chunk(
                    id=next_id, file=path.name, doc_title=doc_title,
                    section=label, start_line=approx_start, end_line=max(approx_end, approx_start),
                    text=" ".join(window),
                ))
                next_id += 1

    return chunks


def tokenize(text: str) -> list[str]:
    """Lightweight tokenizer for BM25 — lowercase alnum tokens, keeps things
    like error codes ('econnrefused', 'db-pool-01') intact without needing
    nltk or any downloaded corpora."""
    return re.findall(r"[a-z0-9][a-z0-9_\-]*", text.lower())


# ------------------------------------------------------------------------
# Embedding (plain transformers, no sentence-transformers)
# ------------------------------------------------------------------------

_tokenizer = None
_model = None


def _load_embed_model():
    """Lazily load the tokenizer/model once, offline, from EMBED_MODEL_PATH."""
    global _tokenizer, _model
    if _model is None:
        import torch
        from transformers import AutoTokenizer, AutoModel

        _tokenizer = AutoTokenizer.from_pretrained(EMBED_MODEL_PATH, local_files_only=True)
        _model = AutoModel.from_pretrained(EMBED_MODEL_PATH, local_files_only=True)
        _model.eval()


def _mean_pool(token_embeddings, attention_mask):
    import torch

    mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * mask_expanded, 1) / torch.clamp(mask_expanded.sum(1), min=1e-9)


def embed_texts(texts: List[str], batch_size: int = 32, max_length: int = 256) -> np.ndarray:
    """Embed a list of texts with mean-pooling + L2 normalization, in batches,
    using plain `transformers` (AutoTokenizer + AutoModel) instead of
    sentence-transformers. Returns a (len(texts), dim) float32 numpy array
    where rows are L2-normalized, so inner product == cosine similarity."""
    import torch

    _load_embed_model()
    all_embeds = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        encoded = _tokenizer(
            batch, padding=True, truncation=True,
            max_length=max_length, return_tensors="pt",
        )
        with torch.no_grad():
            output = _model(**encoded)
        pooled = _mean_pool(output.last_hidden_state, encoded["attention_mask"])
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        all_embeds.append(pooled.cpu().numpy())
        print(f"[ingest] embedded {min(i + batch_size, len(texts))}/{len(texts)}")
    return np.vstack(all_embeds).astype("float32")


# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------

def main():
    if not RUNBOOKS_DIR.exists():
        print(f"[ingest] ERROR: {RUNBOOKS_DIR} does not exist.", file=sys.stderr)
        sys.exit(1)

    md_files = sorted(RUNBOOKS_DIR.glob("*.md"))
    if not md_files:
        print(f"[ingest] ERROR: no .md files found in {RUNBOOKS_DIR}.", file=sys.stderr)
        sys.exit(1)

    print(f"[ingest] found {len(md_files)} runbook file(s)")

    all_chunks: list[Chunk] = []
    for path in md_files:
        file_chunks = chunk_file(path, start_id=len(all_chunks))
        print(f"  - {path.name}: {len(file_chunks)} chunk(s)")
        all_chunks.extend(file_chunks)

    if not all_chunks:
        print("[ingest] ERROR: parsed 0 chunks total.", file=sys.stderr)
        sys.exit(1)

    print(f"[ingest] total chunks: {len(all_chunks)}")

    # --- Dense embeddings + FAISS ---
    print(f"[ingest] loading embedding model from {EMBED_MODEL_PATH} ...")
    import faiss

    texts = [c.text for c in all_chunks]
    print("[ingest] embedding chunks ...")
    embeddings = embed_texts(texts, batch_size=32)

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(FAISS_INDEX_PATH))
    print(f"[ingest] wrote FAISS index ({index.ntotal} vectors, dim={dim}) -> {FAISS_INDEX_PATH}")

    # --- Sparse BM25 ---
    from rank_bm25 import BM25Okapi
    tokenized_corpus = [tokenize(t) for t in texts]
    bm25 = BM25Okapi(tokenized_corpus)
    with open(BM25_PATH, "wb") as f:
        pickle.dump(bm25, f)
    print(f"[ingest] wrote BM25 index -> {BM25_PATH}")

    # --- Chunk metadata (order MUST match the FAISS / BM25 row order) ---
    with open(CHUNKS_PATH, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in all_chunks], f, ensure_ascii=False, indent=2)
    print(f"[ingest] wrote chunk metadata -> {CHUNKS_PATH}")

    print("[ingest] done. Start the server with: uvicorn server:app --reload")


if __name__ == "__main__":
    main()