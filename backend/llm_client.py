"""
LLM client dispatcher.
- If LITELLM_MODEL is set: use LiteLLM (switch providers by changing one env var, e.g. gemini/gemini-2.5-flash-lite or azure/gpt-4o-mini).
- Else: use Azure OpenAI or Gemini based on USE_AZURE_OPENAI.
Same interface: generate_content(), generate_content_async(), generate_content_stream(); all return .text or yield tokens.
"""
import os
from typing import AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv()


def _use_litellm() -> bool:
    """True if LITELLM_MODEL is set (unified model switching via LiteLLM)."""
    return bool((os.getenv("LITELLM_MODEL") or os.getenv("LLM_MODEL") or "").strip())


def _use_azure_openai() -> bool:
    """True if we should use Azure OpenAI instead of Gemini (only when not using LiteLLM)."""
    return os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")


def get_default_model() -> str:
    """Return the default model/deployment name. Uses LITELLM_MODEL if set, else Azure or Gemini env vars."""
    if _use_litellm():
        return (os.getenv("LITELLM_MODEL") or os.getenv("LLM_MODEL") or "").strip()
    if _use_azure_openai():
        return os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv("OPENAI_DEPLOYMENT_NAME") or "gpt-4o"
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")


def validate_llm_environment() -> None:
    """
    Raise ValueError if required credentials for the active llm_client routing are missing.
    Matches generate_content: LiteLLM (LITELLM_MODEL) first, else Azure, else Gemini.
    """
    _litellm = (os.getenv("LITELLM_MODEL") or os.getenv("LLM_MODEL") or "").strip()
    _use_azure = _use_azure_openai()
    if _litellm:
        lm = _litellm.lower()
        if lm.startswith("bedrock/"):
            region = (
                os.getenv("AWS_REGION_NAME")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or ""
            ).strip()
            if not region:
                raise ValueError(
                    "LITELLM_MODEL (or LLM_MODEL) is Bedrock (bedrock/...); set AWS_REGION or AWS_REGION_NAME in .env. "
                    "Ensure AWS credentials can call bedrock:InvokeModel (fix SSM/init_llm_env IAM or use static keys / profile)."
                )
        elif lm.startswith("azure/"):
            if not os.getenv("AZURE_OPENAI_ENDPOINT") or not (
                os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
            ):
                raise ValueError(
                    "LITELLM_MODEL (or LLM_MODEL) is azure/...; set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY) in .env"
                )
        elif lm.startswith("gemini/") or lm.startswith("vertex_ai/"):
            if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
                raise ValueError(
                    "LITELLM_MODEL (or LLM_MODEL) is Gemini/Vertex; set GEMINI_API_KEY or GOOGLE_API_KEY in .env (https://aistudio.google.com/app/apikey)"
                )
    elif _use_azure:
        if not os.getenv("AZURE_OPENAI_ENDPOINT") or not (
            os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
        ):
            raise ValueError(
                "USE_AZURE_OPENAI is set; AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY) must be set in .env"
            )
        if not os.getenv("AZURE_OPENAI_DEPLOYMENT") and not os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") and not os.getenv(
            "OPENAI_DEPLOYMENT_NAME"
        ):
            raise ValueError(
                "USE_AZURE_OPENAI is set; AZURE_OPENAI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT_NAME must be set in .env"
            )
    else:
        if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
            raise ValueError("GEMINI_API_KEY must be set in .env (https://aistudio.google.com/app/apikey)")


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
    Call the configured LLM. Returns an object with .text.
    When using LiteLLM: set LITELLM_MODEL (e.g. gemini/gemini-2.5-flash-lite or azure/gpt-4o-mini).
    Otherwise: Azure (AZURE_OPENAI_*) or Gemini (GEMINI_*).
    """
    if _use_litellm():
        from litellm_client import generate_content as litellm_generate
        return litellm_generate(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        )
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
    """Async version: uses LiteLLM if LITELLM_MODEL set, else Azure or Gemini. Returns an object with .text."""
    if _use_litellm():
        from litellm_client import generate_content_async as litellm_generate_async
        return await litellm_generate_async(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        )
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


async def generate_content_stream(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """Stream tokens from the configured LLM. Uses LiteLLM when LITELLM_MODEL set, else Azure (Gemini-only has no stream)."""
    if _use_litellm():
        from litellm_client import generate_content_stream as litellm_stream
        async for token in litellm_stream(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        ):
            yield token
        return
    if _use_azure_openai():
        from azure_openai_client import generate_content_stream as azure_stream
        async for token in azure_stream(
            prompt,
            model=model,
            api_key=api_key,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        ):
            yield token
        return
    raise ValueError(
        "Streaming requires LITELLM_MODEL (e.g. gemini/gemini-2.5-flash-lite) or USE_AZURE_OPENAI=true. "
        "Set LITELLM_MODEL in .env to use LiteLLM for streaming with any provider."
    )
