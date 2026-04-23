"""
Mem0 loads cross-encoder rerank models with SentenceTransformer(); those ids must use CrossEncoder.
We patch SentenceTransformerReranker.__init__ once per process (no vendored mem0 package).

Toggle: mem0_patch_cross_encoder_reranker / MEM0_PATCH_CROSS_ENCODER_RERANKER (default true).
"""
import logging
import os
from typing import Any, Dict, Union

logger = logging.getLogger(__name__)

_patch_applied = False


def _patch_flag_enabled() -> bool:
    """True unless explicitly disabled via config holder or env."""
    try:
        from .config_holder import _get

        return bool(_get("mem0_patch_cross_encoder_reranker", True))
    except Exception:
        raw = os.environ.get("MEM0_PATCH_CROSS_ENCODER_RERANKER", "true").strip().lower()
        return raw not in ("0", "false", "no", "off")


def _is_cross_encoder_model_id(model_id: str) -> bool:
    return "cross-encoder" in (model_id or "").strip().lower()


def apply_mem0_cross_encoder_reranker_patch() -> None:
    """Idempotent: replace Mem0 reranker __init__ so cross-encoder/* models use CrossEncoder."""
    global _patch_applied

    if _patch_applied:
        return
    if not _patch_flag_enabled():
        logger.info("[Memory] Mem0 CrossEncoder reranker patch skipped (mem0_patch_cross_encoder_reranker=false)")
        _patch_applied = True
        return
    try:
        from mem0.configs.rerankers.base import BaseRerankerConfig
        from mem0.configs.rerankers.sentence_transformer import SentenceTransformerRerankerConfig
        from mem0.reranker import sentence_transformer_reranker as st_mod
    except Exception as e:
        logger.warning("[Memory] CrossEncoder patch: mem0 imports failed: %s", e)
        _patch_applied = True
        return

    ST = st_mod.SentenceTransformerReranker
    _orig_init = ST.__init__

    def _patched_init(self, config: Union[BaseRerankerConfig, SentenceTransformerRerankerConfig, Dict, Any]):
        if not _patch_flag_enabled():
            return _orig_init(self, config)

        try:
            from sentence_transformers import CrossEncoder, SentenceTransformer
        except ImportError:
            logger.warning("[Memory] CrossEncoder patch: sentence-transformers missing; using Mem0 default init")
            return _orig_init(self, config)

        if not getattr(st_mod, "SENTENCE_TRANSFORMERS_AVAILABLE", True):
            return _orig_init(self, config)

        if isinstance(config, dict):
            cfg = SentenceTransformerRerankerConfig(**config)
        elif isinstance(config, BaseRerankerConfig) and not isinstance(config, SentenceTransformerRerankerConfig):
            cfg = SentenceTransformerRerankerConfig(
                provider=getattr(config, "provider", "sentence_transformer"),
                model=getattr(config, "model", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
                api_key=getattr(config, "api_key", None),
                top_k=getattr(config, "top_k", None),
                device=None,
                batch_size=32,
                show_progress_bar=False,
            )
        elif isinstance(config, SentenceTransformerRerankerConfig):
            cfg = config
        else:
            return _orig_init(self, config)

        self.config = cfg
        model_id = (self.config.model or "").strip()
        device = self.config.device

        cache_root = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME")
        cached_cross_encoder = False
        if cache_root:
            cached_cross_encoder = os.path.isdir(
                os.path.join(cache_root, "hub", "models--cross-encoder--ms-marco-MiniLM-L-6-v2")
            ) or os.path.isdir(os.path.join(cache_root, "models--cross-encoder--ms-marco-MiniLM-L-6-v2"))
        if model_id and _is_cross_encoder_model_id(model_id):
            if cache_root:
                self.model = CrossEncoder(
                    model_id,
                    device=device,
                    cache_folder=cache_root,
                    local_files_only=cached_cross_encoder,
                )
            else:
                self.model = CrossEncoder(model_id, device=device)
            logger.info("[Memory] Mem0 reranker: CrossEncoder loaded for %r", model_id)
        elif model_id:
            if cache_root:
                self.model = SentenceTransformer(
                    model_id,
                    device=device,
                    cache_folder=cache_root,
                    local_files_only=cached_cross_encoder,
                )
            else:
                self.model = SentenceTransformer(model_id, device=device)
            logger.info("[Memory] Mem0 reranker: SentenceTransformer loaded for %r", model_id)
        else:
            return _orig_init(self, config)

    ST.__init__ = _patched_init
    _patch_applied = True
    logger.info("[Memory] Applied Mem0 SentenceTransformerReranker CrossEncoder __init__ patch")


def reset_patch_state_for_tests() -> None:
    """Testing only: allow re-applying patch in the same interpreter."""
    global _patch_applied  # noqa: PLW0603

    _patch_applied = False
