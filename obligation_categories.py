"""
Two-tier category/subcategory for consolidated obligation dicts.

Tier 1: exactly seven labels (see VALID_CATEGORIES). No ad-hoc Tier-1 buckets; ambiguous rows
map to General_Legal_Provisions with a concrete subcategory.

Primary path: LLM assigns Tier 1 + Tier 2 together (subcategory is never null after normalize).
Chunks run in parallel by default (same model quality; lower wall-clock time). Tune with
LEGAL_OCR_CATEGORY_LLM_PARALLEL, LEGAL_OCR_CATEGORY_LLM_CONCURRENCY, LEGAL_OCR_CATEGORY_LLM_CHUNK.
Fallback: heuristic rules if LEGAL_OCR_CATEGORY_USE_LLM is false or the API/parse fails.

Legacy consolidated JSON may still use old category strings; _normalize_category maps them
into the seven Tier-1 labels.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Tier 1 — exact strings required from the LLM (and stored on each obligation).
FINANCIAL_PAYMENTS = "Financial_Payments"
INFRASTRUCTURE_MAINTENANCE = "Infrastructure_Maintenance"
INSURANCE_LIABILITY = "Insurance_Liability"
COMPLIANCE_ADMINISTRATIVE = "Compliance_Administrative"
FUTURE_COMMITMENTS = "Future_Commitments"
OPERATIONAL_RESTRICTIONS = "Operational_Restrictions"
GENERAL_LEGAL_PROVISIONS = "General_Legal_Provisions"

VALID_CATEGORIES: Tuple[str, ...] = (
    FINANCIAL_PAYMENTS,
    INFRASTRUCTURE_MAINTENANCE,
    INSURANCE_LIABILITY,
    COMPLIANCE_ADMINISTRATIVE,
    FUTURE_COMMITMENTS,
    OPERATIONAL_RESTRICTIONS,
    GENERAL_LEGAL_PROVISIONS,
)
_VALID_CAT_SET: Set[str] = set(VALID_CATEGORIES)

# Map normalized lowercase_snake aliases → canonical Tier 1 (LLM slop + legacy pipeline values).
_TIER1_NORMALIZATION: Dict[str, str] = {
    "financial_payments": FINANCIAL_PAYMENTS,
    "infrastructure_maintenance": INFRASTRUCTURE_MAINTENANCE,
    "insurance_liability": INSURANCE_LIABILITY,
    "compliance_administrative": COMPLIANCE_ADMINISTRATIVE,
    "future_commitments": FUTURE_COMMITMENTS,
    "operational_restrictions": OPERATIONAL_RESTRICTIONS,
    "general_legal_provisions": GENERAL_LEGAL_PROVISIONS,
    # Legacy v1 categories
    "payment_obligations": FINANCIAL_PAYMENTS,
    "penalties_fees": FINANCIAL_PAYMENTS,
    "insurance": INSURANCE_LIABILITY,
    "liability": INSURANCE_LIABILITY,
    "other": GENERAL_LEGAL_PROVISIONS,
}

# Stable key order for nested JSON output (all keys present; empty arrays allowed)
CATEGORY_NEST_ORDER: Tuple[str, ...] = VALID_CATEGORIES

_SUBCATEGORY_VAGUE: Set[str] = {
    "misc",
    "miscellaneous",
    "general",
    "uncategorized",
    "unknown",
    "default",
    "none",
    "na",
    "n/a",
    "n_a",
    "other",
    "various",
    "diverse",
    "mixed",
    "misc.",
}

_PROMPT_KEYS = (
    "DutyType",
    "Responsible Party",
    "Owner Responsibility",
    "Reasoning",
    "Citation",
    "related_keywords",
)


def _flatten_field(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val if x is not None and str(x).strip())
    if isinstance(val, dict):
        return str(val)
    return str(val).strip()


def _citation_text(citation: Any) -> str:
    if citation is None:
        return ""
    if isinstance(citation, str):
        return citation
    if isinstance(citation, list):
        parts: List[str] = []
        for c in citation:
            if isinstance(c, dict):
                pages = c.get("pageNumbers") or []
                sections = c.get("section") or []
                if pages:
                    parts.append(" ".join(map(str, pages)))
                if sections:
                    parts.append(" ".join(map(str, sections)))
            else:
                parts.append(str(c))
        return " ".join(parts)
    return str(citation)


def _obligation_corpus(ob: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in (
        "DutyType",
        "Responsible Party",
        "Owner Responsibility",
        "Reasoning",
        "related_keywords",
    ):
        parts.append(_flatten_field(ob.get(key)))
    parts.append(_citation_text(ob.get("Citation")))
    return " ".join(parts).lower()


def _word_re(*words: str) -> re.Pattern[str]:
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)


_RE_PENALTIES = _word_re(
    "penalty",
    "penalties",
    "fine",
    "fines",
    "forfeit",
    "forfeiture",
)
_RE_LATE = _word_re("late")
_RE_FEE = _word_re("fee", "fees")
_RE_INSURANCE = _word_re("insurance")
_RE_COVERAGE = _word_re("coverage")
_RE_POLICY = _word_re("policy", "policies")
_RE_HVAC = re.compile(r"\bhvac\b", re.IGNORECASE)
_RE_MAINTENANCE = _word_re("maintenance", "maintain", "maintaining")
_RE_REPAIR = _word_re("repair", "repairs", "replace", "replacement")
_RE_PHYSICAL = _word_re(
    "elevator",
    "roof",
    "plumbing",
    "electrical",
    "structural",
    "mechanical",
    "pavement",
    "parking",
    "facilities",
    "fixture",
    "fixtures",
    "equipment",
    "sewer",
    "drainage",
)
_RE_SYSTEM_INFRA = _word_re("hvac", "sprinkler", "generator", "boiler", "chiller")


def _phrase_in(text: str, phrase: str) -> bool:
    return phrase.lower() in text


def _infra_service_context(text: str) -> bool:
    if not _word_re("service", "services").search(text):
        return False
    return bool(
        _RE_HVAC.search(text)
        or _RE_SYSTEM_INFRA.search(text)
        or _RE_MAINTENANCE.search(text)
        or _RE_REPAIR.search(text)
        or _RE_PHYSICAL.search(text)
        or _phrase_in(text, "operating costs")
        or _phrase_in(text, "operating cost")
    )


def _matches_infrastructure(text: str) -> bool:
    if _RE_HVAC.search(text):
        return True
    if _RE_MAINTENANCE.search(text) or _RE_REPAIR.search(text):
        return True
    if _RE_PHYSICAL.search(text):
        return True
    if _RE_SYSTEM_INFRA.search(text):
        return True
    if _infra_service_context(text):
        return True
    if _phrase_in(text, "additional hvac") or _phrase_in(text, "hvac services"):
        return True
    return False


_RE_FUTURE = re.compile(
    r"\b(?:"
    r"renewal|renewals|renew|renewed|renewing|"
    r"escalation|escalations|escalate|escalated|escalating|"
    r"increase|increases|increased|increasing|"
    r"adjustment|adjustments|adjusted|adjusting|"
    r"reassess|reassessed|reassessment|reassessments|"
    r"extension|extensions|extend|extended|extending"
    r")\b",
    re.IGNORECASE,
)


def _matches_future_commitments(text: str) -> bool:
    if _phrase_in(text, "rent escalation") or _phrase_in(text, "lease extension"):
        return True
    if _phrase_in(text, "option to renew") or _phrase_in(text, "renewal option"):
        return True
    if _phrase_in(text, "option term") or _phrase_in(text, "renewal term"):
        return True
    if _RE_FUTURE.search(text):
        return True
    if _word_re("option").search(text) and (
        _phrase_in(text, "renew")
        or _phrase_in(text, "extend")
        or _phrase_in(text, "lease")
        or _phrase_in(text, "rent")
    ):
        return True
    return False


def _matches_liability(text: str) -> bool:
    if _word_re("liable", "liability").search(text):
        return True
    if "indemnif" in text:
        return True
    if _phrase_in(text, "remain responsible") or _phrase_in(text, "remains responsible"):
        return True
    if _phrase_in(text, "responsible for"):
        return True
    return False


_RE_TAX_PAYMENT = re.compile(
    r"\b(?:"
    r"property\s+tax|real\s+estate\s+tax|ad\s+valorem|"
    r"tax(?:es)?\s+and\s+assessments|special\s+assessment|tax\s+levy|tax\s+bill|"
    r"reimburs\w*\s+(?:for\s+)?(?:the\s+)?tax|pay\w*\s+(?:the\s+)?tax|tax(?:es)?\s+payable|"
    r"tax(?:es)?\s+payment|payment\s+of\s+tax|pass[\s-]?through\s+tax|tax\s+increment"
    r")\b",
    re.IGNORECASE,
)


def _matches_tax_as_payment(text: str) -> bool:
    """Routine tax money duties → Financial_Payments (evaluated before broad liability heuristics)."""
    if _RE_TAX_PAYMENT.search(text):
        return True
    if re.search(r"\btaxes?\b", text, re.IGNORECASE) and (
        _phrase_in(text, "reimburs")
        or _phrase_in(text, "tenant shall pay")
        or _phrase_in(text, "operating expense")
        or _phrase_in(text, "operating expenses")
        or _phrase_in(text, "triple net")
        or _phrase_in(text, "nnn")
    ):
        return True
    return False


_RE_PAYMENT = re.compile(
    r"\b(?:"
    r"pay|payment|payments|payable|paid|"
    r"cost|costs|expense|expenses|rate|rates|"
    r"reimburs\w*|deposit|deposits|sum|amount|due|invoice|invoices|"
    r"charge|charges|prorat\w*"
    r")\b",
    re.IGNORECASE,
)


def _matches_penalties(text: str) -> bool:
    if _RE_PENALTIES.search(text):
        return True
    if _RE_LATE.search(text) and (
        _RE_FEE.search(text)
        or _word_re("interest").search(text)
        or _phrase_in(text, "late payment")
        or _phrase_in(text, "past due")
    ):
        return True
    if _phrase_in(text, "late fee") or _phrase_in(text, "late charge"):
        return True
    return False


def _matches_insurance(text: str) -> bool:
    return bool(_RE_INSURANCE.search(text) or _RE_COVERAGE.search(text) or _RE_POLICY.search(text))


def _matches_payment_obligations(text: str) -> bool:
    return _RE_PAYMENT.search(text) is not None


_RE_COMPLIANCE = _word_re(
    "report",
    "reporting",
    "reports",
    "notice",
    "notices",
    "notify",
    "notification",
    "certificat",
    "filing",
    "filings",
    "disclosure",
    "disclosures",
    "books",
    "records",
    "audit",
    "compliance",
    "regulatory",
)
_RE_OPERATIONAL = _word_re(
    "nuisance",
    "noise",
    "odor",
    "odour",
    "prohibited",
    "prohibition",
    "exclusive",
    "signage",
    "sign",
    "use",
    "uses",
    "hazardous",
    "waste",
)
_RE_GENERAL_LEGAL = _word_re(
    "arbitration",
    "mediate",
    "mediation",
    "litigation",
    "jurisdiction",
    "venue",
    "governing",
    "severab",
    "waiver",
    "amendment",
    "entire",
    "agreement",
    "counterparts",
    "headings",
)


def _matches_compliance_administrative(text: str) -> bool:
    if _RE_COMPLIANCE.search(text):
        return True
    if _phrase_in(text, "financial statements") or _phrase_in(text, "annual statement"):
        return True
    return False


def _matches_operational_restrictions(text: str) -> bool:
    if _phrase_in(text, "shall not") or _phrase_in(text, "must not") or _phrase_in(text, "may not"):
        if _RE_OPERATIONAL.search(text) or _word_re("assign", "assignment", "sublet", "sublease").search(text):
            return True
    if _phrase_in(text, "use of") and _phrase_in(text, "premises"):
        return True
    if _phrase_in(text, "permitted use") or _phrase_in(text, "permitted uses"):
        return True
    if _RE_OPERATIONAL.search(text) and not _matches_insurance(text):
        return True
    return False


def _matches_general_legal_provisions(text: str) -> bool:
    if _RE_GENERAL_LEGAL.search(text):
        return True
    if _phrase_in(text, "dispute resolution") or _phrase_in(text, "choice of law"):
        return True
    return False


def _infer_subcategory_heuristic(category: str, text: str) -> str:
    if category == INFRASTRUCTURE_MAINTENANCE and _RE_HVAC.search(text):
        return "HVAC"
    if category == INFRASTRUCTURE_MAINTENANCE:
        for label, pat in (
            ("Plumbing", _word_re("plumbing", "sewer", "drainage")),
            ("Roof", _word_re("roof")),
            ("Electrical", _word_re("electrical")),
            ("Elevator", _word_re("elevator")),
            ("Parking", _word_re("parking", "pavement")),
        ):
            if pat.search(text):
                return label
        if _RE_MAINTENANCE.search(text) or _RE_REPAIR.search(text):
            return "Repair/Maintenance"
    if category == FINANCIAL_PAYMENTS:
        if _matches_penalties(text) or _phrase_in(text, "late fee") or _phrase_in(text, "late charge"):
            return "Late_fee/Penalty"
        if re.search(r"\btaxes?\b", text, re.IGNORECASE) and (
            _phrase_in(text, "property")
            or _phrase_in(text, "real estate")
            or _phrase_in(text, "reimburs")
            or _phrase_in(text, "ad valorem")
        ):
            return "Tax"
        if _word_re("rent", "rental").search(text) or _phrase_in(text, "base rent"):
            return "Rent"
        if _phrase_in(text, "operating expense") or _phrase_in(text, "operating expenses"):
            return "Operating_expense"
        if _word_re("deposit", "deposits").search(text):
            return "Deposit"
        if _RE_PAYMENT.search(text):
            return "Payment"
    if category == INSURANCE_LIABILITY:
        if _phrase_in(text, "certificate") and _matches_insurance(text):
            return "Certificate_of_insurance"
        if _phrase_in(text, "liability insurance") or _phrase_in(text, "general liability"):
            return "Liability_insurance"
        if _phrase_in(text, "business interruption"):
            return "Business_interruption"
        if "indemnif" in text or _word_re("hold", "harmless").search(text):
            return "Indemnity"
        if _matches_insurance(text):
            return "Insurance"
        if _matches_liability(text):
            return "Liability"
    if category == COMPLIANCE_ADMINISTRATIVE:
        if _word_re("notice", "notices", "notify").search(text):
            return "Notice"
        if _word_re("report", "reporting").search(text):
            return "Reporting"
        if _word_re("audit").search(text):
            return "Audit"
        return "Compliance"
    if category == OPERATIONAL_RESTRICTIONS:
        if _word_re("odor", "odour", "nuisance", "noise").search(text):
            return "Odor/Nuisance"
        if _word_re("assign", "assignment").search(text):
            return "Assignment"
        if _word_re("sublet", "sublease", "subleasing").search(text):
            return "Subletting"
        return "Use/Operations"
    if category == FUTURE_COMMITMENTS:
        if _phrase_in(text, "option to purchase") or _phrase_in(text, "purchase option"):
            return "Option_to_purchase"
        if _phrase_in(text, "right of first refusal") or _phrase_in(text, "rofr"):
            return "Right_of_first_refusal"
        if _phrase_in(text, "renew") or _phrase_in(text, "renewal"):
            return "Renewal"
        if _phrase_in(text, "escalat"):
            return "Escalation"
        return "Future_term"
    if category == GENERAL_LEGAL_PROVISIONS:
        if _word_re("arbitration").search(text):
            return "Arbitration"
        if _word_re("mediation", "mediate").search(text):
            return "Mediation"
        if _phrase_in(text, "governing law") or _word_re("jurisdiction").search(text):
            return "Governing_law/Jurisdiction"
        return "Legal_provision"
    return "General"


def _assign_financial_category_heuristic(ob: Dict[str, Any]) -> None:
    """
    Rule-based Tier 1 + Tier 2. Order: money sanctions and outlays first; physical upkeep before
    broad "liable/responsible" wording so repairs do not land in Insurance_Liability.
    """
    text = _obligation_corpus(ob)
    if _matches_penalties(text) or (
        _RE_LATE.search(text)
        and (
            _RE_FEE.search(text)
            or _word_re("interest").search(text)
            or _phrase_in(text, "late payment")
            or _phrase_in(text, "past due")
        )
    ):
        category = FINANCIAL_PAYMENTS
    elif _matches_tax_as_payment(text):
        category = FINANCIAL_PAYMENTS
    elif _matches_infrastructure(text):
        category = INFRASTRUCTURE_MAINTENANCE
    elif _matches_future_commitments(text):
        category = FUTURE_COMMITMENTS
    elif _matches_insurance(text) or "indemnif" in text or _phrase_in(text, "hold harmless"):
        category = INSURANCE_LIABILITY
    elif _matches_payment_obligations(text):
        category = FINANCIAL_PAYMENTS
    elif _matches_liability(text):
        category = INSURANCE_LIABILITY
    elif _matches_general_legal_provisions(text):
        category = GENERAL_LEGAL_PROVISIONS
    elif _matches_compliance_administrative(text):
        category = COMPLIANCE_ADMINISTRATIVE
    elif _matches_operational_restrictions(text):
        category = OPERATIONAL_RESTRICTIONS
    else:
        category = GENERAL_LEGAL_PROVISIONS
    ob["category"] = category
    ob["subcategory"] = _infer_subcategory_heuristic(category, text)


def assign_financial_category(ob: Dict[str, Any]) -> None:
    """
    Rule-based categorization for a single obligation (tests, fallback).
    Sets ob['category'] and ob['subcategory'] only.
    """
    _assign_financial_category_heuristic(ob)


def _obligation_for_prompt(ob: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in _PROMPT_KEYS:
        out[k] = ob.get(k)
    return out


def _strip_response_json(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _normalize_category(raw: Any) -> str:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return GENERAL_LEGAL_PROVISIONS
    s = str(raw).strip()
    if s in _VALID_CAT_SET:
        return s
    low = s.lower().replace(" ", "_").replace("-", "_")
    low = re.sub(r"_+", "_", low).strip("_")
    if low in _TIER1_NORMALIZATION:
        return _TIER1_NORMALIZATION[low]
    logger.debug("Unknown Tier-1 category %r; mapping to %s", raw, GENERAL_LEGAL_PROVISIONS)
    return GENERAL_LEGAL_PROVISIONS


def _normalize_subcategory(raw: Any) -> str:
    if raw is None:
        return "General"
    s = str(raw).strip()
    if not s:
        return "General"
    s = re.sub(r"\s+", " ", s)
    if len(s) > 120:
        s = s[:120].rstrip()
    if s.lower() in _SUBCATEGORY_VAGUE:
        return "General"
    return s


def _category_prompt_body() -> str:
    # Kept semantically aligned with the full two-tier spec; shorter to cut input tokens per chunk.
    return """Two-tier classification. Use only the input JSON fields; do not invent facts.

