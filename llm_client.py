"""
LLM client dispatcher: uses Azure OpenAI or Gemini based on USE_AZURE_OPENAI.
Same interface for both: generate_content() and generate_content_async() return an object with .text.
"""
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _use_azure_openai() -> bool:
    """True if we should use Azure OpenAI instead of Gemini."""
    return os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")


def get_default_model() -> str:
    """Return the default model/deployment name for the configured provider (Azure or Gemini)."""
    if _use_azure_openai():
        return os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv("OPENAI_DEPLOYMENT_NAME") or "gpt-4o"
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
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
        )
    from gemini_client import generate_content as gemini_generate
    return gemini_generate(
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
    )


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
):
    """Async version: uses the configured LLM (Azure OpenAI or Gemini). Returns an object with .text."""
    if _use_azure_openai():
        from azure_openai_client import generate_content_async as azure_generate_async
        return await azure_generate_async(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        )
    from gemini_client import generate_content_async as gemini_generate_async
    return await gemini_generate_async(
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
    )
