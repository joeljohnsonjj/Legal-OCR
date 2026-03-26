"""
LLM Router: function-calling to choose vector search track (rag_architecture.md §3.2).

Maps:
  search_financial_obligations -> filter record_type == extracted_obligation
  search_general_document      -> filter record_type == raw_page

Backends:
  - **AWS Bedrock** (recommended with Parameter Store + AssumeRole): when ``RAG_ROUTER_MODEL`` or
    ``LITELLM_MODEL`` / ``LLM_MODEL`` is ``bedrock/...``, uses LiteLLM with the same IAM session
    as ``aws_parameter_loader.init_llm_env`` (no OpenAI key required).
  - **OpenAI / Azure OpenAI**: otherwise uses the OpenAI SDK (Azure when endpoint + key are set).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# Aligns with Qdrant / schemas.record_type
RAGRouteDecision = Literal["extracted_obligation", "raw_page"]


ROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_financial_obligations",
            "description": (
                "Search extracted financial duties: rent, fees, deposits, payments, monetary obligations, "
                "who pays whom, maintenance costs, insurance premiums tied to money, default remedies involving payment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase why this tool fits the user message.",
                    }
                },
                "required": ["rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_general_document",
            "description": (
                "Search full contract page text for non-financial clauses: pets, smoking, termination notice, "
                "definitions, parties, use of premises, general rules, dispute resolution, governing law, notices."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase why this tool fits the user message.",
                    }
                },
                "required": ["rationale"],
            },
        },
    },
]


def _openai_client():
    """OpenAI SDK; supports Azure via AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY."""
    from openai import OpenAI, AzureOpenAI

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
    api_key = (
        os.getenv("AZURE_OPENAI_API_KEY")
        or os.getenv("AZURE_OPENAI_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    if endpoint and api_key:
        return AzureOpenAI(
            api_key=api_key,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
            azure_endpoint=endpoint,
        )
    return OpenAI(api_key=api_key or None)


def _router_model() -> str:
    """Prefer explicit RAG_ROUTER_MODEL; then Bedrock from main stack; else Azure/OpenAI defaults."""
    explicit = (os.getenv("RAG_ROUTER_MODEL") or "").strip()
    if explicit:
        return explicit
    litellm_m = (os.getenv("LITELLM_MODEL") or "").strip()
    if litellm_m.lower().startswith("bedrock/"):
        return litellm_m
    llm_m = (os.getenv("LLM_MODEL") or "").strip()
    if llm_m.lower().startswith("bedrock/"):
        return llm_m
    return (
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
        or os.getenv("OPENAI_ROUTER_MODEL")
        or "gpt-4o-mini"
    ).strip()


def _is_bedrock_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("bedrock/")


def _route_result_from_tool_name(name: str, rationale: str) -> RouteResult:
    if name == "search_financial_obligations":
        return RouteResult("extracted_obligation", name, rationale)
    if name == "search_general_document":
        return RouteResult("raw_page", name, rationale)
    logger.warning("Unknown tool %s; defaulting to extracted_obligation", name)
    return RouteResult("extracted_obligation", "search_financial_obligations", rationale)


def _parse_tool_call_openai_style(choice) -> tuple[Optional[str], str]:
    """Return (function_name, rationale) from an OpenAI-style message with tool_calls."""
    if isinstance(choice, dict):
        tools_calls = choice.get("tool_calls")
    else:
        tools_calls = getattr(choice, "tool_calls", None)
    if not tools_calls:
        return None, ""
    tc = tools_calls[0]
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
    else:
        fn = getattr(tc, "function", None)
    if fn is None:
        return None, ""
    if isinstance(fn, dict):
        name = fn.get("name")
        raw_args = fn.get("arguments", "{}")
    else:
        name = getattr(fn, "name", None)
        raw_args = getattr(fn, "arguments", None) or "{}"
    rationale = ""
    try:
        args = json.loads(raw_args or "{}")
        rationale = str(args.get("rationale", ""))
    except json.JSONDecodeError:
        pass
    return name, rationale


def _route_via_litellm_bedrock(user_message: str, model: str) -> RouteResult:
    """Tool-calling router via LiteLLM → AWS Bedrock (IAM from env / AssumeRole)."""
    import litellm

    from litellm_client import bedrock_litellm_kwargs

    sys_prompt = (
        "You route user messages to exactly one search function. "
        "Choose search_financial_obligations for money, rent, fees, deposits, payment duties, costs. "
        "Choose search_general_document for pets, rules, termination, definitions, notices, non-monetary clauses. "
        "If both apply, prefer search_financial_obligations when any payment or money is central."
    )

    max_router = int(os.getenv("RAG_ROUTER_MAX_TOKENS", "512"))

    kwargs = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_router,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_message},
        ],
        "tools": ROUTER_TOOLS,
        "tool_choice": "required",
    }
    kwargs.update(bedrock_litellm_kwargs(model))

    resp = litellm.completion(**kwargs)
    ch0 = resp.choices[0]
    msg = ch0.message
    name, rationale = _parse_tool_call_openai_style(msg)
    if not name:
        logger.warning("Bedrock router returned no tool_calls; defaulting to extracted_obligation")
        return RouteResult("extracted_obligation", "search_financial_obligations", "")
    return _route_result_from_tool_name(name, rationale)


@dataclass
class RouteResult:
    """Outcome of routing + optional model rationale."""

    record_type_filter: RAGRouteDecision
    tool_name: str
    rationale: str = ""


def route_chat_query(user_message: str, model: Optional[str] = None) -> RouteResult:
    """
    Call LLM with forced tool choice so exactly one of the two search modes is selected.

    When the resolved model id is ``bedrock/...``, uses LiteLLM + Bedrock with credentials from the
    environment (including temporary credentials from ``aws_parameter_loader.init_llm_env``).

    Legacy keyword UI should NOT use this — use metadata-only keyword filter instead (spec §3.1).
    """
    m = model or _router_model()

    if _is_bedrock_model(m):
        return _route_via_litellm_bedrock(user_message, m)

    client = _openai_client()
    sys_prompt = (
        "You route user messages to exactly one search function. "
        "Choose search_financial_obligations for money, rent, fees, deposits, payment duties, costs. "
        "Choose search_general_document for pets, rules, termination, definitions, notices, non-monetary clauses. "
        "If both apply, prefer search_financial_obligations when any payment or money is central."
    )

    resp = client.chat.completions.create(
        model=m,
        temperature=0,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=ROUTER_TOOLS,
        tool_choice="required",
    )

    choice = resp.choices[0].message
    name, rationale = _parse_tool_call_openai_style(choice)
    if not name:
        logger.warning("Router returned no tool_calls; defaulting to extracted_obligation")
        return RouteResult("extracted_obligation", "search_financial_obligations", "")
    return _route_result_from_tool_name(name, rationale)