TIER 1 — exact "category" string (one per row):
• Financial_Payments — rent, taxes, CAM/opex, reimbursements, deposits, invoices, monetary charges; late fees/default interest/stipulated fees when the main breach impact is monetary.
• Infrastructure_Maintenance — repair, replace, upkeep; HVAC/roof/utilities serving premises/structure/parking/fixtures (physical asset condition—not buying insurance or defending third-party claims as the core duty).
• Insurance_Liability — insurance (limits, endorsements, certificates, loss duties); indemnity, hold harmless, defense, subrogation waivers; counterpart/third-party loss exposure as the main point (not ordinary rent/tax payment).
• Compliance_Administrative — reporting, books/records, notices, filings, regulatory/contract compliance, administrative audits, estoppel/cooperation letters; burden is process/disclosure vs direct money or physical repair.
• Future_Commitments — options (purchase/lease), renewals, extensions, ROFO/ROFR, future escalations or scheduled rent/charge adjustments.
• Operational_Restrictions — use of premises, exclusives, prohibited conduct, nuisance/noise/odor, signage, hazardous use, hours; assignment/subletting framed as consent/use limits (not pure payment).
• General_Legal_Provisions — dispute resolution, governing law, venue, severability, interpretation, amendments, counterparts, boilerplate when no other Tier 1 dominates.

