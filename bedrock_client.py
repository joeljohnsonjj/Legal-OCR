"""
AWS Bedrock (Anthropic Claude) via boto3 bedrock-runtime.
Same interface as azure_openai_client / gemini_client: generate_content*, generate_content_stream.

Credentials:
  - Default: ambient credentials (e.g. AWS_ACCESS_KEY_ID) call Bedrock directly.
  - Assume role: set LLM_PARAMETER_PATH to an SSM parameter name whose value is a role ARN.
    Base credentials then call SSM + sts:AssumeRole; Bedrock uses temporary keys (same pattern as
    Parameter Store → AssumeRole POC). Set BEDROCK_SKIP_ASSUME_ROLE=true to force direct keys only.

Caching:
  - Direct credentials: one reused bedrock-runtime client per process (region-aware).
  - Assume role: SSM role ARN is read once per param path; STS AssumeRole is refreshed before
    session expiry (default 5 min buffer). Set BEDROCK_ASSUME_ROLE_REFRESH_BUFFER_SECONDS to tune.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, AsyncIterator, Dict, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Default-credential path: reuse client (avoid boto3.client() on every LLM call).
_direct_cache: Dict[str, Any] = {"client": None, "region": None}
# Assume-role path: reuse role ARN from SSM; refresh STS + client before token expiry.
_assumed_cache: Dict[str, Any] = {
    "client": None,
    "expires_at": 0.0,
    "role_arn": None,
    "param_path": None,
    "region": None,
}


class LLMResponse:
    def __init__(self, text: str):
        self.text = text


def _bedrock_config() -> tuple[str, Config]:
    region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1").strip()
    timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "600"))
    cfg = Config(read_timeout=timeout, connect_timeout=60, retries={"max_attempts": 5})
    return region, cfg


def _use_assumed_role_bedrock() -> bool:
    if os.getenv("BEDROCK_SKIP_ASSUME_ROLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    return bool(os.getenv("LLM_PARAMETER_PATH", "").strip())


def _assume_role_refresh_buffer_seconds() -> float:
    raw = os.getenv("BEDROCK_ASSUME_ROLE_REFRESH_BUFFER_SECONDS", "300").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 300.0


def _bedrock_runtime_client():
    """
    Return cached bedrock-runtime client.
    Direct path: singleton per region. Assume-role: SSM once per param path; STS refresh before expiry.
    """
    region, cfg = _bedrock_config()
    if not _use_assumed_role_bedrock():
        with _lock:
            if _direct_cache["client"] is not None and _direct_cache.get("region") == region:
                return _direct_cache["client"]
            client = boto3.client("bedrock-runtime", region_name=region, config=cfg)
            _direct_cache["client"] = client
            _direct_cache["region"] = region
            logger.debug("Bedrock client cached (direct credentials, region=%s)", region)
            return client

    param_path = os.getenv("LLM_PARAMETER_PATH", "").strip()
    session_name = (os.getenv("BEDROCK_ASSUME_ROLE_SESSION_NAME") or "legal-ocr-bedrock").strip()
    session_name = session_name[:64]
    buffer = _assume_role_refresh_buffer_seconds()

    now = time.time()
    with _lock:
        if _assumed_cache.get("param_path") != param_path or _assumed_cache.get("region") != region:
            _assumed_cache["client"] = None
            _assumed_cache["role_arn"] = None
            _assumed_cache["expires_at"] = 0.0
            _assumed_cache["param_path"] = param_path
            _assumed_cache["region"] = region

        if (
            _assumed_cache["client"] is not None
            and now < float(_assumed_cache["expires_at"]) - buffer
        ):
            return _assumed_cache["client"]

        role_arn = _assumed_cache.get("role_arn")
        if not role_arn:
            ssm = boto3.client("ssm", region_name=region, config=cfg)
            pr = ssm.get_parameter(Name=param_path, WithDecryption=True)
            role_arn = pr["Parameter"]["Value"].strip()
            _assumed_cache["role_arn"] = role_arn
            logger.info("Bedrock: cached role ARN from SSM parameter %s", param_path)

        sts = boto3.client("sts", region_name=region, config=cfg)
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
        exp = creds["Expiration"]
        expires_at = exp.timestamp() if hasattr(exp, "timestamp") else now + 3600

        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=cfg,
        )
        _assumed_cache["client"] = client
        _assumed_cache["expires_at"] = expires_at
        logger.info(
            "Bedrock client refreshed (STS assumed role); next refresh in ~%.0f min",
            max(0, (expires_at - now - buffer) / 60),
        )
        return client


def _client():
    return _bedrock_runtime_client()


def _resolve_model_id(model: Optional[str]) -> str:
    raw = (model or os.getenv("LLM_MODEL") or os.getenv("BEDROCK_MODEL_ID") or "").strip()
    if raw.lower().startswith("bedrock/"):
        raw = raw.split("/", 1)[1].strip()
    if not raw:
        raw = "anthropic.claude-3-haiku-20240307-v1:0"
    return raw


def _build_body(
    prompt: str,
    *,
    temperature: float,
    max_output_tokens: Optional[int],
    response_mime_type: str,
) -> Dict[str, Any]:
    max_tok = max_output_tokens if max_output_tokens is not None else int(
        os.getenv("BEDROCK_MAX_TOKENS") or os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "8192")
    )
    body: Dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tok,
        "temperature": temperature,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
    }
    if response_mime_type == "application/json":
        body["system"] = (
            "You must respond with valid JSON only. No markdown code fences, no text before or after the JSON."
        )
    return body


def generate_content(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
    require_json_object: bool = True,
) -> LLMResponse:
    _ = require_json_object
    del api_key  # AWS credentials from env / default chain
    model_id = _resolve_model_id(model)
    client = _client()
    body = _build_body(
        prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type=response_mime_type,
    )
    try:
        resp = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except ClientError as e:
        err = (e.response or {}).get("Error", {}) if getattr(e, "response", None) else {}
        msg = (err.get("Message") or str(e)) or ""
        if "INVALID_PAYMENT_INSTRUMENT" in msg or "Marketplace" in msg:
            logger.error(
                "AWS Bedrock rejected InvokeModel with a Marketplace/billing condition. "
                "This text is returned by AWS (not inferred by Legal-OCR). "
                "Typical fixes: ensure a default payment method on the AWS account, complete "
                "Bedrock model access / Marketplace subscription for this model, and wait a few minutes after changes. "
                "AWS message (truncated): %s",
                msg[:450] + ("…" if len(msg) > 450 else ""),
            )
            if not _use_assumed_role_bedrock():
                logger.error(
                    "If your IAM user’s account lacks Marketplace billing but another account has Bedrock access, "
                    "set LLM_PARAMETER_PATH to an SSM parameter containing that account’s role ARN (SSM + AssumeRole)."
                )
        raise
    payload = json.loads(resp["body"].read())
    parts = payload.get("content") or []
    text = ""
    for block in parts:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text") or ""
    return LLMResponse(text.strip())


async def generate_content_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    response_mime_type: str = "application/json",
    max_output_tokens: Optional[int] = None,
    require_json_object: bool = True,
) -> LLMResponse:
    return await asyncio.to_thread(
        generate_content,
        prompt,
        model=model,
        api_key=api_key,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
        require_json_object=require_json_object,
    )


def _stream_text_chunks(
    prompt: str,
    *,
    model_id: str,
    temperature: float,
    max_output_tokens: Optional[int],
    response_mime_type: str,
):
    client = _client()
    body = _build_body(
        prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type=response_mime_type,
    )
    response = client.invoke_model_with_response_stream(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    stream = response.get("body")
    if not stream:
        return
    for event in stream:
        chunk_data = event.get("chunk") if isinstance(event, dict) else None
        if not chunk_data:
            continue
        raw = chunk_data.get("bytes")
        if not raw:
            continue
        chunk = json.loads(raw)
        if chunk.get("type") != "content_block_delta":
            continue
        delta = chunk.get("delta") or {}
        piece = delta.get("text") or ""
        if piece:
            yield piece


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
    _ = require_json_object
    del api_key
    model_id = _resolve_model_id(model)
    loop = asyncio.get_running_loop()

    def next_chunk(it):
        try:
            return next(it)
        except StopIteration:
            return None

    it = iter(
        _stream_text_chunks(
            prompt,
            model_id=model_id,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type=response_mime_type,
        )
    )
    while True:
        piece = await loop.run_in_executor(None, next_chunk, it)
        if piece is None:
            break
        yield piece
