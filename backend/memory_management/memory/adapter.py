"""
Mem0 adapter: single entry point for add and search. Uses config from config_holder (set_memory_config).
Logs each step for debugging the memory flow. No app imports; optional opik tracing.
"""
import ast
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .config import get_mem0_collection_name, get_mem0_config, get_memory_vector_store_provider
from .config_holder import _get
from .mem0_cross_encoder_reranker_patch import apply_mem0_cross_encoder_reranker_patch
from .opik_streaming_body_fix import apply_opik_bedrock_streaming_body_read_fix

logger = logging.getLogger(__name__)

# Optional tracing: no dependency on opik_template for the library
try:
    from opik import opik_context
    from opik_template import trace
except ImportError:
    opik_context = None
    def trace(**kwargs):
        def deco(f):
            return f
        return deco

_memory_instance = None
_faiss_patch_applied = False
_litellm_facts_patch_applied = False
_spacy_download_patch_applied = False


def _preview_text(text: Optional[str], max_len: int = 140) -> str:
    if not text:
        return ""
    t = str(text).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _mem0_item_diag(item: Any, idx: int) -> Dict[str, Any]:
    """Structured view of one Mem0 search hit (for diagnosing ranking / payload shape)."""
    if isinstance(item, dict):
        row: Dict[str, Any] = {"i": idx, "keys": sorted(item.keys())}
        mem = item.get("memory")
        if mem is not None:
            row["memory_preview"] = _preview_text(str(mem), 160)
        for k in ("score", "distance", "rerank_score", "id", "hash", "created_at", "updated_at"):
            if k in item and item[k] is not None:
                row[k] = item[k]
        if item.get("user_id") is not None:
            row["payload_user_id"] = item.get("user_id")
        md = item.get("metadata")
        if isinstance(md, dict):
            if md.get("user_id") is not None:
                row["meta_user_id"] = md.get("user_id")
            row["metadata_keys"] = sorted(md.keys())[:24]
        return row
    return {"i": idx, "kind": type(item).__name__, "preview": _preview_text(str(item), 120)}


def _apply_mem0_faiss_search_patch():
    """
    Fix Mem0 FAISS search when using user_id filter.
    Mem0's FAISS.search() requests fetch_k = limit*2 candidates but then passes `limit`
    to _parse_output(), so only the first `limit` results are considered before filtering.
    If those don't include the requested user_id, search returns 0. We patch so it
    parses all fetch_k results, then filters, then returns up to limit.
    """
    global _faiss_patch_applied
    if _faiss_patch_applied:
        return
    try:
        from mem0.vector_stores import faiss as faiss_module
        FAISS = faiss_module.FAISS
        _original_search = FAISS.search

        def _patched_search(self, query, vectors, limit=5, filters=None):
            fetch_k = max(limit * 10, 50) if filters else limit
            import numpy as np
            query_vectors = np.array(vectors, dtype=np.float32)
            if len(query_vectors.shape) == 1:
                query_vectors = query_vectors.reshape(1, -1)
            if self.normalize_L2 and self.distance_strategy.lower() == "euclidean":
                import faiss
                faiss.normalize_L2(query_vectors)
            scores, indices = self.index.search(query_vectors, fetch_k)
            results = self._parse_output(scores[0], indices[0], limit=fetch_k)
            if filters:
                filtered_results = []
                for result in results:
                    if self._apply_filters(result.payload, filters):
                        filtered_results.append(result)
                        if len(filtered_results) >= limit:
                            break
                results = filtered_results[:limit]
            return results

        FAISS.search = _patched_search
        _faiss_patch_applied = True
        logger.debug("[Memory] Applied FAISS search patch (parse fetch_k before user_id filter)")
    except Exception as e:
        logger.warning("[Memory] Could not apply FAISS search patch: %s", e)


def _search_kwargs():
    """Extra kwargs for Mem0 search: minimal threshold; rerank when reranker is configured."""
    use_rerank = bool(_get("memory_rerank_enabled", True))
    return {"threshold": 1e-9, "rerank": use_rerank}


