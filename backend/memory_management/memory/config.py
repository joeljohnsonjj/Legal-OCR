"""
Mem0 configuration: Azure OpenAI for LLM (extraction) when set, else LangChain + Bedrock Nova.
Embedder uses Bedrock; vector store is Qdrant (default) or FAISS (legacy).
Uses config from config_holder (set_memory_config); no app imports.
"""
import logging
from typing import Any, Dict, Optional

from .config_holder import _get

logger = logging.getLogger(__name__)


def _use_azure_openai_for_memory() -> bool:
    """True when Azure OpenAI credentials are set for Mem0's LLM (enables automatic extraction)."""
    key = (_get("azure_openai_key") or "").strip()
    endpoint = (_get("azure_openai_endpoint") or "").strip()
    deployment = (_get("azure_openai_deployment_name") or "").strip()
    return bool(key and endpoint and deployment)


def _use_litellm_for_memory() -> bool:
    """True when Mem0 LLM should use LiteLLM (no Azure and MEMORY_LLM_PROVIDER=litellm)."""
    if _use_azure_openai_for_memory():
        return False
    provider = (_get("memory_llm_provider") or "").strip().lower()
    return provider == "litellm"


def _use_langchain_nova_for_memory() -> bool:
    """True when Mem0 LLM is LangChain + Bedrock Nova (no Azure, not LiteLLM); enables extraction via Nova."""
    return not _use_azure_openai_for_memory() and not _use_litellm_for_memory()


def _litellm_fact_extraction_prompt() -> str:
    """Strict fact-extraction prompt so LiteLLM/Bedrock returns JSON with 'facts' key (avoids KeyError in Mem0)."""
    return """
Extract only factual statements about the user from the conversation (role, location, preferences, manager, etc.).
Return ONLY a single JSON object with exactly one key "facts" whose value is an array of strings. No other keys, no markdown, no explanation.

Examples:

Input: Hi.
Output: {"facts": []}

Input: I work in Engineering and I often ask about leave policy.
Output: {"facts": ["Works in Engineering", "Often asks about leave policy"]}

Input: I'm based in Bangalore and I want to know about health benefits.
Output: {"facts": ["Based in Bangalore", "Wants to know about health benefits"]}

Input: My manager is Sarah. I prefer WFH twice a week.
Output: {"facts": ["Manager is Sarah", "Prefers WFH twice a week"]}

Return the JSON object only, with the key "facts" and an array of short fact strings.
"""


def get_memory_vector_store_provider() -> str:
    """Return ``faiss`` or ``qdrant`` (default ``qdrant``)."""
    v = (_get("memory_vector_store") or "qdrant").strip().lower()
    return v if v in ("faiss", "qdrant") else "qdrant"


def get_mem0_collection_name() -> str:
    """
    Qdrant / FAISS collection name.
    ``MEM0_QDRANT_COLLECTION`` if set; else ``MEM0_DEFAULT_COLLECTION`` (default ``mem0_memories``).
    """
    raw = (_get("mem0_qdrant_collection") or "").strip()
    if raw:
        return raw
    fallback = (_get("mem0_default_collection") or "mem0_memories").strip()
    return fallback or "mem0_memories"


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_qdrant_vector_store() -> Dict[str, Any]:
    path = (_get("memory_qdrant_path") or "").strip() or None
    host = (_get("qdrant_host") or "localhost").strip() or "localhost"
    port = _coerce_int(_get("qdrant_port"), 6333)
    collection = get_mem0_collection_name()
    url = (_get("qdrant_url") or "").strip() or None
    api_key = (_get("qdrant_api_key") or "").strip() or None
    on_disk = bool(_get("qdrant_on_disk", False))

    cfg: Dict[str, Any] = {
        "collection_name": collection,
        "embedding_model_dims": 1024,
        "on_disk": on_disk,
    }
    if path:
        cfg["path"] = path
        logger.info(
            "[Memory config] vector_store=qdrant mode=path collection=%r embedding_dims=1024 path=%r",
            collection,
            path,
        )
    elif url and api_key:
        cfg["url"] = url
        cfg["api_key"] = api_key
        logger.info(
            "[Memory config] vector_store=qdrant mode=url collection=%r embedding_dims=1024",
            collection,
        )
    else:
        cfg["host"] = host
        cfg["port"] = port
        logger.info(
            "[Memory config] vector_store=qdrant host=%r port=%s collection=%r embedding_dims=1024 on_disk=%s",
            host,
            port,
            collection,
            on_disk,
        )
    return {"provider": "qdrant", "config": cfg}


