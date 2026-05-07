"""
Grouped processing results: results[] + root citations; flatten for vector indexing.
Inner obligations use `citations`: [{ docId, references: [{ page, section }] }].
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

from citation_utils import _parse_page_numbers, _parse_sections

from processing_taxonomy import category_labels_ordered, normalize_processing_category

logger = logging.getLogger(__name__)

# Primary roles (lowercase). Fallback labels use Title Case — see PARTY_UNSPECIFIED / PARTY_UNIDENTIFIED.
ROLE_TENANT = "tenant"
ROLE_LANDLORD = "landlord"
ROLE_PREVAILING = "prevailing party"
ROLE_NON_PREVAILING = "non prevailing party"

# When the model / pipeline cannot assign a party.
PARTY_UNSPECIFIED = "Unspecified Party"  # empty or missing Responsible Party (matches prompt.txt)
PARTY_UNIDENTIFIED = "Unidentified"  # non-empty value that could not be classified


def _canonical_role_exact(sl: str) -> Optional[str]:
    """Exact match (already lowercased) to known roles."""
    if sl in (ROLE_TENANT, ROLE_LANDLORD, ROLE_PREVAILING, ROLE_NON_PREVAILING):
        return sl
    return None


def normalize_responsible_party_value(
    raw: Any,
    *,
    party_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Normalize Responsible Party to a tenant/landlord/prevailing role when possible,
    map legal names via party_metadata, or mark Unspecified / Unidentified.
    """
    s = str(raw or "").strip()
    sl = s.lower()

    if not s:
        return PARTY_UNSPECIFIED

    if sl in ("unspecified", "unspecified party"):
        return PARTY_UNSPECIFIED
    if sl in ("unidentified", "unknown"):
        return PARTY_UNIDENTIFIED

    canon = _canonical_role_exact(sl)
    if canon is not None:
        return canon

    if "non" in sl and "prevailing" in sl:
        return ROLE_NON_PREVAILING
    if "prevailing" in sl:
        return ROLE_PREVAILING
    if "landlord" in sl or "lessor" in sl:
        return ROLE_LANDLORD
    if "tenant" in sl or "lessee" in sl:
        return ROLE_TENANT

    resolved = map_party_via_metadata(s, party_metadata)
    if resolved:
        return resolved

    logger.warning(
        "Could not classify Responsible Party %r — using %s (populate party_metadata actual_name or use explicit role labels).",
        s,
        PARTY_UNIDENTIFIED,
    )
    return PARTY_UNIDENTIFIED


def _norm_party_compare(s: str) -> str:
    """Lowercase, strip punctuation noise for comparing names and labels."""
    return " ".join(re.sub(r"[^\w\s]+", " ", (s or "").lower()).split())


def _role_from_reference_label(ref_label: str) -> Optional[str]:
    """Infer canonical role from metadata keys such as 'Tenant', 'Landlord', 'Prevailing Party'."""
    k = (ref_label or "").strip().lower()
    if not k:
        return None
    if "non" in k and "prevailing" in k:
        return "non prevailing party"
    if "prevailing" in k:
        return "prevailing party"
    if "landlord" in k or "lessor" in k:
        return "landlord"
    if "tenant" in k or "lessee" in k:
        return "tenant"
    return None