def _configure_vector_store_loggers() -> None:
    """Raise Mem0 / Qdrant client loggers to INFO so local ops are visible (host can tune root if too noisy)."""
    for name in ("mem0.vector_stores.qdrant", "mem0.memory.main"):
        log = logging.getLogger(name)
        if log.level > logging.INFO:
            log.setLevel(logging.INFO)
    # qdrant_client can be very chatty at DEBUG
    qlog = logging.getLogger("qdrant_client")
    if qlog.level > logging.INFO:
        qlog.setLevel(logging.INFO)


def _ensure_env_for_mem0():
    """Set env vars for Mem0's Bedrock embedder (we always use aws_bedrock for embeddings)."""
    if "AWS_REGION" not in os.environ and _get("aws_default_region"):
        os.environ["AWS_REGION"] = _get("aws_default_region")
    if "AWS_ACCESS_KEY_ID" not in os.environ and _get("aws_access_key_id"):
        os.environ["AWS_ACCESS_KEY_ID"] = _get("aws_access_key_id")
    if "AWS_SECRET_ACCESS_KEY" not in os.environ and _get("aws_secret_access_key"):
        os.environ["AWS_SECRET_ACCESS_KEY"] = _get("aws_secret_access_key")


def _apply_litellm_facts_response_patch():
    """
    Patch Mem0's LiteLLM generate_response for fact extraction (LiteLLM + Bedrock only; LangChain unchanged).
    1. Strip response_format={"type": "json_object"} before calling Bedrock.
    2. Normalize any JSON response to include a "facts" key so Mem0 does not KeyError.
    """
    global _litellm_facts_patch_applied
    if _litellm_facts_patch_applied:
        return
    try:
        from mem0.llms import litellm as litellm_module

        LiteLLMClass = litellm_module.LiteLLM
        _original_generate = LiteLLMClass.generate_response

        def _normalize_facts_response(response: str, _messages: List[Dict[str, str]]) -> str:
            if not response or not response.strip():
                return response
            try:
                parsed = json.loads(response)
            except (json.JSONDecodeError, TypeError):
                try:
                    from mem0.memory.utils import extract_json
                    extracted = extract_json(response)
                    parsed = json.loads(extracted)
                except Exception:
                    return response
            if isinstance(parsed, dict) and "facts" in parsed:
                return response
            if isinstance(parsed, dict):
                facts = (
                    parsed.get("facts")
                    or parsed.get("extracted_facts")
                    or parsed.get("result")
                    or parsed.get("items")
                    or parsed.get("statements")
                    or parsed.get("data")
                )
                if facts is None and len(parsed) == 1:
                    only_val = next(iter(parsed.values()), None)
                    if isinstance(only_val, list):
                        facts = only_val
                if facts is None:
                    content = parsed.get("content")
                    if isinstance(content, str) and content.strip():
                        facts = [s.strip() for s in content.replace("\n", ",").split(",") if s.strip()]
                    elif isinstance(content, list):
                        facts = content
            elif isinstance(parsed, list):
                facts = parsed
            else:
                facts = [str(parsed)] if parsed else []
            if not isinstance(facts, list):
                facts = [facts] if facts else []
            if len(facts) == 0:
                logger.info(
                    "[Memory] LiteLLM facts patch: model returned 0 facts; raw response (truncated): %s",
                    (response.strip()[:400] + "..." if len(response.strip()) > 400 else response.strip()) or "(empty)",
                )
            normalized = json.dumps({"facts": facts})
            logger.info("[Memory] LiteLLM facts patch: normalized response to %d fact(s)", len(facts))
            return normalized

        def _is_fact_extraction_call(messages: List[Dict]) -> bool:
            if not messages or len(messages) < 2:
                return False
            first = messages[0]
            if first.get("role") != "system":
                return False
            content = first.get("content") or ""
            content_str = content if isinstance(content, str) else str(content)
            return (
                "extract" in content_str.lower()
                or "facts" in content_str.lower()
                or "return only" in content_str.lower()
            )

        def _is_update_memory_call(messages: List[Dict]) -> bool:
            if not messages or len(messages) != 1:
                return False
            first = messages[0]
            if first.get("role") != "user":
                return False
            content = first.get("content") or ""
            content_str = content if isinstance(content, str) else str(content)
            return "new retrieved facts" in content_str.lower() and '"memory"' in content_str

        def _extract_facts_from_update_prompt(content: str) -> List[str]:
            for match in re.finditer(r"```\s*(.*?)\s*```", content, re.DOTALL):
                block = match.group(1).strip()
                try:
                    parsed = ast.literal_eval(block)
                    if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                        return parsed
                except (ValueError, SyntaxError):
                    try:
                        parsed = json.loads(block)
                        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                            return parsed
                    except (json.JSONDecodeError, TypeError):
                        continue
            return []

        def _fallback_update_memory_response(messages: List[Dict], response: str) -> str:
            if not _is_update_memory_call(messages):
                return response
            if response and response.strip():
                try:
                    parsed = json.loads(response)
                    if isinstance(parsed, dict) and parsed.get("memory") is not None:
                        return response
                except (json.JSONDecodeError, TypeError):
                    pass
            content = (messages[0].get("content") or "") if messages else ""
            content_str = content if isinstance(content, str) else str(content)
            facts = _extract_facts_from_update_prompt(content_str)
            if not facts:
                return response
            memory_list = [{"text": t, "event": "ADD"} for t in facts]
            out = json.dumps({"memory": memory_list})
            logger.info("[Memory] LiteLLM update-memory fallback: built response with %d ADD(s) from prompt", len(facts))
            return out

        def _patched_generate_response(
            self,
            messages: List[Dict[str, str]],
            response_format=None,
            tools: Optional[List[Dict]] = None,
            tool_choice: str = "auto",
        ):
            effective_format = response_format
            if (
                response_format
                and isinstance(response_format, dict)
                and response_format.get("type") == "json_object"
                and _is_fact_extraction_call(messages)
            ):
                effective_format = None
                logger.debug("[Memory] LiteLLM facts: stripped response_format for fact-extraction call only")
            response = _original_generate(
                self, messages, response_format=effective_format, tools=tools, tool_choice=tool_choice
            )
            if isinstance(response, str):
                if _is_update_memory_call(messages):
                    response = _fallback_update_memory_response(messages, response)
                else:
                    response = _normalize_facts_response(response, messages)
            return response

        LiteLLMClass.generate_response = _patched_generate_response
        _litellm_facts_patch_applied = True
        logger.info("[Memory] Applied LiteLLM facts-response patch (normalize 'facts' key for Mem0)")
    except Exception as e:
        logger.warning("[Memory] Could not apply LiteLLM facts patch: %s", e)


