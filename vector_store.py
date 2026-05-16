"""
Vector store for legal obligation chunks using ChromaDB.
Primary retrieval surface: one chunk per obligation.

**Embedding text (default):** When ``related_keywords`` is present and non-empty, the embedded
document string is **only** those keywords (space-joined, light de-duplication). Semantic search
then aligns with the curated keyword list. Set ``LEGAL_OCR_VECTOR_EMBED_RELATED_KEYWORDS_ONLY=false``
to restore the legacy chunk: party prefix + DutyType + keywords + duty snippet from Owner
Responsibility.

Optional: ``LEGAL_OCR_EMBED_KEYWORDS_PARTY_PREFIX=true`` prepends tenant/landlord role tokens when
using keywords-only mode.

Fallback when ``related_keywords`` is missing or empty: party + DutyType + start of Owner
Responsibility (unchanged).

Re-index Chroma after changing embedding logic (``index_obligations`` / reprocess document).
"""

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Increase recursion limit to handle deep nested module calls in transformers/torch
# This prevents RecursionError during embedding initialization
sys.setrecursionlimit(10000)

logger = logging.getLogger(__name__)


# Default collection and persistence path
DEFAULT_COLLECTION_NAME = "obligations"
DEFAULT_CHROMA_PATH = "chroma_db"

# Supplement keyword chips: duty lines often contain topic words never copied into related_keywords.
_EMBED_LINE_TOPIC_HINT = re.compile(
    r"maintenance|maintain|repair|replac|hvac|plumb|electr|roof|fixture|alteration|"
    r"rent|additional\s+rent|holdover|abatement|operating\s+expense|utility|utilities|mechanical|heating|cooling|"
    r"ventilation|sprinkler|sewer|meter|default|deposit|compliance|landlord'?s\s+service|common\s+area|"
    r"tenant\s+improvement|cam|triple\s+net|security|insurance|indemnif",
    re.I,
)


def _embed_duty_snippet_budget() -> int:
    raw = (os.getenv("LEGAL_OCR_EMBED_DUTY_SNIPPET_CHARS") or "700").strip()
    try:
        n = int(raw)
        return max(0, min(n, 2500))
    except ValueError:
        return 700


def _owner_responsibility_snippet_for_embedding(obligation: Dict[str, Any]) -> str:
    """
    Short text from Owner Responsibility lines: always the opening duty line(s), plus any
    additional lines whose vocabulary matches common obligation themes (maintenance, rent, etc.).
    """
    budget = _embed_duty_snippet_budget()
    if budget <= 0:
        return ""
    v = obligation.get("Owner Responsibility")
    lines: List[str] = []
    if isinstance(v, list):
        lines = [str(x).strip() for x in v if x is not None and str(x).strip()]
    elif v is not None and str(v).strip():
        lines = [str(v).strip()]
    if not lines:
        return ""

    pieces: List[str] = []
    pieces.append(lines[0][:240])
    seen_low = {lines[0].lower()}
    if len(lines) > 1:
        pieces.append(lines[1][:200])
        seen_low.add(lines[1].lower())
    for ln in lines[2:]:
        if _EMBED_LINE_TOPIC_HINT.search(ln) and ln.lower() not in seen_low:
            pieces.append(ln[:180])
            seen_low.add(ln.lower())

    blob = " ".join(p for p in pieces if p)
    return blob[:budget]


def _flatten_field(val: Any) -> str:
    """Turn a field (string or list of strings) into a single string for chunk text."""
    if val is None:
        return ""
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val if x)
    return str(val).strip()


