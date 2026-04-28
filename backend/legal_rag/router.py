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
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# Aligns with Qdrant / schemas.record_type
RAGRouteDecision = Literal["extracted_obligation", "raw_page"]
ChatIntent = Literal["normal", "list_all_obligations", "document_summary"]


ROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_financial_obligations",
            "description": (
                "Search extracted financial duties: rent, fees, deposits, payments, monetary obligations, "
                "who pays whom, maintenance costs, insurance premiums tied to money, default remedies involving payment. "
                "Use this when the user asks to list all obligations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase why this tool fits the user message.",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["normal", "list_all_obligations", "document_summary"],
                        "description": (
                            "Pick document_summary for requests to summarize the whole document, "
                            "list_all_obligations for 'list all obligations' requests, else normal."
                        ),
                    },
                    "summary_mode": {
                        "type": "boolean",
                        "description": "True when the user asks for an overview or list-all summary mode.",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short keyword list extracted from the user message for BM25 filtering. "
                            "Include responsible party terms (tenant/landlord) and topic words; omit filler."
                        ),
                    },
                },
                "required": ["rationale", "intent", "summary_mode", "keywords"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_general_document",
            "description": (
                "Search full contract page text for non-financial clauses: pets, smoking, termination notice, "
                "definitions, parties, use of premises, general rules, dispute resolution, governing law, notices. "
                "Do NOT use this when the user asks to list all obligations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase why this tool fits the user message.",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["normal", "list_all_obligations", "document_summary"],
                        "description": (
                            "Pick document_summary for requests to summarize the whole document, "
                            "list_all_obligations for 'list all obligations' requests, else normal."
                        ),
                    },
                    "summary_mode": {
                        "type": "boolean",
                        "description": "True when the user asks for an overview or list-all summary mode.",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short keyword list extracted from the user message for BM25 filtering. "
                            "Include responsible party terms (tenant/landlord) and topic words; omit filler."
                        ),
                    },
                },
                "required": ["rationale", "intent", "summary_mode", "keywords"],
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


def _router_system_prompt() -> str:
    return (
        "You are a strict routing classifier for a legal document analysis system. "
        "Your sole job is to analyze the user query and choose exactly one tool call. "
        "Populate the tool arguments with intent, summary_mode, and keywords.\n\n"
        "## AVAILABLE TOOLS\n"
        "- search_financial_obligations → Use when the query is about duties, responsibilities, or requirements "
        "(who must do, pay, maintain, insure, comply, notify, or provide).\n"
        "- search_general_document → Use when the query is about definitions, general policies, or non-obligation "
        "clauses (e.g., pet rules, notice periods, permitted use, lease term, document structure).\n\n"
        "## INTENT VALUES\n"
        "- list_all_obligations → User wants ALL obligations under a broad topic (e.g., 'all tenant obligations', "
        "'every insurance requirement', 'list maintenance duties').\n"
        "- document_summary → User wants an overview or summary of the entire document.\n"
        "- normal → User asks a specific, narrow question about a single clause, party, or requirement.\n\n"
        "## DECISION RULES (apply in order)\n"
        "1) If the query asks to summarize the whole document → choose search_general_document, "
        "intent=document_summary, summary_mode=true.\n"
        "2) If the query asks to LIST or SHOW ALL obligations/duties under a broad topic → "
        "choose search_financial_obligations, intent=list_all_obligations, summary_mode=true.\n"
        "3) If the query asks a narrow, specific obligation question → "
        "choose search_financial_obligations, intent=normal, summary_mode=false.\n"
        "4) If the query asks about general clauses, definitions, or policies (not obligations) → "
        "choose search_general_document, intent=normal, summary_mode=false.\n"
        "5) If the query could match BOTH tools → always prefer search_financial_obligations.\n\n"
        "## SUMMARY_MODE RULES\n"
        "- ALWAYS true when intent is list_all_obligations or document_summary.\n"
        "- ALWAYS false when intent is normal.\n\n"
        "## KEYWORDS RULES\n"
        "- Extract 1–3 words that appear VERBATIM in the user query.\n"
        "- Only include domain-specific nouns: parties (tenant, landlord), topics (insurance, maintenance, "
        "parking), or named clauses.\n"
        "- NEVER include generic terms: obligations, duties, expenses, payments, fees, deposits, "
        "responsibilities, list, show, all, document.\n\n"
        "## EXAMPLES (use as guidance; respond with a tool call and arguments)\n"
        "Query: \"What are all the tenant obligations?\" → "
        "tool=search_financial_obligations, intent=list_all_obligations, summary_mode=true, keywords=[\"tenant\"]\n"
        "Query: \"Is the tenant required to carry liability insurance?\" → "
        "tool=search_financial_obligations, intent=normal, summary_mode=false, keywords=[\"tenant\", \"insurance\"]\n"
        "Query: \"Summarize the entire lease agreement.\" → "
        "tool=search_general_document, intent=document_summary, summary_mode=true, keywords=[\"lease\"]\n"
        "Query: \"What is the pet policy?\" → "
        "tool=search_general_document, intent=normal, summary_mode=false, keywords=[\"pet\"]\n\n"
        "Return only the tool call with those arguments; do not add extra text."
    )


