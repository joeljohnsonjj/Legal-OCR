"""
Vector store for legal obligation chunks using ChromaDB.
Each obligation is one chunk: text is embedded and metadata (document_name, DutyType, Party, etc.) is stored for filtering.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default collection and persistence path
DEFAULT_COLLECTION_NAME = "obligations"
DEFAULT_CHROMA_PATH = "chroma_db"


def _flatten_field(val: Any) -> str:
    """Turn a field (string or list of strings) into a single string for chunk text."""
    if val is None:
        return ""
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val if x)
    return str(val).strip()


def obligation_to_chunk_text(obligation: Dict[str, Any]) -> str:
    """
    Convert one obligation dict into a single text chunk (full text).
    Used only when no related_keywords; otherwise use obligation_to_keyword_chunk_text for embedding.
    """
    duty = _flatten_field(obligation.get("DutyType"))
    party = _flatten_field(obligation.get("Responsible Party"))
    owner_resp = _flatten_field(obligation.get("Owner Responsibility"))
    reasoning = _flatten_field(obligation.get("Reasoning"))
    citation = obligation.get("Citation")
    if isinstance(citation, str):
        citation_str = citation
    elif isinstance(citation, list):
        parts = []
        for c in citation:
            if isinstance(c, dict):
                pages = c.get("pageNumbers") or []
                sections = c.get("section") or []
                if pages:
                    parts.append(f"Page {', '.join(map(str, pages))}")
                if sections:
                    parts.append("; ".join(sections))
            else:
                parts.append(str(c))
        citation_str = "; ".join(parts)
    else:
        citation_str = str(citation or "")
    return (
        f"DutyType: {duty}. Responsible Party: {party}. "
        f"Key Obligations: {owner_resp}. Reasoning: {reasoning}. Citation: {citation_str}"
    )


def obligation_to_keyword_chunk_text(obligation: Dict[str, Any]) -> str:
    """
    Text used for embedding in Chroma: Responsible Party + related_keywords, so search by party
    (e.g. "tenant") or topic both match. Fallback: party + DutyType + start of Owner Responsibility.
    """
    party = _flatten_field(obligation.get("Responsible Party")).strip()
    keywords = obligation.get("related_keywords")
    if isinstance(keywords, list) and keywords:
        keyword_str = " ".join(str(k).strip() for k in keywords if k).strip()
        return (party + " " + keyword_str).strip() if (party or keyword_str) else "obligation"
    # Fallback when related_keywords not present
    duty = _flatten_field(obligation.get("DutyType"))
    owner_resp = _flatten_field(obligation.get("Owner Responsibility"))[:200]
    base = (duty + " " + owner_resp).strip() or "obligation"
    return (party + " " + base).strip() if party else base


def _obligation_metadata(obligation: Dict[str, Any], document_name: str, index: int) -> Dict[str, str]:
    """Build ChromaDB-safe metadata (str values only) for one obligation."""
    duty = _flatten_field(obligation.get("DutyType")) or ""
    party = _flatten_field(obligation.get("Responsible Party")) or ""
    citation = obligation.get("Citation")
    if isinstance(citation, str):
        citation_str = citation[:2000]  # ChromaDB metadata size limit
    else:
        citation_str = str(citation or "")[:2000]
    return {
        "document_name": document_name,
        "DutyType": duty[:500],
        "Responsible_Party": party[:500],
        "Citation": citation_str,
        "chunk_index": str(index),
    }


def _chunk_id(document_name: str, index: int) -> str:
    """Stable unique id for a chunk (same doc + index = same id for upsert)."""
    raw = f"{document_name}|{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_chroma_client(path: Optional[str] = None):
    """Return a persistent ChromaDB client. path defaults to env CHROMA_DB_PATH or ./chroma_db."""
    import chromadb
    p = path or os.getenv("CHROMA_DB_PATH", DEFAULT_CHROMA_PATH)
    Path(p).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=p)


def get_embedding_function():
    """
    Return ChromaDB embedding function.
    Priority: USE_LOCAL_EMBEDDING=true -> sentence-transformers; else Azure if configured; else OpenAI; else sentence-transformers.
    """
    from chromadb.utils import embedding_functions

    # Local sentence-transformers (no API call at index or query time). Set USE_LOCAL_EMBEDDING=true to force this.
    use_local = os.getenv("USE_LOCAL_EMBEDDING", "").strip().lower() in ("true", "1", "yes")
    if use_local:
        model_name = os.getenv("SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2").strip() or "all-MiniLM-L6-v2"
        logger.info(f"Using local embeddings: sentence-transformers ({model_name})")
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model_name)

    # Azure OpenAI embeddings (same endpoint as chat; separate deployment for embedding model)
    use_azure = os.getenv("USE_AZURE_OPENAI", "").strip().lower() in ("true", "1", "yes")
    azure_embed_deploy = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "").strip()
    if use_azure and azure_embed_deploy:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
        api_key = (os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY", "")).strip()
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview").strip()
        if endpoint and api_key:
            return embedding_functions.OpenAIEmbeddingFunction(
                api_key=api_key,
                model_name=azure_embed_deploy,
                api_base=endpoint,
                api_type="azure",
                api_version=api_version,
                deployment_id=azure_embed_deploy,
            )

    # OpenAI (platform) embeddings
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"
    if api_key:
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=model,
        )

    # Fallback: local sentence-transformers (no API key)
    model_name = os.getenv("SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2").strip() or "all-MiniLM-L6-v2"
    logger.info(f"Using local embeddings: sentence-transformers ({model_name})")
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model_name)


def get_or_create_collection(client, collection_name: str = DEFAULT_COLLECTION_NAME):
    """Get or create the obligations collection with a local embedding function."""
    ef = get_embedding_function()
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
        metadata={"description": "Legal financial obligation chunks"},
    )


def delete_obligations_by_document(
    document_name: str,
    chroma_path: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
) -> None:
    """
    Remove all chunks for the given document from ChromaDB.
    Call this before re-indexing a document so no orphan chunks remain if the new run has fewer obligations.
    """
    try:
        client = get_chroma_client(chroma_path)
        collection = get_or_create_collection(client, collection_name)
        collection.delete(where={"document_name": document_name})
        logger.info(f"Deleted existing chunks for document: {document_name}")
    except Exception as e:
        logger.warning(f"Could not delete existing chunks for {document_name}: {e}")


def index_obligations(
    document_name: str,
    consolidated_results: List[Dict[str, Any]],
    chroma_path: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
) -> int:
    """
    Convert each obligation into a chunk and add to ChromaDB.
    Removes any existing chunks for this document first, then upserts the new set.
    Returns the number of chunks indexed.
    """
    if not consolidated_results:
        logger.warning("No obligations to index")
        return 0
    try:
        client = get_chroma_client(chroma_path)
        collection = get_or_create_collection(client, collection_name)
        delete_obligations_by_document(
            document_name=document_name,
            chroma_path=chroma_path,
            collection_name=collection_name,
        )
        documents = []
        metadatas = []
        ids = []
        for i, ob in enumerate(consolidated_results):
            chunk_text = obligation_to_keyword_chunk_text(ob)
            documents.append(chunk_text)
            metadatas.append(_obligation_metadata(ob, document_name, i))
            ids.append(_chunk_id(document_name, i))
        # Upsert: add or update by id (so re-run overwrites chunks for this document)
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logger.info(f"Indexed {len(ids)} obligation chunks for document: {document_name}")
        return len(ids)
    except Exception as e:
        logger.error(f"Vector index error: {e}", exc_info=True)
        raise


def query_obligations(
    query_text: str,
    n_results: int = 10,
    document_name: Optional[str] = None,
    chroma_path: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    max_distance: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Query the vector store by semantic similarity.
    Returns list of dicts with keys: id, document_name, DutyType, Responsible_Party, Citation, distance, document (chunk text).
    If document_name is provided, results are filtered to that document only.
    If max_distance is set, only results with distance <= max_distance are returned (query uses large n_results then filters).
    """
    try:
        client = get_chroma_client(chroma_path)
        collection = get_or_create_collection(client, collection_name)
        where = {"document_name": document_name} if document_name else None
        # When using distance threshold, fetch more candidates then filter
        fetch_n = 500 if max_distance is not None else n_results
        results = collection.query(
            query_texts=[query_text],
            n_results=fetch_n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        out = []
        if results and results["ids"] and results["ids"][0]:
            for i, id_ in enumerate(results["ids"][0]):
                meta = (results["metadatas"][0][i] or {}) if results["metadatas"] else {}
                dist = (results["distances"][0][i]) if results.get("distances") and results["distances"][0] else None
                if max_distance is not None and dist is not None and dist > max_distance:
                    continue
                doc = (results["documents"][0][i]) if results.get("documents") and results["documents"][0] else ""
                out.append({
                    "id": id_,
                    "document_name": meta.get("document_name", ""),
                    "DutyType": meta.get("DutyType", ""),
                    "Responsible_Party": meta.get("Responsible_Party", ""),
                    "Citation": meta.get("Citation", ""),
                    "chunk_index": meta.get("chunk_index", ""),
                    "distance": dist,
                    "document": doc,
                })
        return out
    except Exception as e:
        logger.error(f"Vector query error: {e}", exc_info=True)
        return []