def _keywords_only_embedding_enabled() -> bool:
    """When True (default), embed only joined related_keywords when that list is non-empty."""
    v = (os.getenv("LEGAL_OCR_VECTOR_EMBED_RELATED_KEYWORDS_ONLY") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _join_related_keywords_for_embedding(obligation: Dict[str, Any]) -> Optional[str]:
    """
    Space-join related_keywords with case-insensitive de-duplication (order preserved).
    Returns None if missing or empty.
    """
    keywords = obligation.get("related_keywords")
    if not isinstance(keywords, list) or not keywords:
        return None
    parts = [str(k).strip() for k in keywords if k is not None and str(k).strip()]
    if not parts:
        return None
    seen: set = set()
    ordered: List[str] = []
    for p in parts:
        pl = p.lower()
        if pl in seen:
            continue
        seen.add(pl)
        ordered.append(p)
    blob = " ".join(ordered).strip()
    if not blob:
        return None
    raw = (os.getenv("LEGAL_OCR_EMBED_KEYWORDS_MAX_CHARS") or "12000").strip()
    try:
        max_c = int(raw)
    except ValueError:
        max_c = 12000
    if max_c > 0 and len(blob) > max_c:
        blob = blob[:max_c].rstrip()
    return blob


def _party_embedding_prefix(party: str) -> str:
    """
    Tokens prepended to embedded chunk text so queries like 'tenant' / 'landlord' match
    semantically even when related_keywords omit party words (they're often duty-focused).
    """
    p = (party or "").strip().lower()
    if not p:
        return ""
    tokens: List[str] = []
    if p == "unidentified":
        tokens.append("unidentified")
    elif p in ("unspecified", "unspecified party"):
        tokens.extend(["unspecified party", "unspecified"])
    if p == "tenant" or p.startswith("tenant ") or "lessee" in p:
        tokens.extend(["tenant", "lessee"])
    elif "tenant" in p or "lessee" in p:
        tokens.extend(["tenant", "lessee"])
    if p == "landlord" or p.startswith("landlord ") or "lessor" in p:
        tokens.extend(["landlord", "lessor"])
    elif "landlord" in p or "lessor" in p:
        tokens.extend(["landlord", "lessor"])
    # Dedupe preserving order
    seen: set = set()
    ordered: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return " ".join(ordered)


def _party_metadata_where_clause(responsible_party: str) -> Dict[str, Any]:
    """
    Chroma metadata uses exact match on Responsible_Party. Consolidated JSON often stores
    lowercase 'tenant' / 'landlord' while callers may pass Title Case — expand variants
    (and common synonyms) so filters still match.
    """
    rp = (responsible_party or "").strip()
    if not rp:
        return {}
    variants: List[str] = []
    for v in (rp, rp.lower(), rp.capitalize(), rp.title()):
        if v and v not in variants:
            variants.append(v)
    low = rp.lower()
    if low in ("unspecified", "unspecified party"):
        for extra in ("Unspecified Party", "Unspecified", "UNSPECIFIED"):
            if extra not in variants:
                variants.append(extra)
    elif low == "unidentified":
        for extra in ("Unidentified", "UNIDENTIFIED"):
            if extra not in variants:
                variants.append(extra)
    elif low == "tenant":
        for extra in ("Lessee", "lessee", "LESSEE"):
            if extra not in variants:
                variants.append(extra)
    elif low == "landlord":
        for extra in ("Lessor", "lessor", "LESSOR"):
            if extra not in variants:
                variants.append(extra)
    if len(variants) == 1:
        return {"Responsible_Party": variants[0]}
    return {"$or": [{"Responsible_Party": v} for v in variants]}


def _citation_blob_for_chunk(obligation: Dict[str, Any]) -> str:
    """Human-readable citation snippet for chunk text (legacy Citation or extraction `citations`)."""
    cit = obligation.get("citations")
    if cit is None:
        cit = obligation.get("Citation")
    if isinstance(cit, str):
        return cit
    if isinstance(cit, list):
        parts: List[str] = []
        for c in cit:
            if not isinstance(c, dict):
                parts.append(str(c))
                continue
            if "references" in c:
                doc = str(c.get("docId") or "")
                for ref in c.get("references") or []:
                    if isinstance(ref, dict):
                        parts.append(f"{doc} p.{ref.get('page')} §{ref.get('section', '')}".strip())
                continue
            pages = c.get("pageNumbers") or []
            sections = c.get("section") or []
            if pages:
                parts.append(f"Page {', '.join(map(str, pages))}")
            if sections:
                parts.append("; ".join(str(s) for s in (sections if isinstance(sections, list) else [sections])))
        return "; ".join(parts)
    return str(cit or "")


def obligation_to_chunk_text(obligation: Dict[str, Any]) -> str:
    """
    Convert one obligation dict into a single text chunk (full text).
    Used only when no related_keywords; otherwise use obligation_to_keyword_chunk_text for embedding.
    """
    duty = _flatten_field(obligation.get("DutyType"))
    party = _flatten_field(obligation.get("Responsible Party"))
    owner_resp = _flatten_field(obligation.get("Owner Responsibility"))
    reasoning = _flatten_field(obligation.get("Reasoning"))
    citation_str = _citation_blob_for_chunk(obligation)
    return (
        f"DutyType: {duty}. Responsible Party: {party}. "
        f"Key Obligations: {owner_resp}. Reasoning: {reasoning}. Citation: {citation_str}"
    )


def category_to_keyword_chunk_text(category_data: Dict[str, Any]) -> str:
    """
    Extract and combine all keywords from all obligations in a category for semantic search.
    
    Args:
        category_data: Dict with "category" name and "obligations" list
        
    Returns:
        Combined keywords string for category-level embedding
    """
    category_name = category_data.get("category", "")
    obligations = category_data.get("obligations", [])
    
    all_keywords = []
    
    # Add category name as keywords
    if category_name:
        # Split category name into meaningful tokens
        category_tokens = category_name.lower().replace("&", "").replace("-", " ").split()
        all_keywords.extend(category_tokens)
    
    # Collect all related_keywords from obligations in this category
    for obligation in obligations:
        if isinstance(obligation, dict):
            keywords = obligation.get("related_keywords", [])
            if isinstance(keywords, list):
                all_keywords.extend([str(k).strip() for k in keywords if k])
    
    # Remove duplicates while preserving order
    seen = set()
    unique_keywords = []
    for keyword in all_keywords:
        keyword_lower = keyword.lower()
        if keyword_lower not in seen and keyword_lower:
            seen.add(keyword_lower)
            unique_keywords.append(keyword)
    
    # Join keywords with space separation
    keywords_text = " ".join(unique_keywords)
    
    # Limit length to avoid extremely long embeddings
    max_chars = int(os.getenv("CATEGORY_EMBED_MAX_CHARS", "8000"))
    if len(keywords_text) > max_chars:
        keywords_text = keywords_text[:max_chars].rstrip()
    
    return keywords_text


def obligation_to_keyword_chunk_text(obligation: Dict[str, Any]) -> str:
    """
    Text embedded in Chroma for semantic search.

    NEW APPROACH: Uses auto-generated keywords from individual responsibility lines.
    This replaces the static related_keywords approach with dynamic keyword generation
    that's specific to each obligation.

    Default (``LEGAL_OCR_VECTOR_EMBED_RELATED_KEYWORDS_ONLY`` unset or true): when
    ``related_keywords`` is non-empty, the embedded string is **only** those keywords (see
    ``_join_related_keywords_for_embedding``). Optional ``LEGAL_OCR_EMBED_KEYWORDS_PARTY_PREFIX=true``
    prepends tenant/landlord tokens.

    Legacy mode (``LEGAL_OCR_VECTOR_EMBED_RELATED_KEYWORDS_ONLY=false``): party prefix + DutyType +
    keywords + duty snippet from Owner Responsibility.

    Fallback when keywords are missing or empty: party + DutyType + start of Owner Responsibility.
    """
    # If this is an atomic responsibility row, ensure embedding uses its specific keywords
    resp_id = obligation.get("responsibility_id")
    if resp_id:
        # Atomic row: keywords are per-responsibility, not aggregate
        kw_blob = _join_related_keywords_for_embedding(obligation)
        if kw_blob and _keywords_only_embedding_enabled():
            party = _flatten_field(obligation.get("Responsible Party")).strip()
            role_prefix = _party_embedding_prefix(party)
            if (os.getenv("LEGAL_OCR_EMBED_KEYWORDS_PARTY_PREFIX") or "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                return f"{role_prefix} {kw_blob}".strip() if role_prefix else kw_blob
            return kw_blob

    party = _flatten_field(obligation.get("Responsible Party")).strip()
    role_prefix = _party_embedding_prefix(party)
    duty_cat = _flatten_field(obligation.get("DutyType")).strip()

    # NEW: Try auto-generated keywords first
    auto_keywords = obligation.get("auto_generated_keywords")
    if isinstance(auto_keywords, list) and auto_keywords:
        # Use auto-generated keywords as primary embedding content
        keyword_str = " ".join(str(k).strip() for k in auto_keywords if k).strip()
        if keyword_str:
            if (os.getenv("LEGAL_OCR_EMBED_KEYWORDS_PARTY_PREFIX") or "").strip().lower() in (
                "1", "true", "yes",
            ):
                return f"{role_prefix} {keyword_str}".strip() if role_prefix else keyword_str
            return keyword_str

    # Fallback to existing logic for backward compatibility
    kw_blob = _join_related_keywords_for_embedding(obligation)
    if kw_blob and _keywords_only_embedding_enabled():
        if (os.getenv("LEGAL_OCR_EMBED_KEYWORDS_PARTY_PREFIX") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return f"{role_prefix} {kw_blob}".strip() if role_prefix else kw_blob
        return kw_blob

    keywords = obligation.get("related_keywords")
    if isinstance(keywords, list) and keywords:
        keyword_str = " ".join(str(k).strip() for k in keywords if k).strip()
        if keyword_str:
            snip = _owner_responsibility_snippet_for_embedding(obligation)
            body_parts = []
            if duty_cat:
                body_parts.append(duty_cat)
            body_parts.append(keyword_str)
            if snip:
                body_parts.append(snip)
            body = " ".join(body_parts).strip()
            return f"{role_prefix} {body}".strip() if role_prefix else body
    duty = _flatten_field(obligation.get("DutyType"))
    owner_resp = _flatten_field(obligation.get("Owner Responsibility"))[:200]
    base = (duty + " " + owner_resp).strip() or "obligation"
    fallback = (party + " " + base).strip() if party else base
    return f"{role_prefix} {fallback}".strip() if role_prefix else fallback


def _obligation_metadata(obligation: Dict[str, Any], document_name: str, index: int) -> Dict[str, str]:
    """Build ChromaDB-safe metadata (str values only) for one obligation."""
    duty = _flatten_field(obligation.get("DutyType")) or ""
    party = _flatten_field(obligation.get("Responsible Party")) or ""
    responsibility_id = str(obligation.get("responsibility_id") or "")
    citation = obligation.get("citations")
    if citation is None:
        citation = obligation.get("Citation")
    if isinstance(citation, str):
        citation_str = citation[:2000]  # ChromaDB metadata size limit
    else:
        try:
            citation_str = json.dumps(citation, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):
            citation_str = str(citation or "")[:2000]
    
    # Add source category metadata for individual obligations
    source_category = obligation.get("source_category", "") or obligation.get("_category", "")
    obligation_index_in_category = obligation.get("obligation_index_in_category", "")

    # Serialize responsibility_type list
    resp_type = obligation.get("responsibility_type") or []
    resp_type_str = json.dumps(resp_type) if isinstance(resp_type, list) else str(resp_type or "")
    
    metadata = {
        "document_name": document_name,
        "DutyType": duty[:500],
        "Responsible_Party": party[:500],
        "Citation": citation_str,
        "chunk_index": str(index),
        "responsibility_id": responsibility_id[:200],
        "responsibility_type": resp_type_str[:500],
    }
    
    if source_category:
        metadata["source_category"] = str(source_category)[:500]
    if obligation_index_in_category not in ("", None):
        metadata["obligation_index_in_category"] = str(obligation_index_in_category)
    
    return metadata


def _chunk_id(document_name: str, index: int) -> str:
    """Stable unique id for a chunk (same doc + index = same id for upsert)."""
    raw = f"{document_name}|{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _individual_obligation_chunk_id(document_name: str, index: int) -> str:
    """Generate stable unique id for an individual obligation chunk."""
    raw = f"ind_{document_name}|{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_chroma_client(path: Optional[str] = None):
    """Return a persistent ChromaDB client. path defaults to env CHROMA_DB_PATH or ./chroma_db.
    Uses caching to avoid re-initializing the client on every query, which triggers pydantic/dotenv
    parsing and causes recursion issues with deep module hierarchies.
    """
    import chromadb
    
    # Use module-level cache to avoid reinitializing clients
    cache_key = path or os.getenv("CHROMA_DB_PATH", DEFAULT_CHROMA_PATH)
    
    if not hasattr(get_chroma_client, '_cache'):
        get_chroma_client._cache = {}
    
    if cache_key in get_chroma_client._cache:
        return get_chroma_client._cache[cache_key]
    
    p = cache_key
    Path(p).mkdir(parents=True, exist_ok=True)
    
    # Suppress pydantic/dotenv logging during client initialization
    # to avoid recursion issues with exception handling
    old_level = logging.getLogger("pydantic").level
    logging.getLogger("pydantic").setLevel(logging.ERROR)
    try:
        client = chromadb.PersistentClient(path=p)
        get_chroma_client._cache[cache_key] = client
        return client
    finally:
        logging.getLogger("pydantic").setLevel(old_level)


def clear_chroma_cache():
    """Clear the ChromaDB client cache (useful when resetting the database)"""
    if hasattr(get_chroma_client, '_cache'):
        get_chroma_client._cache.clear()
        logger.info("ChromaDB client cache cleared")


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


def index_categories(
    document_name: str,
    category_results: List[Dict[str, Any]],
    chroma_path: Optional[str] = None,
    collection_name: str = "categories",
) -> int:
    """
    Index categories (not individual obligations) for semantic search.
    Each category becomes one chunk with aggregated keywords from all its obligations.
    
    Args:
        document_name: Name of the document
        category_results: List of category dicts with "category" and "obligations" keys
        chroma_path: Path to ChromaDB storage
        collection_name: ChromaDB collection name for categories
        
    Returns:
        Number of categories indexed
    """
    if not category_results:
        logger.warning("No categories to index")
        return 0
        
    try:
        client = get_chroma_client(chroma_path)
        collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=get_embedding_function(),
            metadata={"description": "Legal obligation categories with aggregated keywords"},
        )
        
        # Delete existing categories for this document
        try:
            collection.delete(where={"document_name": document_name})
            logger.info(f"Deleted existing category chunks for document: {document_name}")
        except Exception as e:
            logger.warning(f"Could not delete existing category chunks for {document_name}: {e}")
        
        documents = []
        metadatas = []
        ids = []
        
        for i, category_data in enumerate(category_results):
            if not isinstance(category_data, dict):
                continue
                
            category_name = category_data.get("category", f"Category_{i}")
            obligations_count = len(category_data.get("obligations", []))
            
            # Create chunk text from aggregated keywords
            chunk_text = category_to_keyword_chunk_text(category_data)
            
            if not chunk_text.strip():
                logger.warning(f"Empty chunk text for category: {category_name}")
                continue
            
            documents.append(chunk_text)
            metadatas.append({
                "document_name": document_name,
                "category_name": category_name,
                "obligations_count": str(obligations_count),
                "category_index": str(i),
            })
            ids.append(_category_chunk_id(document_name, i))
        
        if documents:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            logger.info(f"Indexed {len(ids)} category chunks for document: {document_name}")
        
        return len(documents)
        
    except Exception as e:
        logger.error(f"Category vector index error: {e}", exc_info=True)
        raise


def _category_chunk_id(document_name: str, category_index: int) -> str:
    """Generate stable unique id for a category chunk."""
    raw = f"cat_{document_name}|{category_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def index_individual_obligations(
    document_name: str,
    individual_obligations: List[Dict[str, Any]],
    chroma_path: Optional[str] = None,
    collection_name: str = "individual_obligations",
) -> int:
    """
    Index individual obligations (flattened from categories) for fine-grained retrieval.
    Each obligation becomes its own searchable unit with auto-generated keywords.
    
    Args:
        document_name: Name of the document
        individual_obligations: List of individual obligation dicts (from flatten_obligations_for_individual_indexing)
        chroma_path: Path to ChromaDB storage
        collection_name: ChromaDB collection name for individual obligations
        
    Returns:
        Number of individual obligations indexed
    """
    if not individual_obligations:
        logger.warning("No individual obligations to index")
        return 0
    
    try:
        client = get_chroma_client(chroma_path)
        collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=get_embedding_function(),
            metadata={"description": "Individual legal obligations with auto-generated keywords"},
        )
        
        # Delete existing obligations for this document
        try:
            collection.delete(where={"document_name": document_name})
            logger.info(f"Deleted existing individual obligation chunks for document: {document_name}")
        except Exception as e:
            logger.warning(f"Could not delete existing individual obligation chunks for {document_name}: {e}")
        
        documents = []
        metadatas = []
        ids = []
        
        for i, obligation in enumerate(individual_obligations):
            if not isinstance(obligation, dict):
                continue
            
            # Generate embedding text from auto-generated keywords
            chunk_text = obligation_to_keyword_chunk_text(obligation)
            
            if not chunk_text.strip():
                logger.warning(f"Empty chunk text for individual obligation {i}")
                continue
            
            documents.append(chunk_text)
            metadatas.append(_obligation_metadata(obligation, document_name, i))
            ids.append(_individual_obligation_chunk_id(document_name, i))
        
        if documents:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            logger.info(f"Indexed {len(ids)} individual obligation chunks for document: {document_name}")
        
        return len(documents)
        
    except Exception as e:
        logger.error(f"Individual obligation vector index error: {e}", exc_info=True)
        raise


