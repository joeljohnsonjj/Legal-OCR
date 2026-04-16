"""
Embed text for Qdrant vectors. Keep in sync with production embedding model (env-driven).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional


@lru_cache(maxsize=1)
def _embedding_dim() -> int:
    return int(os.getenv("RAG_EMBEDDING_DIM", "384"))


def embed_texts(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """
    Batch embed strings. Priority:
    1. OPENAI_API_KEY -> OpenAI text-embedding-3-small (or RAG_OPENAI_EMBEDDING_MODEL)
    2. Local sentence-transformers (all-MiniLM-L6-v2, dim 384)
    """
    if not texts:
        return []
    use_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if use_openai:
        from openai import OpenAI

        client = OpenAI()
        m = model or os.getenv("RAG_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip()
        resp = client.embeddings.create(model=m, input=texts)
        return [d.embedding for d in resp.data]
    # Local (single cached model — avoids reloads during Chroma + Qdrant in one process)
    mname = os.getenv("RAG_SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2").strip()
    model_st = _get_sentence_transformer(mname)
    emb = model_st.encode(texts, convert_to_numpy=True)
    return [row.tolist() for row in emb]


@lru_cache(maxsize=4)
def _get_sentence_transformer(model_name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def embed_text(text: str) -> List[float]:
    return embed_texts([text])[0]


def warm_sentence_transformer() -> None:
    """Preload sentence-transformers model at startup to avoid first-request latency."""
    if os.getenv("OPENAI_API_KEY", "").strip():
        return
    model_name = os.getenv("RAG_SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2").strip()
    model_st = _get_sentence_transformer(model_name)
    # Trigger model load and ensure weights are cached.
    model_st.encode(["warmup"], convert_to_numpy=True)