def _apply_spacy_download_guard() -> None:
    """Prevent spaCy model auto-download to avoid runtime sys.exit issues."""
    global _spacy_download_patch_applied
    if _spacy_download_patch_applied:
        return
    try:
        try:
            from mem0.utils import spacy_models as spacy_models_module
        except ImportError:
            from mem0ai.utils import spacy_models as spacy_models_module

        def _ensure_model_available_no_download():
            try:
                import spacy
            except ImportError as exc:
                raise ImportError(
                    "spaCy is not installed. Install it with: pip install mem0ai[nlp]"
                ) from exc
            if not spacy.util.is_package("en_core_web_sm"):
                raise RuntimeError(
                    "spaCy model en_core_web_sm not installed; auto-download disabled. "
                    "Install it manually with: python -m spacy download en_core_web_sm"
                )

        spacy_models_module._ensure_model_available = _ensure_model_available_no_download
        _spacy_download_patch_applied = True
        logger.info("[Memory] Applied spaCy download guard (manual install required)")
    except Exception as e:
        logger.warning("[Memory] Could not apply spaCy download guard: %s", e)


@trace(name="mem0.get_memory", tags=["mem0", "memory"])
def get_memory():
    """Return the shared Mem0 Memory instance (lazy init)."""
    global _memory_instance
    if _memory_instance is not None:
        logger.info(
            "[Memory][diag] get_memory reuse instance_id=%s pid=%s",
            id(_memory_instance),
            os.getpid(),
        )
        return _memory_instance
    if not _get("memory_enabled", True):
        logger.info("[Memory] Disabled (memory_enabled=False)")
        return None
    apply_opik_bedrock_streaming_body_read_fix()
    try:
        logger.info("[Memory] Initializing Mem0...")
        cache_root = os.getenv("HF_HOME") or str(os.path.abspath(os.path.join(os.getcwd(), "output", "hf_cache")))
        os.environ["HF_HOME"] = cache_root
        os.environ["TRANSFORMERS_CACHE"] = cache_root
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = cache_root
        cached_hub = os.path.join(cache_root, "hub", "models--cross-encoder--ms-marco-MiniLM-L-6-v2")
        cached_root = os.path.join(cache_root, "models--cross-encoder--ms-marco-MiniLM-L-6-v2")
        if os.path.isdir(cached_hub) or os.path.isdir(cached_root):
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["HF_DATASETS_OFFLINE"] = "1"
        _ensure_env_for_mem0()
        vs = get_memory_vector_store_provider()
        if vs == "faiss":
            _apply_mem0_faiss_search_patch()
        else:
            logger.info("[Memory] Skipping FAISS search patch (vector_store=%s)", vs)
        _configure_vector_store_loggers()
        try:
            from mem0 import Memory
        except ImportError:
            from mem0ai import Memory
        config = get_mem0_config()
        apply_mem0_cross_encoder_reranker_patch()
        llm_provider = config.get("llm", {}).get("provider", "?")
        if llm_provider == "litellm":
            _apply_litellm_facts_response_patch()
        _apply_spacy_download_guard()
        if vs == "faiss":
            _rel = _get("memory_faiss_path") or "faiss_index/memories"
            logger.info(
                "[Memory] Config: LLM=%s, embedder=aws_bedrock, vector_store=faiss path=%s",
                llm_provider,
                _rel,
            )
            logger.info(
                "[Memory][diag] init Memory pid=%s cwd=%s faiss_dir_abs=%s",
                os.getpid(),
                os.getcwd(),
                os.path.abspath(_rel),
            )
        else:
            qpath = (_get("memory_qdrant_path") or "").strip() or None
            qh = (_get("qdrant_host") or "localhost").strip() or "localhost"
            qp = _get("qdrant_port")
            try:
                qp_int = int(qp) if qp not in (None, "") else 6333
            except (TypeError, ValueError):
                qp_int = 6333
            qcoll = get_mem0_collection_name()
            if qpath:
                logger.info(
                    "[Memory] Config: LLM=%s, embedder=aws_bedrock, vector_store=qdrant path=%s collection=%s",
                    llm_provider,
                    qpath,
                    qcoll,
                )
                logger.info(
                    "[Memory][diag] init Memory pid=%s cwd=%s qdrant_path=%s collection=%r rerank_enabled=%s",
                    os.getpid(),
                    os.getcwd(),
                    qpath,
                    qcoll,
                    _get("memory_rerank_enabled", True),
                )
            else:
                logger.info(
                    "[Memory] Config: LLM=%s, embedder=aws_bedrock, vector_store=qdrant host=%s port=%s collection=%s",
                    llm_provider,
                    qh,
                    qp_int,
                    qcoll,
                )
                logger.info(
                    "[Memory][diag] init Memory pid=%s cwd=%s qdrant=%s:%s collection=%r rerank_enabled=%s",
                    os.getpid(),
                    os.getcwd(),
                    qh,
                    qp_int,
                    qcoll,
                    _get("memory_rerank_enabled", True),
                )
        _memory_instance = Memory.from_config(config)
        logger.info(
            "[Memory] Mem0 initialized successfully instance_id=%s",
            id(_memory_instance),
        )
        return _memory_instance
    except Exception as e:
        logger.warning("[Memory] Init failed: %s", e)
        return None


