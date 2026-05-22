"""
LLM client dispatcher: Azure OpenAI, AWS Bedrock (Claude), or Gemini.
Same interface: generate_content() and generate_content_async() return an object with .text.
"""
import os
from typing import AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv()


def _use_azure_openai() -> bool:
    """True if we should use Azure OpenAI."""
    return os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")


def _raw_llm_or_bedrock_model() -> str:
    """Non-empty model string from LLM_MODEL or BEDROCK_MODEL_ID (trimmed, not lowercased)."""
    return (os.getenv("LLM_MODEL") or os.getenv("BEDROCK_MODEL_ID") or "").strip()


def _looks_like_bedrock_foundation_model_id(model_raw: str) -> bool:
    """
    True when ``model_raw`` looks like an AWS Bedrock base model id (no ``bedrock/`` prefix).
    Users often set only ``BEDROCK_MODEL_ID=anthropic.claude-...`` or ``LLM_MODEL=anthropic...``;
    without this, routing incorrectly fell through to Gemini and required GEMINI_API_KEY.
    """
    s = (model_raw or "").strip().lower()
    if not s or s.startswith("bedrock/"):
        return False
    prefixes = (
        "anthropic.",
        "us.anthropic.",
        "eu.anthropic.",
        "apac.anthropic.",
        "meta.",
        "amazon.",
        "mistral.",
        "cohere.",
        "ai21.",
    )
    return any(s.startswith(p) for p in prefixes)


def use_bedrock_llm() -> bool:
    """
    True when Bedrock should be used. Disabled if USE_AZURE_OPENAI is true (Azure takes precedence).

    Bedrock is selected when any of:
    - ``USE_BEDROCK`` is true/1/yes
    - ``LLM_MODEL`` starts with ``bedrock/``
    - ``LLM_MODEL`` or ``BEDROCK_MODEL_ID`` looks like a Bedrock foundation model id (e.g. ``anthropic.claude-3-haiku-...``)
    """
    if _use_azure_openai():
        return False
    if os.getenv("USE_BEDROCK", "").lower() in ("true", "1", "yes"):
        return True
    raw = _raw_llm_or_bedrock_model()
    rl = raw.lower()
    if rl.startswith("bedrock/"):
        return True
    return _looks_like_bedrock_foundation_model_id(raw)


def _bedrock_model_id() -> str:
    raw = (os.getenv("LLM_MODEL") or os.getenv("BEDROCK_MODEL_ID") or "").strip()
    if raw.lower().startswith("bedrock/"):
        return raw.split("/", 1)[1].strip()
    return raw or "anthropic.claude-3-haiku-20240307-v1:0"


def get_default_model() -> str:
    """Return the default model/deployment id for the configured provider."""
    if _use_azure_openai():
        return (
            os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
            or os.getenv("OPENAI_DEPLOYMENT_NAME")
            or "gpt-4o"
        )
    if use_bedrock_llm():
        return _bedrock_model_id()
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
    require_json_object: bool = True,
):
    """
    Call the configured LLM (Azure OpenAI, Bedrock Claude, or Gemini). Returns an object with .text.
    When using Azure: set AZURE_OPENAI_MAX_TOKENS or OPENAI_MAX_OUTPUT_TOKENS (default 8192) for long extraction.
    When using Gemini: set GEMINI_MAX_OUTPUT_TOKENS (default 8192) for long extraction.
    """
    if _use_azure_openai():
        from azure_openai_client import generate_content as azure_generate
        return azure_generate(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        )
    if use_bedrock_llm():
        from bedrock_client import generate_content as bedrock_generate
        return bedrock_generate(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        )
    from gemini_client import generate_content as gemini_generate
    return gemini_generate(
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
        require_json_object=require_json_object,
    )


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
    require_json_object: bool = True,
):
    """Async version: uses the configured LLM. Returns an object with .text."""
    if _use_azure_openai():
        from azure_openai_client import generate_content_async as azure_generate_async
        return await azure_generate_async(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        )
    if use_bedrock_llm():
        from bedrock_client import generate_content_async as bedrock_generate_async
        return await bedrock_generate_async(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        )
    from gemini_client import generate_content_async as gemini_generate_async
    return await gemini_generate_async(
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
        require_json_object=require_json_object,
    )


async def generate_content_stream(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
    require_json_object: bool = True,
) -> AsyncIterator[str]:
    """Stream merge/rank output; supported for Azure OpenAI and Bedrock only."""
    if _use_azure_openai():
        from azure_openai_client import generate_content_stream as azure_stream

        async for token in azure_stream(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        ):
            yield token
        return
    if use_bedrock_llm():
        from bedrock_client import generate_content_stream as bedrock_stream

        async for token in bedrock_stream(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        ):
            yield token
        return
    raise RuntimeError(
        "Streaming requires Azure OpenAI (USE_AZURE_OPENAI=true) or Bedrock "
        "(USE_AZURE_OPENAI=false and LLM_MODEL=bedrock/... or USE_BEDROCK=true). "
        "Gemini does not support this stream path."
    )
