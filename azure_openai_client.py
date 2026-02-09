"""
Azure OpenAI API client using REST.
Same interface as gemini_client: generate_content() and generate_content_async()
return an object with .text for drop-in use in query_system and process_legal_documents.
"""
import asyncio
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# Response object compatible with GeminiResponse (.text)
class LLMResponse:
    """Minimal response object with .text for compatibility with existing code."""
    def __init__(self, text: str):
        self.text = text


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> LLMResponse:
    """
    Call Azure OpenAI Chat Completions via REST.
    Uses AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT (or model).
    """
    endpoint = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
    key = api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
    deployment = model or os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv("OPENAI_DEPLOYMENT_NAME")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

    if not endpoint or not key:
        raise ValueError(
            "Azure OpenAI requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY or OPENAI_API_KEY) in .env"
        )
    if not deployment:
        raise ValueError(
            "Azure OpenAI requires AZURE_OPENAI_DEPLOYMENT (or OPENAI_DEPLOYMENT_NAME) in .env - the deployment name of your model"
        )

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    headers = {
        "api-key": key,
        "Content-Type": "application/json",
    }
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_output_tokens if max_output_tokens is not None else int(os.getenv("AZURE_OPENAI_MAX_TOKENS") or os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "8192")),
    }
    if response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}

    r = requests.post(url, headers=headers, json=body, timeout=120)
    if r.status_code == 400:
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
        except Exception:
            msg = r.text
        raise ValueError(f"Azure OpenAI 400 Bad Request: {msg}") from None
    if r.status_code == 401:
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
        except Exception:
            msg = r.text
        raise ValueError(
            f"Azure OpenAI 401 Unauthorized: {msg}. Check AZURE_OPENAI_API_KEY and endpoint."
        ) from None
    if r.status_code == 429:
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
        except Exception:
            msg = r.text
        raise requests.exceptions.HTTPError(
            f"Azure OpenAI 429 Rate limit: {msg}", response=r
        ) from None
    r.raise_for_status()
    data = r.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return LLMResponse(content.strip())


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> LLMResponse:
    """Async version using asyncio.to_thread."""
    return await asyncio.to_thread(
        generate_content,
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
    )