Tie-break: several tiers apply → choose by PRIMARY impact if breached (admin step → specific fee ⇒ Financial_Payments; main risk suit/indemnity ⇒ Insurance_Liability). You must still output one Tier 1; if weak fit use General_Legal_Provisions with a precise Tier 2. Penalties with money impact ⇒ Financial_Payments (Tier 2 labels fee vs rent). Coverage vs indemnity ⇒ both Insurance_Liability (Tier 2 distinguishes).

TIER 2 — "subcategory": required every row; short root concept (Title Case or short phrase); strip location, severity, frequency. E.g. Plumbing; Odor/Nuisance; Arbitration.

Output: JSON with "classifications": [{"index": <int>, "category": "<Tier1 exact>", "subcategory": "<string>"}, ...] matching each input index."""


def _build_category_classification_prompt(items: List[Dict[str, Any]]) -> str:
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    return (
        "Classify each legal obligation below. Fields: index, DutyType, Responsible Party, "
        "Owner Responsibility, Reasoning, Citation, related_keywords.\n\n"
        f"{_category_prompt_body()}\n\n"
        f"INPUT:\n{payload}\n\n"
        'Respond with JSON only: {"classifications": [{"index": 0, "category": "Financial_Payments", "subcategory": "Rent"}, ...]}'
    )


def _parse_classifications_response(text: str) -> List[Tuple[int, str, str]]:
    raw = _strip_response_json(text or "")
    parsed = json.loads(raw)
    rows = parsed.get("classifications")
    if not isinstance(rows, list):
        raise ValueError("LLM response missing 'classifications' array")
    out: List[Tuple[int, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int):
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                continue
        cat = _normalize_category(row.get("category"))
        sub = _normalize_subcategory(row.get("subcategory"))
        out.append((idx, cat, sub))
    return out


def _classify_chunk_llm(
    items: List[Dict[str, Any]],
    *,
    model: Optional[str],
    max_output_tokens: int,
) -> List[Tuple[int, str, str]]:
    from llm_client import generate_content, get_default_model

    model_name = model or get_default_model()
    prompt = _build_category_classification_prompt(items)
    resp = generate_content(
        prompt,
        model=model_name,
        temperature=0.1,
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
    )
    return _parse_classifications_response(resp.text or "")


async def _classify_chunk_llm_async(
    items: List[Dict[str, Any]],
    *,
    model: Optional[str],
    max_output_tokens: int,
) -> List[Tuple[int, str, str]]:
    from llm_client import generate_content_async, get_default_model

    model_name = model or get_default_model()
    prompt = _build_category_classification_prompt(items)
    resp = await generate_content_async(
        prompt,
        model=model_name,
        temperature=0.1,
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
    )
    return _parse_classifications_response(resp.text or "")


async def _classify_chunks_parallel(
    chunk_specs: List[Tuple[int, List[Dict[str, Any]]]],
    *,
    model: Optional[str],
    max_output_tokens: int,
    concurrency: int,
) -> List[Tuple[int, int, List[Tuple[int, str, str]]]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run_one(spec: Tuple[int, List[Dict[str, Any]]]) -> Tuple[int, int, List[Tuple[int, str, str]]]:
        start, items = spec
        async with sem:
            res = await _classify_chunk_llm_async(
                items, model=model, max_output_tokens=max_output_tokens
            )
            return start, len(items), res

    return await asyncio.gather(*[run_one(s) for s in chunk_specs])


def _apply_llm_chunk_results(
    consolidated: List[Dict[str, Any]],
    start: int,
    chunk_len: int,
    results: List[Tuple[int, str, str]],
) -> bool:
    by_index = {t[0]: (t[1], t[2]) for t in results}
    ok = True
    for j in range(chunk_len):
        global_i = start + j
        if global_i not in by_index:
            logger.warning("Missing LLM classification for index %s", global_i)
            ok = False
            continue
        cat, sub = by_index[global_i]
        ob = consolidated[global_i]
        if isinstance(ob, dict):
            ob["category"] = cat
            ob["subcategory"] = sub
    return ok


def assign_financial_categories(consolidated: List[Dict[str, Any]], *, model: Optional[str] = None) -> None:
    """
    Assign Tier-1 category and Tier-2 subcategory in place. Uses LLM when
    LEGAL_OCR_CATEGORY_USE_LLM is true (default). On failure, falls back to heuristics for the
    whole list (subcategory is always a non-empty string after assignment).
    """
    if not consolidated:
        return

    use_llm = os.getenv("LEGAL_OCR_CATEGORY_USE_LLM", "true").lower() in ("true", "1", "yes")
    if not use_llm:
        for ob in consolidated:
            if isinstance(ob, dict):
                _assign_financial_category_heuristic(ob)
        return

    try:
        chunk_size = int(os.getenv("LEGAL_OCR_CATEGORY_LLM_CHUNK", "20"))
    except ValueError:
        chunk_size = 20
    chunk_size = max(1, min(chunk_size, 50))

    try:
        max_out = int(os.getenv("LEGAL_OCR_CATEGORY_LLM_MAX_TOKENS", "4096"))
    except ValueError:
        max_out = 4096

    chunk_specs: List[Tuple[int, List[Dict[str, Any]]]] = []
    for start in range(0, len(consolidated), chunk_size):
        chunk = consolidated[start : start + chunk_size]
        items: List[Dict[str, Any]] = []
        for j, ob in enumerate(chunk):
            global_i = start + j
            row: Dict[str, Any] = {"index": global_i}
            if isinstance(ob, dict):
                row.update(_obligation_for_prompt(ob))
            else:
                for k in _PROMPT_KEYS:
                    row[k] = None
            items.append(row)
        chunk_specs.append((start, items))

    parallel = os.getenv("LEGAL_OCR_CATEGORY_LLM_PARALLEL", "true").lower() in ("true", "1", "yes")
    try:
        conc = int(os.getenv("LEGAL_OCR_CATEGORY_LLM_CONCURRENCY", "4"))
    except ValueError:
        conc = 4
    conc = max(1, min(conc, 16))

    llm_ok = True
    t_cat0 = time.perf_counter()
    try:
        use_parallel = parallel and len(chunk_specs) > 1
        loop_running = False
        if use_parallel:
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
        if use_parallel:
            # asyncio.run() cannot be used from a running loop (e.g. FastAPI). Previously we fell
            # back to sequential chunk calls (very slow). Run the parallel coroutine in a worker
            # thread that has no loop so asyncio.run() is valid.
            if loop_running:
                logger.info(
                    "Category LLM: %d chunk(s), concurrency=%d — running parallel batches in a "
                    "background thread (caller has an active asyncio event loop)",
                    len(chunk_specs),
                    conc,
                )

                def _run_parallel_classify() -> List[Tuple[int, int, List[Tuple[int, str, str]]]]:
                    return asyncio.run(
                        _classify_chunks_parallel(
                            chunk_specs,
                            model=model,
                            max_output_tokens=max_out,
                            concurrency=conc,
                        )
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    outcomes = pool.submit(_run_parallel_classify).result()
            else:
                outcomes = asyncio.run(
                    _classify_chunks_parallel(
                        chunk_specs, model=model, max_output_tokens=max_out, concurrency=conc
                    )
                )
            for start, n_items, results in sorted(outcomes, key=lambda x: x[0]):
                if len(results) < n_items:
                    logger.warning(
                        "LLM returned %d classifications for %d obligations; falling back to heuristics for entire list",
                        len(results),
                        n_items,
                    )
                    llm_ok = False
                    break
                if not _apply_llm_chunk_results(consolidated, start, n_items, results):
                    llm_ok = False
                    break
        else:
            if len(chunk_specs) > 1:
                logger.info(
                    "Category LLM: %d chunk(s) sequential (LEGAL_OCR_CATEGORY_LLM_PARALLEL=false or only one chunk)",
                    len(chunk_specs),
                )
            for start, items in chunk_specs:
                try:
                    results = _classify_chunk_llm(items, model=model, max_output_tokens=max_out)
                except Exception as e:
                    logger.warning(
                        "LLM obligation categorization failed for chunk starting at %s: %s; falling back to heuristics for entire list",
                        start,
                        e,
                    )
                    llm_ok = False
                    break
                if len(results) < len(items):
                    logger.warning(
                        "LLM returned %d classifications for %d obligations; falling back to heuristics for entire list",
                        len(results),
                        len(items),
                    )
                    llm_ok = False
                    break
                if not _apply_llm_chunk_results(consolidated, start, len(items), results):
                    llm_ok = False
                    break
    except Exception as e:
        logger.warning(
            "LLM obligation categorization failed: %s; falling back to heuristics for entire list",
            e,
        )
        llm_ok = False

    logger.info(
        "Category assignment wall time: %.2fs (%d obligations, LLM_ok=%s)",
        time.perf_counter() - t_cat0,
        len(consolidated),
        llm_ok,
    )

    if llm_ok:
        for ob in consolidated:
            if isinstance(ob, dict):
                cat = _normalize_category(ob.get("category"))
                sub = _normalize_subcategory(ob.get("subcategory"))
                ob["category"] = cat
                ob["subcategory"] = sub

    if not llm_ok:
        for ob in consolidated:
            if isinstance(ob, dict):
                _assign_financial_category_heuristic(ob)


def group_obligations_by_category(
    obligations: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Build category -> list of obligation dicts (same object references). Normalizes Tier 1 and
    Tier 2. Unknown Tier-1 strings map to General_Legal_Provisions. Output contains exactly the
    seven canonical Tier-1 keys in CATEGORY_NEST_ORDER (empty lists allowed).
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in CATEGORY_NEST_ORDER}
    for ob in obligations:
        if not isinstance(ob, dict):
            continue
        cat = _normalize_category(ob.get("category"))
        ob["category"] = cat
        ob["subcategory"] = _normalize_subcategory(ob.get("subcategory"))
        buckets[cat].append(ob)
    return {k: buckets[k] for k in CATEGORY_NEST_ORDER}


def flatten_obligations_from_category_buckets(
    buckets: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Stable flat list: CATEGORY_NEST_ORDER, then any extra bucket keys. Use the same order when indexing
    vector chunks as when loading category-only consolidated JSON so chunk_index stays aligned.
    """
    out: List[Dict[str, Any]] = []
    for cat in CATEGORY_NEST_ORDER:
        lst = buckets.get(cat)
        if isinstance(lst, list):
            out.extend(ob for ob in lst if isinstance(ob, dict))
    for key, lst in buckets.items():
        if key in CATEGORY_NEST_ORDER:
            continue
        if isinstance(lst, list):
            out.extend(ob for ob in lst if isinstance(ob, dict))
    return out


def obligations_from_consolidated_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Obligations from a *_consolidated.json payload. Prefers legacy flat consolidated_results when present;
    otherwise flattens consolidated_results_by_category.
    """
    flat = data.get("consolidated_results")
    if isinstance(flat, list) and len(flat) > 0:
        return [x for x in flat if isinstance(x, dict)]
    buckets = data.get("consolidated_results_by_category")
    if isinstance(buckets, dict) and buckets:
        return flatten_obligations_from_category_buckets(buckets)
    return []

