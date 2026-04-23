"""
Embed text for Qdrant vectors. Keep in sync with production embedding model (env-driven).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import List, Optional


@lru_cache(maxsize=1)
def _embedding_dim() -> int:
    if _use_bedrock_embeddings():
        return 1024
    return int(os.getenv("RAG_EMBEDDING_DIM", "384"))


def _use_bedrock_embeddings() -> bool:
    if (os.getenv("RAG_USE_BEDROCK_EMBEDDING") or "").strip().lower() in ("true", "1", "yes"):
        return True
    if (os.getenv("RAG_EMBEDDING_PROVIDER") or "").strip().lower() == "bedrock":
        return True
    return False


@lru_cache(maxsize=1)
def _bedrock_client():
    import boto3

    region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1").strip()
    return boto3.client("bedrock-runtime", region_name=region)


def _bedrock_model_id() -> str:
    return (
        os.getenv("EMBEDDING_MODEL_ID")
        or "amazon.titan-embed-text-v2:0"
    ).strip()


def _embed_texts_bedrock(texts: List[str]) -> List[List[float]]:
    client = _bedrock_client()
    model_id = _bedrock_model_id()
    out: List[List[float]] = []
    for text in texts:
        body = {"inputText": text}
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        payload = json.loads(resp["body"].read())
        embedding = payload.get("embedding") or payload.get("vector")
        if not embedding:
            raise ValueError(f"Bedrock embedding response missing vector (keys={list(payload.keys())})")
        out.append(embedding)
    return out


def embed_texts(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """
    Batch embed strings. Priority:
    1. OPENAI_API_KEY -> OpenAI text-embedding-3-small (or RAG_OPENAI_EMBEDDING_MODEL)
    2. Local sentence-transformers (all-MiniLM-L6-v2, dim 384)
    """
    if not texts:
        return []
    if _use_bedrock_embeddings():
        return _embed_texts_bedrock(texts)
    use_local = os.getenv("USE_LOCAL_EMBEDDING", "").strip().lower() in ("true", "1", "yes")
    use_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if use_local:
        use_openai = False
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
    """Warm embedding backend to avoid first-request latency."""
    if _use_bedrock_embeddings():
        try:
            _embed_texts_bedrock(["warmup"])
        except Exception:
            # Warmup is best-effort; failures should not stop startup.
            return
        return
    use_local = os.getenv("USE_LOCAL_EMBEDDING", "").strip().lower() in ("true", "1", "yes")
    if not use_local and os.getenv("OPENAI_API_KEY", "").strip():
        return
    model_name = os.getenv("RAG_SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2").strip()
    model_st = _get_sentence_transformer(model_name)
    # Trigger model load and ensure weights are cached.
    model_st.encode(["warmup"], convert_to_numpy=True)