def _route_result_from_tool_name(
    name: str,
    rationale: str,
    *,
    intent: ChatIntent = "normal",
    summary_mode: bool = False,
    keywords: Optional[List[str]] = None,
) -> RouteResult:
    keywords = keywords or []
    if intent == "list_all_obligations" and name != "search_financial_obligations":
        logger.warning(
            "[router] overriding tool=%s to search_financial_obligations due to intent=list_all_obligations",
            name,
        )
        name = "search_financial_obligations"
    logger.info(
        "[router] tool=%s intent=%s summary_mode=%s rationale=%s",
        name,
        intent,
        summary_mode,
        rationale,
    )
    if name == "search_financial_obligations":
        return RouteResult(
            "extracted_obligation",
            name,
            rationale,
            intent=intent,
            summary_mode=summary_mode,
            keywords=keywords,
        )
    if name == "search_general_document":
        return RouteResult(
            "raw_page",
            name,
            rationale,
            intent=intent,
            summary_mode=summary_mode,
            keywords=keywords,
        )
    logger.warning("Unknown tool %s; defaulting to extracted_obligation", name)
    return RouteResult(
        "extracted_obligation",
        "search_financial_obligations",
        rationale,
        intent=intent,
        summary_mode=summary_mode,
        keywords=keywords,
    )


def _parse_tool_call_openai_style(choice) -> Tuple[Optional[str], str, ChatIntent, bool, List[str]]:
    """Return (function_name, rationale, intent, summary_mode, keywords) from tool_calls."""
    if isinstance(choice, dict):
        tools_calls = choice.get("tool_calls")
    else:
        tools_calls = getattr(choice, "tool_calls", None)
    if not tools_calls:
        return None, "", "normal", False, []
    tc = tools_calls[0]
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
    else:
        fn = getattr(tc, "function", None)
    if fn is None:
        return None, "", "normal", False, []
    if isinstance(fn, dict):
        name = fn.get("name")
        raw_args = fn.get("arguments", "{}")
    else:
        name = getattr(fn, "name", None)
        raw_args = getattr(fn, "arguments", None) or "{}"
    rationale = ""
    intent: ChatIntent = "normal"
    summary_mode = False
    keywords: List[str] = []
    try:
        args = json.loads(raw_args or "{}")
        rationale = str(args.get("rationale", ""))
        raw_intent = str(args.get("intent") or "normal").strip().lower()
        if raw_intent in {"normal", "list_all_obligations", "document_summary"}:
            intent = raw_intent  # type: ignore[assignment]
        summary_mode = bool(args.get("summary_mode", False))
        raw_keywords = args.get("keywords") or []
        if isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
    except json.JSONDecodeError:
        pass
    logger.info(
        "[router] tool_call name=%s intent=%s summary_mode=%s keywords=%s rationale=%s raw_args=%s",
        name,
        intent,
        summary_mode,
        keywords,
        rationale,
        raw_args,
    )
    return name, rationale, intent, summary_mode, keywords


def _route_via_litellm_bedrock(user_message: str, model: str) -> RouteResult:
    """Tool-calling router via LiteLLM → AWS Bedrock (IAM from env / AssumeRole)."""
    import litellm

    from litellm_client import bedrock_litellm_kwargs

    sys_prompt = _router_system_prompt()

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
    name, rationale, intent, summary_mode, keywords = _parse_tool_call_openai_style(msg)
    if not name:
        logger.warning("Bedrock router returned no tool_calls; defaulting to extracted_obligation")
        return RouteResult(
            "extracted_obligation",
            "search_financial_obligations",
            "",
            intent="normal",
            summary_mode=False,
            keywords=[],
        )
    return _route_result_from_tool_name(
        name,
        rationale,
        intent=intent,
        summary_mode=summary_mode,
        keywords=keywords,
    )


@dataclass
class RouteResult:
    """Outcome of routing + optional model rationale."""

    record_type_filter: RAGRouteDecision
    tool_name: str
    rationale: str = ""
    intent: ChatIntent = "normal"
    summary_mode: bool = False
    keywords: List[str] = field(default_factory=list)


def route_chat_query(user_message: str, model: Optional[str] = None) -> RouteResult:
    """
    Call LLM with forced tool choice so exactly one of the two search modes is selected.

    When the resolved model id is ``bedrock/...``, uses LiteLLM + Bedrock with credentials from the
    environment (including temporary credentials from ``aws_parameter_loader.init_llm_env``).

    Legacy keyword UI should NOT use this — use metadata-only keyword filter instead (spec §3.1).
    """
    m = model or _router_model()

    if _is_bedrock_model(m):
        logger.info("[router] using Bedrock model=%s", m)
        return _route_via_litellm_bedrock(user_message, m)

    logger.info("[router] using OpenAI/Azure model=%s", m)
    client = _openai_client()
    sys_prompt = _router_system_prompt()

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
    name, rationale, intent, summary_mode, keywords = _parse_tool_call_openai_style(choice)
    if not name:
        logger.warning("Router returned no tool_calls; defaulting to extracted_obligation")
        return RouteResult(
            "extracted_obligation",
            "search_financial_obligations",
            "",
            intent="normal",
            summary_mode=False,
            keywords=[],
        )
    return _route_result_from_tool_name(
        name,
        rationale,
        intent=intent,
        summary_mode=summary_mode,
        keywords=keywords,
    )