def map_party_via_metadata(raw: str, meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    If the model put a legal name in Responsible Party, map it to a role using `party_metadata`
    from extraction: each entry is { reference_label: { "actual_name": "..." } }.
    Also matches when the raw string is a substring/superstring of actual_name (legal names vs.
    shortened extractions). Returns None if no match (caller must not guess blindly).
    """
    if not meta or not str(raw or "").strip():
        return None
    rnorm = _norm_party_compare(str(raw).strip())
    if not rnorm:
        return None
    for ref_label, info in meta.items():
        if not isinstance(info, dict):
            continue
        role = _role_from_reference_label(str(ref_label))
        if not role:
            continue
        an = (info.get("actual_name") or "").strip()
        if an:
            anorm = _norm_party_compare(an)
            if anorm and anorm == rnorm:
                return role
            # "Fidelity Funding Company" vs "Fidelity Funding Company a Nevada Corporation"
            if len(anorm) >= 8 and len(rnorm) >= 6 and (anorm in rnorm or rnorm in anorm):
                return role
            head = anorm.split(",")[0].strip()
            if len(head) >= 8 and head in rnorm:
                return role
        if _norm_party_compare(str(ref_label)) == rnorm:
            return role
    return None


# Exhibit B–style work letter: lease often says "Landlord will perform … at Landlord's sole cost"
# while bullets omit "Landlord". Model may leave Responsible Party unclassified → Unidentified.
_EXHIBIT_B_OR_TI = re.compile(r"exhibit\s*b|tenant\s+improvements", re.I)
_LANDLORD_DELIVERY_DUTY = re.compile(
    r"landlord\s+(?:will|must|shall)\s+(?:perform|provide)|"
    r"landlord\s+is\s+responsible\s+for|"
    r"landlord'?s\s+sole\s+cost|"
    r"landlord\s+must\s+provide|"
    r"performed\s+by\s+landlord|"
    r"at\s+landlord'?s\s+sole\s+cost",
    re.I,
)


def _obligation_text_blob_for_party_inference(ob: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("Owner Responsibility", "Reasoning"):
        v = ob.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x is not None)
        elif v is not None:
            parts.append(str(v))
    return " ".join(parts)


def _citation_sections_blob(ob: Dict[str, Any]) -> str:
    chunks: List[str] = []
    raw = ob.get("citations") or ob.get("Citation") or ob.get("Citations")
    if isinstance(raw, list):
        for block in raw:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue
            sec = block.get("section")
            if isinstance(sec, list):
                chunks.extend(str(s) for s in sec if s is not None)
            elif sec is not None:
                chunks.append(str(sec))
    elif raw is not None:
        chunks.append(str(raw))
    return " ".join(chunks)


def infer_party_from_obligation_evidence(ob: Dict[str, Any]) -> Optional[str]:
    """
    When normalization yields Unidentified, infer landlord/tenant only from strong document cues.
    Used for Exhibit B Tenant Improvements: duties cite that exhibit while bullets omit the party.
    """
    cite_blob = _citation_sections_blob(ob)
    if not _EXHIBIT_B_OR_TI.search(cite_blob):
        return None
    text = (_obligation_text_blob_for_party_inference(ob) + " " + cite_blob).lower()
    if _LANDLORD_DELIVERY_DUTY.search(text):
        return ROLE_LANDLORD
    # Reasoning often states landlord delivery even when duty lines are bare imperatives
    if re.search(r"\blandlord\b", text) and not re.search(
        r"\btenant\s+(?:must|shall|will|agrees\s+to)\s+(?:pay|perform|construct)\b", text
    ):
        if text.count("landlord") >= max(1, text.count("tenant") - 2):
            return ROLE_LANDLORD
    return None


def normalize_responsible_party_for_obligation(
    ob: Dict[str, Any],
    *,
    party_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Set canonical Responsible Party; optional party_metadata maps legal names to roles."""
    base = normalize_responsible_party_value(ob.get("Responsible Party"), party_metadata=party_metadata)
    if base != PARTY_UNIDENTIFIED:
        return base
    inferred = infer_party_from_obligation_evidence(ob)
    return inferred if inferred else base


def normalize_party_fields_in_groups(
    groups: List[Dict[str, Any]],
    *,
    party_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """In-place: normalize Responsible Party on every obligation in category groups."""
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        for ob in g.get("obligations") or []:
            if isinstance(ob, dict):
                ob["Responsible Party"] = normalize_responsible_party_for_obligation(
                    ob, party_metadata=party_metadata
                )


def _party_key(ob: Dict[str, Any], *, party_metadata: Optional[Dict[str, Any]] = None) -> str:
    return normalize_responsible_party_for_obligation(ob, party_metadata=party_metadata)


def _merge_str_lists(a: Any, b: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for src in (a, b):
        if isinstance(src, list):
            for x in src:
                s = str(x).strip()
                if s and s.lower() not in seen:
                    seen.add(s.lower())
                    out.append(s)
        elif src is not None:
            s = str(src).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


# --- Related keywords: strip LLM/tokenizer noise; add broad lease-retrieval phrases ---------------

RELATED_KW_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "as",
        "at",
        "by",
        "for",
        "in",
        "is",
        "it",
        "of",
        "on",
        "to",
        "so",
        "no",
        "be",
        "are",
        "was",
        "were",
        "been",
        "being",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "may",
        "can",
        "any",
        "all",
        "not",
        "this",
        "that",
        "with",
        "from",
        "into",
        "over",
        "under",
        "per",
        "via",
        "also",
        "such",
        "each",
        "both",
        "nor",
        "yet",
        "then",
        "than",
        "when",
        "where",
        "which",
        "who",
        "how",
        "about",
        "after",
        "before",
        "between",
        "through",
        "during",
        "upon",
        "within",
        "without",
        "caused",
        "cause",
        "including",
        "other",
        "same",
        "some",
        "only",
        "just",
        "etc",
    }
)


def _related_kw_alphanumeric_core(s: str) -> str:
    return re.sub(r"[^\w]+", "", (s or "").lower())


def _is_junk_related_keyword(s: str) -> bool:
    t = " ".join(str(s).split()).strip()
    if len(t) < 2:
        return True
    if t in ("&", "|", "-", "–", "—", "/", "\\"):
        return True
    # Pure punctuation / numbering fragments from sloppy extraction
    if re.match(r"^[\s\W\d]{1,5}$", t):
        return True
    if re.match(r"^\(?\d+[a-z]?\)?$", t, re.I):
        return True
    core = _related_kw_alphanumeric_core(t)
    if len(core) <= 1:
        return True
    if " " not in t:
        tok = re.sub(r"[^\w]+$", "", t.lower())
        tok = re.sub(r"^[^\w]+", "", tok)
        if tok in RELATED_KW_STOPWORDS:
            return True
        if len(tok) <= 2:
            return True
    return False


def sanitize_related_keywords_list(keywords: List[str], *, max_items: int = 32) -> List[str]:
    """Drop meaningless chips (&, lone punctuation, stopwords); dedupe case-insensitively."""
    out: List[str] = []
    seen: Set[str] = set()
    for k in keywords or []:
        s = str(k).strip()
        if _is_junk_related_keyword(s):
            continue
        key = " ".join(s.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(" ".join(s.split()))
        if len(out) >= max_items:
            break
    return out


def _owner_text_blob(owner_responsibility: Any) -> str:
    if isinstance(owner_responsibility, list):
        return " ".join(str(x) for x in owner_responsibility[:24] if x is not None)
    return str(owner_responsibility or "")


# Phrases appended for embedding recall: Chroma uses dense vectors over the full chunk (DutyType +
# keywords + duty snippet), not literal keyword matching—rich overlap with natural queries matters.
_KEYWORDS_UTILITIES_BUILDING_SYSTEMS = (
    "utilities",
    "utility services",
    "utility charges",
    "utility costs",
    "electric service",
    "electrical service",
    "electricity",
    "power supply",
    "natural gas",
    "gas service",
    "water service",
    "water and sewer",
    "sewer",
    "sewage",
    "storm drain",
    "meter",
    "submeter",
    "hvac",
    "heating and cooling",
    "heating ventilation air conditioning",
    "climate control",
    "air conditioning",
    "ventilation",
    "plumbing",
    "plumbing systems",
    "mechanical systems",
    "building mechanical systems",
    "building systems",
    "sprinkler system",
    "fire suppression",
    "boiler",
    "chiller",
    "risers",
)


def broaden_related_keywords(
    *,
    category: str = "",
    owner_responsibility: Any = None,
    base_keywords: Optional[List[str]] = None,
    max_items: int = 64,
) -> List[str]:
    """
    Clean model-generated keywords, then add taxonomy + duty-derived phrases so semantic search
    matches user queries when embeddings are driven primarily by ``related_keywords``
    (see ``vector_store.obligation_to_keyword_chunk_text``).

    Embeddings use the joined keyword list by default; broad synonym bundles matter more because
    duty-line snippets are no longer mixed into the vector by default.
    """
    # Keep only best model chips; reserve budget for taxonomy + domain bundles (fixes starvation).
    kb_budget = min(20, max(6, max_items // 3))
    cleaned = sanitize_related_keywords_list(list(base_keywords or []), max_items=kb_budget)
    seeds: List[str] = []

    cat = (category or "").strip()
    cat_l = cat.lower()
    if cat:
        seeds.append(cat.lower())
        for segment in re.split(r"\s*&\s*", cat):
            seg = segment.strip()
            if len(seg) >= 3 and not _is_junk_related_keyword(seg):
                seeds.append(seg.lower())

    blob_l = _owner_text_blob(owner_responsibility).lower()
    domain: List[str] = []

    def add(*phrases: str) -> None:
        for ph in phrases:
            ph = ph.strip()
            if ph and not _is_junk_related_keyword(ph):
                domain.append(ph)

    if any(
        x in blob_l
        for x in (
            " rent",
            "rent ",
            "rental",
            "base rent",
            "additional rent",
            "monthly rent",
        )
    ):
        add("rent", "rent payment", "base rent", "additional rent", "lease payment")
    if any(x in blob_l for x in ("maintain", "maintenance", "repair", "hvac", "plumb", "electr")):
        add("maintenance", "repairs", "building maintenance", "premises upkeep")
    if "insurance" in blob_l:
        add("insurance", "liability", "coverage", "policy")
    if "indemnif" in blob_l:
        add("indemnification", "hold harmless")
    if any(x in blob_l for x in ("tax", "taxes", "assessment")):
        add("taxes", "real estate taxes", "assessment")

    # Utilities & building systems: broad overlap with "utilities" queries (incl. HVAC / plumbing).
    util_blob = any(
        x in blob_l
        for x in (
            "utility",
            "utilities",
            "electric",
            "electrical",
            "water ",
            "water.",
            " gas",
            "gas ",
            "sewer",
            "sewage",
            "meter",
            "submeter",
            "steam",
        )
    )
    util_cat = any(
        p in cat_l
        for p in (
            "utilit",
            "hvac",
            "plumb",
            "electric",
            "mechanical",
            "sprinkler",
            "water &",
            "gas &",
            "building service",
            "heat &",
            "cool",
        )
    )
    mech_blob = any(
        x in blob_l
        for x in (
            "hvac",
            "plumb",
            "plumbing",
            "heating",
            "cooling",
            "ventilat",
            "air condition",
            "boiler",
            "chiller",
            "sprinkler",
            "riser",
            "drainage",
            "drain ",
            "plumbing fixture",
            "mechanical",
            "condens",
            "compressor",
        )
    )
    if util_blob or util_cat or mech_blob:
        add(*_KEYWORDS_UTILITIES_BUILDING_SYSTEMS)

    if any(x in blob_l for x in ("default", "breach", "cure")):
        add("lease default", "cure period", "breach")
    if "deposit" in blob_l or "security deposit" in blob_l:
        add("security deposit")
    if any(x in blob_l for x in ("alteration", "improvement", "tenant improvement")):
        add("tenant improvements", "alterations")
    if any(x in blob_l for x in ("compliance", "ordinance", "statute", "law")):
        add("compliance", "legal requirements")
    if "sign" in blob_l or "signage" in blob_l:
        add("signage")
    if any(x in blob_l for x in ("landlord", "lessor")):
        add("landlord obligations")
    if any(x in blob_l for x in ("tenant", "lessee")):
        add("tenant obligations")

    merged = seeds + domain + cleaned
    return sanitize_related_keywords_list(merged, max_items=max_items)


def _get_citations_field(ob: Dict[str, Any]) -> Any:
    if "citations" in ob and ob["citations"] is not None:
        return ob["citations"]
    return ob.get("Citation") or ob.get("Citations")


def _ref_key(doc: str, page: int, section: str) -> Tuple[str, int, str]:
    return (doc.strip().lower(), int(page), (section or "").strip().lower())


def _normalize_citations_list(raw: Any, fallback_doc: str) -> List[Dict[str, Any]]:
    """Return list of { docId, references: [{ page, section }] }."""
    if not isinstance(raw, list) or not raw:
        return []
    out: List[Dict[str, Any]] = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        doc = str(block.get("docId") or block.get("doc_id") or "").strip() or fallback_doc
        refs: List[Dict[str, Any]] = []
        
        # NEW FORMAT: Direct page/section in citation object (preferred)
        if "page" in block and "section" in block:
            # Extract page and section directly from the citation object
            p = block.get("page")
            try:
                pi = int(p) if p is not None else 0
            except (TypeError, ValueError):
                pi = 0
            sec = str(block.get("section") or "").strip()
            if pi > 0 or sec:
                refs.append({"page": pi, "section": sec})
        
        # OLD FORMAT: Nested references array (fallback)
        if not refs:
            refs_in = block.get("references")
            if isinstance(refs_in, list):
                for r in refs_in:
                    if not isinstance(r, dict):
                        continue
                    p = r.get("page")
                    try:
                        pi = int(p) if p is not None else 0
                    except (TypeError, ValueError):
                        pi = 0
                    sec = str(r.get("section") or "").strip()
                    if pi > 0 or sec:
                        refs.append({"page": pi, "section": sec})
        
        # LEGACY FORMAT: pageNumbers + section (oldest fallback); pageNumbers may be comma-separated string
        if not refs:
            pn = block.get("pageNumbers") or block.get("page_numbers")
            pages = _parse_page_numbers(pn)
            sec_raw = block.get("section")
            if isinstance(sec_raw, list):
                secs = [str(s).strip() for s in sec_raw if str(s).strip()]
            else:
                secs = _parse_sections(sec_raw)
            if not secs:
                secs = [""]
            for p in sorted(set(pages)) if pages else [0]:
                for s in secs:
                    if p > 0 or s:
                        refs.append({"page": p, "section": s})
        
        if doc or refs:
            out.append({"docId": doc or fallback_doc, "references": refs})
    return out


def _merge_citations_lists(a: Any, b: Any, fallback_doc: str) -> List[Dict[str, Any]]:
    la = _normalize_citations_list(a, fallback_doc)
    lb = _normalize_citations_list(b, fallback_doc)
    seen: set = set()
    merged: List[Dict[str, Any]] = []
    by_doc: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for block in la + lb:
        if not isinstance(block, dict):
            continue
        doc = str(block.get("docId") or "").strip() or fallback_doc
        by_doc.setdefault(doc, [])
        for ref in block.get("references") or []:
            if not isinstance(ref, dict):
                continue
            try:
                p = int(ref.get("page") or 0)
            except (TypeError, ValueError):
                p = 0
            sec = str(ref.get("section") or "").strip()
            k = _ref_key(doc, p, sec)
            if k in seen:
                continue
            if p <= 0 and not sec:
                continue
            seen.add(k)
            by_doc[doc].append({"page": p, "section": sec})
    for doc, refs in by_doc.items():
        if refs:
            merged.append({"docId": doc, "references": refs})
    return merged


def merge_duplicate_party_within_category(
    results: List[Dict[str, Any]],
    *,
    default_doc: str = "",
    party_metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Within each category group, merge obligations with the same Responsible Party."""
    fb = (default_doc or "").strip()
    out: List[Dict[str, Any]] = []
    for grp in results or []:
        if not isinstance(grp, dict):
            continue
        cat = normalize_processing_category(grp.get("category"))
        obs_in = grp.get("obligations")
        if not isinstance(obs_in, list):
            out.append({"category": cat, "obligations": []})
            continue
        buckets: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for ob in obs_in:
            if not isinstance(ob, dict):
                continue
            pk = _party_key(ob, party_metadata=party_metadata) or "__unknown__"
            if pk not in buckets:
                o2 = dict(ob)
                o2["Responsible Party"] = normalize_responsible_party_for_obligation(
                    ob, party_metadata=party_metadata
                )
                o2["Owner Responsibility"] = _merge_str_lists([], ob.get("Owner Responsibility"))
                o2["Reasoning"] = _merge_str_lists([], ob.get("Reasoning"))
                o2["related_keywords"] = _merge_str_lists([], ob.get("related_keywords"))
                o2["citations"] = _normalize_citations_list(_get_citations_field(ob), fb)
                for k in ("Citation", "Citations", "DutyType", "financial_category", "financial_subcategory", "category", "subcategory"):
                    o2.pop(k, None)
                buckets[pk] = o2
                continue
            acc = buckets[pk]
            acc["Owner Responsibility"] = _merge_str_lists(acc.get("Owner Responsibility"), ob.get("Owner Responsibility"))
            acc["Reasoning"] = _merge_str_lists(acc.get("Reasoning"), ob.get("Reasoning"))
            acc["related_keywords"] = _merge_str_lists(acc.get("related_keywords"), ob.get("related_keywords"))
            acc["citations"] = _merge_citations_lists(acc.get("citations"), _get_citations_field(ob), fb)
        out.append({"category": cat, "obligations": list(buckets.values())})
    return out


def normalize_results_categories(results: List[Dict[str, Any]]) -> None:
    for grp in results or []:
        if isinstance(grp, dict):
            grp["category"] = normalize_processing_category(grp.get("category"))


def build_root_citations_api_shape(
    results: List[Dict[str, Any]],
    default_doc_id: str,
) -> List[Dict[str, Any]]:
    """
    Root-level citations: DISABLED - citations are already attached to each obligation.
    Returning empty list to avoid redundant citation data.
    """
    return []  # Skip root-level citations since they're already in obligations
    fallback = (default_doc_id or "").strip()
    merged: List[Dict[str, Any]] = []
    for grp in results or []:
        if not isinstance(grp, dict):
            continue
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            for block in _normalize_citations_list(_get_citations_field(ob), fallback):
                doc = str(block.get("docId") or "").strip() or fallback
                for ref in block.get("references") or []:
                    if not isinstance(ref, dict):
                        continue
                    try:
                        p = int(ref.get("page") or 0)
                    except (TypeError, ValueError):
                        p = 0
                    sec = str(ref.get("section") or "").strip()
                    if p > 0 or sec:
                        merged.append({"docId": doc, "pageNumbers": [p] if p else [], "section": [sec] if sec else [""]})

    by_doc: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    seen_pairs: set = set()
    for c in merged:
        if not isinstance(c, dict):
            continue
        doc_id = str(c.get("docId") or "").strip()
        if not doc_id:
            continue
        pages = []
        for x in c.get("pageNumbers") or []:
            try:
                pages.append(int(x))
            except (TypeError, ValueError):
                continue
        pages = sorted(set(pages))
        secs = [str(s).strip() for s in (c.get("section") or []) if str(s).strip()]
        refs = by_doc.setdefault(doc_id, [])
        for p in pages:
            if secs:
                for s in secs:
                    key = (doc_id, p, s.lower())
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    refs.append({"page": p, "section": s})
            else:
                key = (doc_id, p, "")
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                refs.append({"page": p, "section": ""})
    return [{"docId": d, "references": r} for d, r in by_doc.items() if r]


def flatten_processing_results_for_index(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Stable flat rows for Chroma: taxonomy order of groups, then obligation order.
    DutyType = processing category (search metadata).
    """
    flat: List[Dict[str, Any]] = []
    for grp in results or []:
        if not isinstance(grp, dict):
            continue
        cat = str(grp.get("category") or "").strip()
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            row = dict(ob)
            row["DutyType"] = cat
            if "related_keywords" not in row or not row.get("related_keywords"):
                or_lines = row.get("Owner Responsibility")
                row["related_keywords"] = broaden_related_keywords(
                    category=cat,
                    owner_responsibility=or_lines,
                    base_keywords=[],
                    max_items=64,
                )
            # Chroma path still reads legacy Citation in metadata — mirror citations
            if row.get("citations") and not row.get("Citation"):
                row["Citation"] = row["citations"]
            flat.append(row)
    return flat


def count_obligations_in_results(results: List[Dict[str, Any]]) -> int:
    n = 0
    for grp in results or []:
        if isinstance(grp, dict) and isinstance(grp.get("obligations"), list):
            n += sum(1 for x in grp["obligations"] if isinstance(x, dict))
    return n


def obligations_from_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten grouped results to a list (add _processing_category)."""
    out: List[Dict[str, Any]] = []
    for grp in results or []:
        if not isinstance(grp, dict):
            continue
        cat = str(grp.get("category") or "").strip()
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            o2 = dict(ob)
            o2["_processing_category"] = cat
            out.append(o2)
    return out


def _flatten_obligations_from_category_buckets(buckets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Stable flat list from legacy Tier-1 bucket dict."""
    out: List[Dict[str, Any]] = []
    for lst in buckets.values():
        if isinstance(lst, list):
            out.extend(x for x in lst if isinstance(x, dict))
    return out


def obligations_from_consolidated_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Obligations from a *_consolidated.json payload. Prefers top-level grouped ``results``,
    then legacy flat ``consolidated_results``, then bucket dicts.

    When using grouped ``results``, applies ``normalize_party_fields_in_groups`` in place so
    saved files produced before metadata/inference improvements still resolve roles at read time.
    """
    res = data.get("results")
    if isinstance(res, list) and res:
        normalize_party_fields_in_groups(res, party_metadata=data.get("party_metadata"))
        out = obligations_from_results(res)
        if out:
            return out

    flat = data.get("consolidated_results")
    if isinstance(flat, list) and len(flat) > 0:
        return [x for x in flat if isinstance(x, dict)]

    fb = data.get("consolidated_results_by_facility_category")
    if isinstance(fb, dict) and fb and any(isinstance(v, list) and len(v) > 0 for v in fb.values()):
        order = category_labels_ordered()
        out_fb: List[Dict[str, Any]] = []
        for k in order:
            lst = fb.get(k)
            if isinstance(lst, list):
                out_fb.extend(x for x in lst if isinstance(x, dict))
        for k, lst in fb.items():
            if k in order:
                continue
            if isinstance(lst, list):
                out_fb.extend(x for x in lst if isinstance(x, dict))
        return out_fb
    buckets = data.get("consolidated_results_by_category")
    if isinstance(buckets, dict) and buckets:
        return _flatten_obligations_from_category_buckets(buckets)
    return []