def _build_faiss_vector_store() -> Dict[str, Any]:
    path = _get("memory_faiss_path") or "faiss_index/memories"
    collection = get_mem0_collection_name()
    logger.info(
        "[Memory config] vector_store=faiss path=%r collection=%r embedding_dims=1024 cosine+L2",
        path,
        collection,
    )
    return {
        "provider": "faiss",
        "config": {
            "path": path,
            "collection_name": collection,
            "embedding_model_dims": 1024,
            "distance_strategy": "cosine",
            "normalize_L2": True,
        },
    }


def _build_reranker_block() -> Optional[Dict[str, Any]]:
    if not _get("memory_rerank_enabled", True):
        logger.info("[Memory config] reranker disabled (memory_rerank_enabled=false)")
        return None
    primary = (_get("memory_rerank_model") or "").strip()
    fallback = (_get("mem0_default_rerank_model") or "cross-encoder/ms-marco-MiniLM-L-6-v2").strip()
    model = primary or fallback or "cross-encoder/ms-marco-MiniLM-L-6-v2"
    device_raw = _get("memory_rerank_device")
    device = (str(device_raw).strip() or None) if device_raw not in (None, "") else None
    batch_size = _coerce_int(_get("memory_rerank_batch_size"), 32)
    show_bar = bool(_get("memory_rerank_show_progress", False))
    logger.info(
        "[Memory config] reranker=sentence_transformer model=%r device=%r batch_size=%s",
        model,
        device,
        batch_size,
    )
    return {
        "provider": "sentence_transformer",
        "config": {
            "model": model,
            "device": device,
            "batch_size": batch_size,
            "show_progress_bar": show_bar,
        },
    }


def get_mem0_config() -> dict:
    """Build Mem0 config: Azure when set, else LiteLLM or LangChain Bedrock Nova; Bedrock embedder; Qdrant or FAISS."""
    litellm_extra = {}
    if _use_azure_openai_for_memory():
        logger.info("[Memory config] Using Azure OpenAI for LLM (automatic extraction enabled)")
        api_version = _get("azure_openai_api_version") or "2024-02-15-preview"
        endpoint = (_get("azure_openai_endpoint") or "").strip().rstrip("/")
        llm_config = {
            "provider": "azure_openai",
            "config": {
                "model": _get("azure_openai_deployment_name"),
                "temperature": 0.2,
                "max_tokens": 2000,
                "azure_kwargs": {
                    "azure_deployment": _get("azure_openai_deployment_name"),
                    "azure_endpoint": endpoint,
                    "api_key": _get("azure_openai_key"),
                    "api_version": api_version,
                },
            },
        }
    elif _use_litellm_for_memory():
        model = (_get("memory_litellm_model") or "").strip()
        if not model:
            model = (_get("llm_model") or "").strip()
        if not model:
            model = "bedrock/" + (_get("aws_bedrock_model") or "us.amazon.nova-pro-v1:0")
        logger.info("[Memory config] Using LiteLLM for LLM (model=%s, extraction enabled)", model)
        llm_config = {
            "provider": "litellm",
            "config": {
                "model": model,
                "temperature": 0.2,
                "max_tokens": 2000,
            },
        }
        litellm_custom_prompt = _litellm_fact_extraction_prompt()
        litellm_extra = {"custom_fact_extraction_prompt": litellm_custom_prompt}
    else:
        logger.info("[Memory config] Using LangChain + Bedrock Nova for LLM (extraction enabled)")
        from langchain_aws import ChatBedrockConverse

        llm = ChatBedrockConverse(
            model=_get("aws_bedrock_model"),
            temperature=0.2,
            max_tokens=2000,
            region_name=_get("aws_default_region") or "us-east-1",
        )
        llm_config = {
            "provider": "langchain",
            "config": {"model": llm},
        }

    vs_provider = get_memory_vector_store_provider()
    vector_store = _build_faiss_vector_store() if vs_provider == "faiss" else _build_qdrant_vector_store()

    config: Dict[str, Any] = {
        "llm": llm_config,
        "embedder": {
            "provider": "aws_bedrock",
            "config": {
                "model": _get("embedding_model_id"),
            },
        },
        "vector_store": vector_store,
    }
    reranker = _build_reranker_block()
    if reranker:
        config["reranker"] = reranker
    config.update(litellm_extra)
    return config