@trace(name="mem0.search", tags=["mem0", "memory"])
def search(query: str, user_id: str, limit: int = 5) -> List[str]:
    """
    Search memories for this user. Returns list of memory text strings.
    If memory is disabled or user_id missing, returns [].
    """
    logger.info("[Memory] search() called: user_id=%r query=%r limit=%d", user_id, query[:80] + "..." if len(query) > 80 else query, limit)
    if not user_id:
        logger.info("[Memory] search() skipped: no user_id")
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"skipped": "no user_id"})
            except Exception:
                pass
        return []
    mem = get_memory()
    if mem is None:
        logger.info("[Memory] search() skipped: Mem0 not available")
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"skipped": "Mem0 not available"})
            except Exception:
                pass
        return []
    try:
        kwargs = _search_kwargs()
        logger.info(
            "[Memory][search] phase=call_mem0 user_id=%r query_len=%d limit=%d threshold=%s rerank=%s instance_id=%s pid=%s",
            user_id,
            len(query or ""),
            limit,
            kwargs.get("threshold"),
            kwargs.get("rerank"),
            id(mem),
            os.getpid(),
        )
        result = mem.search(
            query=query,
            limit=limit,
            filters={"user_id": user_id},
            **kwargs,
        )
        if isinstance(result, dict):
            items = result.get("results", result.get("memories", []))
            logger.info(
                "[Memory][search] phase=mem0_response type=dict top_keys=%s raw_count=%d",
                list(result.keys()),
                len(items),
            )
        else:
            items = result if isinstance(result, list) else []
            logger.info(
                "[Memory][search] phase=mem0_response type=%s raw_count=%d",
                type(result).__name__,
                len(items),
            )
        diag_cap = min(len(items), max(25, limit + 5))
        logger.info(
            "[Memory][search] phase=mem0_hits_diag %s",
            json.dumps([_mem0_item_diag(items[i], i) for i in range(diag_cap)], default=str),
        )
        if not result or not items:
            logger.info(
                "[Memory][search] phase=empty_mem0_vector_hits -> keyword_fallback branch=1",
            )
            out = _search_fallback_by_keywords(query, user_id, limit)
            if opik_context:
                try:
                    opik_context.update_current_span(metadata={"result_count": len(out), "source": "keyword_fallback"})
                except Exception:
                    pass
            return out
        texts = []
        skipped_other_user = 0
        for item in items:
            if isinstance(item, dict):
                item_user_id = item.get("user_id") or (item.get("metadata") or {}).get("user_id")
                if item_user_id != user_id:
                    skipped_other_user += 1
                    continue
                if "memory" in item:
                    texts.append(item["memory"])
            elif isinstance(item, str):
                texts.append(item)
        logger.info(
            "[Memory][search] phase=parse_items kept_text_count=%d skipped_other_user=%d (semantic order preserved)",
            len(texts),
            skipped_other_user,
        )
        if items and not texts:
            logger.warning(
                "[Memory][search] phase=no_memory_key_in_items raw_count=%d first_item_diag=%s -> keyword_fallback branch=2",
                len(items),
                json.dumps(_mem0_item_diag(items[0], 0), default=str) if items else {},
            )
            out = _search_fallback_by_keywords(query, user_id, limit)
            if opik_context:
                try:
                    opik_context.update_current_span(metadata={"result_count": len(out), "source": "keyword_fallback"})
                except Exception:
                    pass
            return out
        texts = texts[:limit]
        logger.info(
            "[Memory][search] phase=exit_semantic_top_k applied_limit=%d final_count=%s previews=%s",
            limit,
            len(texts),
            json.dumps([_preview_text(t, 200) for t in texts], default=str),
        )
        logger.info("[Memory] search() found %d memory item(s) for user_id=%r", len(texts), user_id)
        for i, t in enumerate(texts):
            logger.debug("[Memory]   [%d] %s", i + 1, (t[:100] + "..." if len(t) > 100 else t))
        if opik_context:
            try:
                opik_context.update_current_span(
                    metadata={
                        "query_preview": (query or "")[:200],
                        "user_id": user_id,
                        "result_count": len(texts),
                        "first_preview": (texts[0] or "")[:150] if texts else "",
                    }
                )
            except Exception:
                pass
        return texts
    except Exception as e:
        logger.warning("[Memory][search] phase=exception user_id=%r err=%s", user_id, e, exc_info=True)
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"error": str(e)[:200]})
            except Exception:
                pass
        return []