def index_obligations(
    document_name: str,
    consolidated_results: List[Dict[str, Any]],
    chroma_path: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
) -> int:
    """
    Convert each obligation into a chunk and add to ChromaDB only (no Qdrant; chatbot RAG uses a separate store).
    Removes any existing chunks for this document first, then upserts the new set.
    Returns the number of chunks indexed.
    List order must match ``flatten_processing_results_for_index`` output from consolidated JSON
    (chunk_index resolution in the query path).

    Chunk text is obligation_to_keyword_chunk_text (by default: related_keywords only when present).
    Re-run indexing after changing embedding strategy so Chroma matches consolidated JSON.
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


def query_categories(
    query_text: str,
    n_results: int = 10,
    document_name: Optional[str] = None,
    chroma_path: Optional[str] = None,
    collection_name: str = "categories",
) -> List[Dict[str, Any]]:
    """
    Query categories by semantic similarity to aggregated keywords.

    Args:
        query_text: User's search query
        n_results: Maximum number of categories to return (Chroma top-k)
        document_name: Optional filter by document
        chroma_path: Path to ChromaDB storage
        collection_name: ChromaDB collection name for categories

    Returns:
        List of matching categories with metadata and distances
    """
    try:
        client = get_chroma_client(chroma_path)
        collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=get_embedding_function(),
            metadata={"description": "Legal obligation categories with aggregated keywords"},
        )
        
        # Build where filter for document if provided
        where = {"document_name": document_name} if document_name else None

        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        categories = []
        if results and results["ids"] and results["ids"][0]:
            for i, category_id in enumerate(results["ids"][0]):
                meta = (results["metadatas"][0][i] or {}) if results["metadatas"] else {}
                dist = (results["distances"][0][i]) if results.get("distances") and results["distances"][0] else None

                doc = (results["documents"][0][i]) if results.get("documents") and results["documents"][0] else ""
                
                categories.append({
                    "id": category_id,
                    "document_name": meta.get("document_name", ""),
                    "category_name": meta.get("category_name", ""),
                    "obligations_count": int(meta.get("obligations_count", 0)),
                    "category_index": meta.get("category_index", ""),
                    "distance": dist,
                    "keywords": doc,  # The aggregated keywords that were embedded
                })
        
        return categories[:n_results]  # Apply final limit
        
    except Exception as e:
        logger.error(f"Category vector query error: {e}", exc_info=True)
        return []


def query_individual_obligations(
    query_text: str,
    n_results: int = 20,
    document_name: Optional[str] = None,
    chroma_path: Optional[str] = None,
    collection_name: str = "individual_obligations",
    responsible_party: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Query individual obligations by semantic similarity to auto-generated keywords.
    This provides fine-grained retrieval at the obligation level rather than category level.

    Args:
        query_text: User's search query
        n_results: Chroma top-k / max obligations returned
        document_name: Optional filter by document
        chroma_path: Path to ChromaDB storage
        collection_name: ChromaDB collection name for individual obligations
        responsible_party: Optional filter by responsible party

    Returns:
        List of matching individual obligations with metadata and distances
    """
    try:
        client = get_chroma_client(chroma_path)
        collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=get_embedding_function(),
            metadata={"description": "Individual legal obligations with auto-generated keywords"},
        )
        
        # Build where filter: combine document_name and responsible_party if provided
        where = None
        party_where = _party_metadata_where_clause(responsible_party) if responsible_party else {}
        if document_name and party_where:
            where = {"$and": [{"document_name": document_name}, party_where]}
        elif document_name:
            where = {"document_name": document_name}
        elif party_where:
            where = party_where

        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        obligations = []
        if results and results["ids"] and results["ids"][0]:
            for i, obligation_id in enumerate(results["ids"][0]):
                meta = (results["metadatas"][0][i] or {}) if results["metadatas"] else {}
                dist = (results["distances"][0][i]) if results.get("distances") and results["distances"][0] else None

                doc = (results["documents"][0][i]) if results.get("documents") and results["documents"][0] else ""
                _cit_meta = meta.get("Citation", "") or ""
                # #region agent log
                if len(obligations) < 5:
                    try:
                        import json as _agent_json, time as _agent_time

                        with open(
                            r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                            "a",
                            encoding="utf-8",
                        ) as _agent_f:
                            _agent_f.write(
                                _agent_json.dumps(
                                    {
                                        "sessionId": "fe1e15",
                                        "hypothesisId": "H1",
                                        "location": "vector_store.query_individual_obligations",
                                        "message": "chroma hit Citation metadata sample",
                                        "data": {
                                            "doc_name": (meta.get("document_name") or "")[:160],
                                            "citation_head": str(_cit_meta)[:220],
                                            "looks_like_python_repr": str(_cit_meta).lstrip().startswith("[{"),
                                            "chunk_index": str(meta.get("chunk_index", ""))[:24],
                                            "source_category": str(meta.get("source_category", ""))[:120],
                                        },
                                        "timestamp": int(_agent_time.time() * 1000),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                # #endregion
                obligations.append({
                    "id": obligation_id,
                    "document_name": meta.get("document_name", ""),
                    "DutyType": meta.get("DutyType", ""),
                    "Responsible_Party": meta.get("Responsible_Party", ""),
                    "Citation": _cit_meta,
                    "source_category": meta.get("source_category", ""),
                    "obligation_index_in_category": meta.get("obligation_index_in_category", ""),
                    "chunk_index": meta.get("chunk_index", ""),
                    "responsibility_id": meta.get("responsibility_id", ""),
                    "distance": dist,
                    "document": doc,  # The auto-generated keywords that were embedded
                })

        return obligations[:n_results]

    except Exception as e:
        logger.error(f"Individual obligation vector query error: {e}", exc_info=True)
        return []


def query_obligations(
    query_text: str,
    n_results: int = 10,
    document_name: Optional[str] = None,
    chroma_path: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    responsible_party: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Query the vector store by semantic similarity.
    Returns list of dicts with keys: id, document_name, DutyType, Responsible_Party, Citation, distance, document (chunk text).
    If document_name is provided, results are filtered to that document only.
    If responsible_party is provided, results are filtered to that party only (e.g. "Tenant", "Landlord").
    ``n_results`` is the Chroma top-k (no distance cutoff).
    """
    try:
        client = get_chroma_client(chroma_path)
        collection = get_or_create_collection(client, collection_name)
        # Build where filter: combine document_name and responsible_party if provided
        where = None
        party_where = _party_metadata_where_clause(responsible_party) if responsible_party else {}
        if document_name and party_where:
            where = {"$and": [{"document_name": document_name}, party_where]}
        elif document_name:
            where = {"document_name": document_name}
        elif party_where:
            where = party_where
        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        out = []
        if results and results["ids"] and results["ids"][0]:
            for i, id_ in enumerate(results["ids"][0]):
                meta = (results["metadatas"][0][i] or {}) if results["metadatas"] else {}
                dist = (results["distances"][0][i]) if results.get("distances") and results["distances"][0] else None
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
        return out[:n_results]
    except KeyError as e:
        if e.args and e.args[0] == "_type":
            logger.error(
                "ChromaDB cannot read this persistent store (metadata schema mismatch — often after upgrading "
                "chromadb). Delete the folder (e.g. output/chroma_db) and re-run document processing to "
                "re-index obligations, or install a chromadb version matching the DB."
            )
        else:
            logger.error(f"Vector query error: {e}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Vector query error: {e}", exc_info=True)
        return []
