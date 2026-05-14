"""
LLM client dispatcher: Azure OpenAI or Gemini (Google AI Studio / Vertex).
Same interface: generate_content() and generate_content_async() return an object with .text.
"""
import os
from typing import AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv()

# Default when using Google AI Studio (Gemini API); override with GEMINI_MODEL in .env
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"


def _use_azure_openai() -> bool:
    """True if we should use Azure OpenAI."""
    return os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")


def get_default_model() -> str:
    """Return the default model/deployment id for the configured provider."""
    if _use_azure_openai():
        return (
            os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
            or os.getenv("OPENAI_DEPLOYMENT_NAME")
            or "gpt-4o"
        )
    return os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


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
    Call the configured LLM (Azure OpenAI or Gemini). Returns an object with .text.
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
    """Stream merge/rank output; supported for Azure OpenAI and Gemini (Google AI Studio SSE)."""
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
    from gemini_client import generate_content_stream as gemini_stream

    async for token in gemini_stream(
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
        require_json_object=require_json_object,
    ):
        yield token