def _search_fallback_by_keywords(query: str, user_id: str, limit: int) -> List[str]:
    """Fallback when Mem0 vector search returns 0: get all memories for user and rank by query-word overlap."""
    logger.info(
        "[Memory][search] phase=keyword_fallback_enter user_id=%r limit=%d query_preview=%r",
        user_id,
        limit,
        _preview_text(query, 200),
    )
    try:
        pool_limit = max(limit * 3, 30)
        all_texts = get_all(user_id, limit=pool_limit)
        logger.info(
            "[Memory][search] phase=keyword_fallback_pool get_all_count=%d (pool_limit=%d)",
            len(all_texts),
            pool_limit,
        )
        if not all_texts:
            logger.info("[Memory][search] phase=keyword_fallback_exit empty_pool")
            return []
        query_words = {w.lower() for w in query.split() if len(w) > 1}
        if not query_words:
            out = list(dict.fromkeys(all_texts))[:limit]
            logger.info(
                "[Memory][search] phase=keyword_fallback_exit no_query_tokens took_head count=%d",
                len(out),
            )
            return out
        scored = []
        for text in all_texts:
            low = text.lower()
            score = sum(1 for w in query_words if w in low)
            scored.append((score, text))
        scored.sort(key=lambda x: (-x[0], x[1]))
        seen = set()
        out = []
        for sc, text in scored:
            if text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                break
        top_scores = [sc for sc, _ in scored[: min(5, len(scored))]]
        logger.info(
            "[Memory][search] phase=keyword_fallback_exit count=%d top_overlap_scores_sample=%s previews=%s",
            len(out),
            top_scores,
            json.dumps([_preview_text(t, 160) for t in out], default=str),
        )
        return out
    except Exception as e:
        logger.warning("[Memory][search] phase=keyword_fallback_exception err=%s", e, exc_info=True)
        return []


