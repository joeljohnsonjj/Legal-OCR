"""
Lightweight API hardening (optional, env-driven):
- API key on sensitive POST routes (+ DELETE index)
- Per-IP rate limit for those routes
- Chat message bounds + NUL stripping (basic injection / abuse mitigation)
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _expected_api_key() -> str:
    return (os.getenv("LEGAL_OCR_API_KEY") or "").strip()


def _rate_limit_per_minute() -> int:
    try:
        return max(0, int(os.getenv("LEGAL_OCR_RATE_LIMIT_PER_MINUTE", "0")))
    except ValueError:
        return 0


def max_chat_message_chars() -> int:
    try:
        return max(256, min(100_000, int(os.getenv("LEGAL_OCR_MAX_CHAT_MESSAGE_CHARS", "12000"))))
    except ValueError:
        return 12000


def max_query_chars() -> int:
    try:
        return max(256, min(50_000, int(os.getenv("LEGAL_OCR_MAX_QUERY_CHARS", "8000"))))
    except ValueError:
        return 8000


def sanitize_chat_message(s: str, max_len: int | None = None) -> str:
    """Strip NULs, trim, cap length (reduces prompt bloat / some control-char tricks)."""
    if not s:
        return ""
    cap = max_len if max_len is not None else max_chat_message_chars()
    s = (s or "").replace("\x00", "").strip()
    if len(s) > cap:
        s = s[:cap]
    return s


def sanitize_query_text(s: str) -> str:
    if s is None:
        return ""
    cap = max_query_chars()
    t = str(s).replace("\x00", "").strip()
    if len(t) > cap:
        t = t[:cap]
    return t


def expose_error_details() -> bool:
    return (os.getenv("LEGAL_OCR_EXPOSE_ERROR_DETAILS") or "").lower() in ("true", "1", "yes")


def _protected_route(method: str, path: str) -> bool:
    if method == "DELETE" and path.startswith("/rag/index/"):
        return True
    if method != "POST":
        return False
    if path == "/chat" or path.startswith("/process"):
        return True
    if path == "/query" or path.startswith("/query/"):
        return True
    return False


_post_ts: dict[str, list[float]] = defaultdict(list)


class LegalOCRSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        expected = _expected_api_key()
        if expected and _protected_route(request.method, path):
            got = (request.headers.get("X-API-Key") or "").strip()
            auth = request.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            if got != expected:
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)

        lim = _rate_limit_per_minute()
        if lim > 0 and _protected_route(request.method, path):
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            window = 60.0
            arr = _post_ts[ip]
            arr[:] = [t for t in arr if now - t < window]
            if len(arr) >= lim:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Try again shortly."},
                    status_code=429,
                )
            arr.append(now)

        return await call_next(request)


def http_safe_exception_detail(exc: Exception) -> Tuple[str, str]:
    """
    Return (client_message, log_message). Avoid echoing raw exception text to clients by default.
    """
    if expose_error_details():
        return (str(exc), str(exc))
    return ("Request failed. Set LEGAL_OCR_EXPOSE_ERROR_DETAILS=true for diagnostics.", str(exc))
