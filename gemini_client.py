"""
Gemini API client using REST + API key only.
Supports:
  1) Google AI Studio (Gemini API): generativelanguage.googleapis.com — use GEMINI_API_KEY (starts with AIza).
  2) Vertex AI with API key: aiplatform.googleapis.com — use GOOGLE_GENAI_USE_VERTEXAI=true + GEMINI_API_KEY.
     - With GOOGLE_CLOUD_PROJECT set: standard Vertex path (project/location in URL).
     - Without project: Vertex express mode — no project ID needed; key is tied to project at creation.
"""
import asyncio
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# Google AI Studio (Gemini) — accepts API key in header
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Vertex AI — API key as ?key=
VERTEX_BASE = "https://aiplatform.googleapis.com/v1"
# Vertex express mode: no project/location in path (project is associated with the key)
VERTEX_EXPRESS_PATH = "publishers/google/models"


class GeminiResponse:
    """Minimal response object with .text for compatibility with existing code."""
    def __init__(self, text: str):
        self.text = text


def _use_vertex_api_key() -> bool:
    """True if we should call Vertex AI with API key (not AI Studio)."""
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1", "yes")


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> GeminiResponse:
    """
    Call Gemini generateContent via REST using API key.
    - If GOOGLE_GENAI_USE_VERTEXAI=true: uses Vertex AI (aiplatform.googleapis.com) with project/location.
    - Otherwise: uses Google AI Studio (generativelanguage.googleapis.com).
    """
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY must be set")

    use_vertex = _use_vertex_api_key()
    model_name = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

    if use_vertex:
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("VERTEX_PROJECT")
        if project:
            # Standard Vertex AI: project + location in path
            location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("VERTEX_LOCATION", "us-central1")
            model_path = f"projects/{project}/locations/{location}/publishers/google/models/{model_name}"
            url = f"{VERTEX_BASE}/{model_path}:generateContent?key={key}"
        else:
            # Vertex AI express mode: no project ID — key is associated with project when created in console
            # https://cloud.google.com/vertex-ai/generative-ai/docs/start/express-mode/overview
            model_path = f"{VERTEX_EXPRESS_PATH}/{model_name}"
            url = f"{VERTEX_BASE}/{model_path}:generateContent?key={key}"
        headers = {"Content-Type": "application/json"}
    else:
        # Google AI Studio (Gemini API)
        url = f"{GEMINI_BASE}/models/{model_name}:generateContent"
        headers = {
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        }

    # Build request body: Vertex expects role in content; both accept contents[].parts[].text
    gen_config = {
        "temperature": temperature,
        "responseMimeType": response_mime_type,
        "maxOutputTokens": max_output_tokens if max_output_tokens is not None else int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
    }
    if use_vertex:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
    else:
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
    r = requests.post(url, headers=headers, json=body, timeout=120)
    if r.status_code == 400:
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
        except Exception:
            msg = r.text
        raise ValueError(
            f"Gemini/Vertex API 400 Bad Request: {msg}. "
            "For Vertex: check request body format and that the model name is valid for express mode (e.g. gemini-2.5-flash-lite)."
        ) from None
    if r.status_code == 401:
        if use_vertex:
            hint = "For Vertex AI: check GEMINI_API_KEY. If using express mode you don't need project ID; otherwise set GOOGLE_CLOUD_PROJECT. Get a key at https://console.cloud.google.com/apis/credentials or https://console.cloud.google.com/expressmode"
        else:
            hint = "Get or verify your key at https://aistudio.google.com/app/apikey – Gemini API keys start with 'AIza'. Ensure GEMINI_API_KEY is set in .env and the process was started from the project directory."
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
        except Exception:
            msg = r.text
        raise ValueError(f"Gemini API 401 Unauthorized: {msg}. {hint}") from None
    if r.status_code == 429:
        # Extract rate limit info if available
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text) or r.text
            # Extract quota details if present
            details = err.get("error", {}).get("details", [])
            quota_info = ""
            for detail in details:
                if isinstance(detail, dict) and "quotaInfo" in detail:
                    quota_info = f"\n* {detail.get('quotaInfo', '')}"
                elif isinstance(detail, dict) and "quotaMetric" in detail:
                    metric = detail.get("quotaMetric", "")
                    limit = detail.get("quotaLimit", "")
                    quota_info = f"\n* Quota exceeded for metric: {metric}, limit: {limit}, model: {model_name}"
        except Exception:
            msg = r.text
            quota_info = ""
        hint = f"Rate limit/quota exceeded. The retry decorator will automatically retry with exponential backoff.{quota_info}\nIf this persists: (1) Wait for quota reset (free tier: 20 requests per minute), (2) Process fewer pages at once, (3) Add delays between pages, (4) Check your API quota at https://aistudio.google.com/app/apikey"
        raise requests.exceptions.HTTPError(f"Gemini API 429 Too Many Requests: {msg}. {hint}", response=r) from None
    r.raise_for_status()
    data = r.json()
    parts = (
        data.get("candidates")
        or [{}]
    )[0].get("content", {}).get("parts") or []
    text = (parts[0].get("text") or "").strip()
    return GeminiResponse(text)


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
) -> GeminiResponse:
    """
    Async version of generate_content using asyncio.to_thread.
    
    This wraps the synchronous generate_content function to allow it to be used
    in async contexts without blocking the event loop. The actual HTTP request
    is executed in a thread pool.
    
    Args:
        prompt: The text prompt to send to Gemini
        model: Model name (optional, defaults to GEMINI_MODEL env var)
        api_key: API key (optional, defaults to GEMINI_API_KEY env var)
        temperature: Generation temperature (default: 0.1)
        response_mime_type: Expected response MIME type (default: "application/json")
    
    Returns:
        GeminiResponse object with .text attribute
    """
    return await asyncio.to_thread(
        generate_content,
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
    )