def get_all(user_id: str, limit: int = 100) -> List[str]:
    """List all stored memory texts for a user (for debugging). Returns list of memory strings."""
    logger.info("[Memory][get_all] enter user_id=%r limit=%d", user_id, limit)
    if not user_id:
        logger.info("[Memory][get_all] exit skip no user_id")
        return []
    mem = get_memory()
    if mem is None:
        logger.info("[Memory][get_all] exit skip mem None")
        return []
    try:
        result = mem.get_all(filters={"user_id": user_id}, limit=limit)
        items = result.get("results", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
        logger.info(
            "[Memory][get_all] mem0_raw type=%s keys=%s item_count=%d",
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else None,
            len(items),
        )
        out = [item.get("memory", "") for item in items if isinstance(item, dict) and item.get("memory")]
        logger.info(
            "[Memory][get_all] exit count=%d first_previews=%s",
            len(out),
            json.dumps([_preview_text(t, 120) for t in out[:8]], default=str),
        )
        return out
    except Exception as e:
        logger.warning("[Memory][get_all] exception user_id=%r err=%s", user_id, e, exc_info=True)
        return []


@trace(name="mem0.add", tags=["mem0", "memory"])
def add(messages: List[Dict[str, str]], user_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    """
    Add a conversation turn to memory.
    With infer=True: Mem0 extracts facts via LLM. With infer=False: stores raw messages via embedder only.
    No-op if memory disabled or user_id missing.
    """
    logger.info("[Memory] add() called: user_id=%r messages=%d", user_id, len(messages))
    if not user_id:
        logger.info("[Memory] add() skipped: no user_id")
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"skipped": "no user_id"})
            except Exception:
                pass
        return
    mem = get_memory()
    if mem is None:
        logger.info("[Memory] add() skipped: Mem0 not available")
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"skipped": "Mem0 not available"})
            except Exception:
                pass
        return
    from .config import (
        _use_azure_openai_for_memory,
        _use_langchain_nova_for_memory,
        _use_litellm_for_memory,
    )

    use_langchain_nova = _use_langchain_nova_for_memory()
    use_litellm = _use_litellm_for_memory()
    infer = (
        _use_azure_openai_for_memory()
        or use_langchain_nova
        or use_litellm
        or _get("memory_infer", False)
    )
    logger.info("[Memory] add() infer=%s (extract facts via LLM=%s)", infer, infer)
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = (m.get("content") or "")[:60]
        logger.debug("[Memory]   message[%d] role=%s content=%r...", i, role, content)
    try:
        logger.info(
            "[Memory][add] phase=mem0_add_call user_id=%r infer=%s metadata_keys=%s",
            user_id,
            infer,
            sorted((metadata or {}).keys()),
        )
        result = mem.add(messages, user_id=user_id, metadata=metadata or {}, infer=infer)
        logger.info(
            "[Memory][add] phase=mem0_add_response type=%s dict_keys=%s",
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else None,
        )
        results = result.get("results", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
        n = len(results)
        logger.info("[Memory] add() completed for user_id=%r: Mem0 stored %d memory item(s)", user_id, n)
        for i, item in enumerate(results[:3]):
            if isinstance(item, dict) and "memory" in item:
                logger.info("[Memory]   stored[%d] %s", i + 1, (item["memory"][:80] + "..." if len(item.get("memory", "")) > 80 else item.get("memory", "")))
        if n > 3:
            logger.info("[Memory]   ... and %d more", n - 3)
        if opik_context:
            try:
                first_content = ""
                if messages:
                    c = (messages[0].get("content") or "")
                    first_content = (c[:100] + "...") if len(c) > 100 else c
                opik_context.update_current_span(
                    metadata={
                        "user_id": user_id,
                        "messages_count": len(messages),
                        "first_content_preview": first_content,
                        "infer": infer,
                        "stored_count": n,
                        "first_memory_preview": (results[0]["memory"] or "")[:150] if results and isinstance(results[0], dict) and results[0].get("memory") else "",
                    }
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning("[Memory] add() failed: %s", e, exc_info=True)
        if opik_context:
            try:
                opik_context.update_current_span(metadata={"error": str(e)[:200]})
            except Exception:
                pass


def _clear_qdrant_collection() -> bool:
    """Drop Mem0 collection in Qdrant (fresh start)."""
    try:
        from qdrant_client import QdrantClient
    except ImportError as e:
        logger.warning("[Memory][clear] qdrant-client not installed: %s", e)
        return False
    collection = get_mem0_collection_name()
    path = (_get("memory_qdrant_path") or "").strip() or None
    url = (_get("qdrant_url") or "").strip() or None
    api_key = (_get("qdrant_api_key") or "").strip() or None
    host = (_get("qdrant_host") or "localhost").strip() or "localhost"
    try:
        port = int(_get("qdrant_port") or 6333)
    except (TypeError, ValueError):
        port = 6333
    try:
        if path:
            client = QdrantClient(path=path)
            logger.info("[Memory][clear] qdrant client mode=path path=%r collection=%r", path, collection)
        elif url and api_key:
            client = QdrantClient(url=url, api_key=api_key)
            logger.info("[Memory][clear] qdrant client mode=url collection=%r", collection)
        else:
            client = QdrantClient(host=host, port=port)
            logger.info(
                "[Memory][clear] qdrant client host=%r port=%s collection=%r",
                host,
                port,
                collection,
            )
        existing = {c.name for c in client.get_collections().collections}
        if collection in existing:
            client.delete_collection(collection_name=collection)
            logger.info("[Memory][clear] deleted Qdrant collection %r", collection)
        else:
            logger.info("[Memory][clear] Qdrant collection %r not present (already clean)", collection)
        return True
    except Exception as e:
        logger.warning("[Memory][clear] Qdrant delete failed: %s", e)
        return False


def clear_memory_index() -> bool:
    """
    Reset memory index: Qdrant collection drop, or on-disk FAISS files when using FAISS.
    Clears the in-process Mem0 singleton. Returns True on success or already-empty store.
    """
    global _memory_instance
    logger.info("[Memory][clear] phase=start reset_singleton pid=%s", os.getpid())
    _memory_instance = None
    vs = get_memory_vector_store_provider()
    if vs == "qdrant":
        return _clear_qdrant_collection()
    coll = get_mem0_collection_name()
    path = _get("memory_faiss_path") or "faiss_index/memories"
    base = os.path.abspath(path)
    faiss_file = os.path.join(base, f"{coll}.faiss")
    pkl_file = os.path.join(base, f"{coll}.pkl")
    removed = []
    try:
        if os.path.isfile(faiss_file):
            os.remove(faiss_file)
            removed.append(faiss_file)
            logger.info("[Memory] Removed FAISS index: %s", faiss_file)
        if os.path.isfile(pkl_file):
            os.remove(pkl_file)
            removed.append(pkl_file)
            logger.info("[Memory] Removed metadata: %s", pkl_file)
        if not removed:
            logger.info("[Memory] No existing index at %s (already clean)", base)
        return True
    except Exception as e:
        logger.warning("[Memory] clear_memory_index failed: %s", e)
        return False
