"""
LiteLLM wrapper: same interface as llm_client (generate_content, generate_content_async, generate_content_stream).
Use when LITELLM_MODEL is set in .env to switch providers without code changes.

Examples:
  LITELLM_MODEL=gemini/gemini-2.5-flash-lite
  LITELLM_MODEL=azure/gpt-4o-mini
  LITELLM_MODEL=bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0

- Azure: set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_API_VERSION; model = azure/<deployment_name>.
- Gemini: set GEMINI_API_KEY or GOOGLE_API_KEY.
- AWS Bedrock (Claude): set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION (or AWS_REGION_NAME);
  model = bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0 (or other Bedrock Claude model IDs).
"""
import os
from typing import AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv()


class LiteLLMResponse:
    """Response object with .text for compatibility with existing code."""

    def __init__(self, text: str):
        self.text = text


def _litellm_model() -> Optional[str]:
    return (os.getenv("LITELLM_MODEL") or "").strip() or None


def _sanitize_header(s: str) -> str:
    """Remove newlines/carriage returns so value is safe for HTTP headers."""
    return (s or "").replace("\r", "").replace("\n", "").strip()


# Claude 3 Haiku max is 4096; Claude 3.5+ models support up to 8192+
_BEDROCK_MAX_TOKENS = 4096
_DEFAULT_MAX_TOKENS = 8192


def _safe_max_tokens(model: str) -> int:
    """Return a safe max_tokens value respecting the model's context limit."""
    env_val = int(os.getenv("AZURE_OPENAI_MAX_TOKENS") or os.getenv("GEMINI_MAX_OUTPUT_TOKENS") or "0")
    if env_val:
        return env_val
    if (model or "").strip().lower().startswith("bedrock/"):
        return _BEDROCK_MAX_TOKENS
    return _DEFAULT_MAX_TOKENS


def _azure_kwargs(model: str):
    """If model is azure/<deployment>, add Azure env vars for LiteLLM. Values sanitized for headers."""
    if not (model or "").strip().lower().startswith("azure/"):
        return {}
    base = _sanitize_header(os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_API_BASE") or "").rstrip("/")
    key = _sanitize_header(os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_API_KEY") or "")
    version = _sanitize_header(os.getenv("AZURE_OPENAI_API_VERSION") or os.getenv("AZURE_API_VERSION") or "2024-02-15-preview")
    out = {}
    if base:
        out["api_base"] = base
    if key:
        out["api_key"] = key
    if version:
        out["api_version"] = version
    return out


def _bedrock_kwargs(model: str):
    """If model is bedrock/..., pass region + any temporary session credentials (AssumeRole).
    Values sanitized for headers."""
    if not (model or "").strip().lower().startswith("bedrock/"):
        return {}
    region = _sanitize_header(os.getenv("AWS_REGION_NAME") or os.getenv("AWS_REGION") or "")
    out = {}
    if region:
        out["aws_region_name"] = region
    # Pass temporary credentials from AssumeRole if available (set by aws_parameter_loader.init_llm_env)
    access_key = _sanitize_header(os.getenv("AWS_ACCESS_KEY_ID") or "")
    secret_key = _sanitize_header(os.getenv("AWS_SECRET_ACCESS_KEY") or "")
    session_token = _sanitize_header(os.getenv("AWS_SESSION_TOKEN") or "")
    if access_key:
        out["aws_access_key_id"] = access_key
    if secret_key:
        out["aws_secret_access_key"] = secret_key
    if session_token:
        out["aws_session_token"] = session_token
    return out


def bedrock_litellm_kwargs(model: str) -> dict:
    """
    Extra kwargs for ``litellm.completion`` / ``acompletion`` when ``model`` is ``bedrock/...``.

    Uses ``AWS_REGION*`` and ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``
    from the environment (including credentials from ``aws_parameter_loader.init_llm_env`` / Parameter Store + AssumeRole).
    """
    return _bedrock_kwargs(model)


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> LiteLLMResponse:
    """Sync completion via LiteLLM. Returns object with .text."""
    import litellm

    model_name = model or _litellm_model()
    if not model_name:
        raise ValueError("LITELLM_MODEL must be set in .env when using LiteLLM")

    kwargs = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_output_tokens or _safe_max_tokens(model_name),
    }
    # For Bedrock, use IAM only (do not pass api_key unless it's a valid Bedrock API key with AWS prefix)
    key = (api_key or os.getenv("API_KEY") or "").replace("\r", "").replace("\n", "").strip()
    if key and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["api_key"] = key
    # response_format=json_object is NOT supported by Bedrock Converse API; skip it for bedrock/ models
    if response_mime_type == "application/json" and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["response_format"] = {"type": "json_object"}
    kwargs.update(_azure_kwargs(model_name))
    kwargs.update(_bedrock_kwargs(model_name))

    response = litellm.completion(**kwargs)
    choice = (response.choices or [None])[0]
    if choice is None:
        content = ""
    else:
        msg = getattr(choice, "message", None) or (choice.get("message") if isinstance(choice, dict) else None)
        content = (getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None) or "")
    return LiteLLMResponse(text=content)


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> LiteLLMResponse:
    """Async completion via LiteLLM. Returns object with .text."""
    import litellm

    model_name = model or _litellm_model()
    if not model_name:
        raise ValueError("LITELLM_MODEL must be set in .env when using LiteLLM")

    kwargs = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_output_tokens or _safe_max_tokens(model_name),
    }
    key = (api_key or os.getenv("API_KEY") or "").replace("\r", "").replace("\n", "").strip()
    if key and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["api_key"] = key
    # response_format=json_object is NOT supported by Bedrock Converse API; skip it for bedrock/ models
    if response_mime_type == "application/json" and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["response_format"] = {"type": "json_object"}
    kwargs.update(_azure_kwargs(model_name))
    kwargs.update(_bedrock_kwargs(model_name))

    response = await litellm.acompletion(**kwargs)
    choice = (response.choices or [None])[0]
    if choice is None:
        content = ""
    else:
        msg = getattr(choice, "message", None) or (choice.get("message") if isinstance(choice, dict) else None)
        content = (getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None) or "")
    return LiteLLMResponse(text=content)


async def generate_content_stream(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """Async stream of tokens via LiteLLM. Yields text chunks."""
    import litellm

    model_name = model or _litellm_model()
    if not model_name:
        raise ValueError("LITELLM_MODEL must be set in .env when using LiteLLM")

    kwargs = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_output_tokens or _safe_max_tokens(model_name),
        "stream": True,
    }
    key = (api_key or os.getenv("API_KEY") or "").replace("\r", "").replace("\n", "").strip()
    if key and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["api_key"] = key
    # response_format=json_object is NOT supported by Bedrock Converse API; skip it for bedrock/ models
    if response_mime_type == "application/json" and not (model_name or "").strip().lower().startswith("bedrock/"):
        kwargs["response_format"] = {"type": "json_object"}
    kwargs.update(_azure_kwargs(model_name))
    kwargs.update(_bedrock_kwargs(model_name))

    stream = await litellm.acompletion(**kwargs)
    async for chunk in stream:
        if not chunk or not getattr(chunk, "choices", None):
            continue
        c0 = chunk.choices[0]
        delta = getattr(c0, "delta", None) or (c0.get("delta") if isinstance(c0, dict) else None) or {}
        content = (getattr(delta, "content", None) or (delta.get("content") if isinstance(delta, dict) else None) or "")
        if content:
            yield content
