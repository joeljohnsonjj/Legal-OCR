"""
Normalize structured obligation citations: one object per document (merged pageNumbers + sections).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Tuple


def merge_structured_citations_by_doc_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Combine citation dicts that share the same docId into a single entry.

    Example: two objects both with docId "Lease.pdf" become one with union of
    pageNumbers (sorted, unique) and section labels (order preserved, case-insensitive dedupe).
    """
    if not items:
        return []
    buckets: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for c in items:
        if not isinstance(c, dict):
            continue
        doc = str(c.get("docId") or c.get("doc_id") or "").strip()
        if doc not in buckets:
            buckets[doc] = {"docId": doc, "pages": set(), "sections": [], "seen_sec_lower": set()}
        b = buckets[doc]
        raw_pages = c.get("pageNumbers") or c.get("page_numbers") or []
        for x in raw_pages:
            try:
                b["pages"].add(int(x))
            except (TypeError, ValueError):
                continue
        for s in c.get("section") or []:
            ss = str(s).strip()
            if not ss:
                continue
            low = ss.lower()
            if low not in b["seen_sec_lower"]:
                b["seen_sec_lower"].add(low)
                b["sections"].append(ss)
    out: List[Dict[str, Any]] = []
    for b in buckets.values():
        out.append(
            {
                "docId": b["docId"],
                "pageNumbers": sorted(b["pages"]),
                "section": b["sections"],
            }
        )
    return out


def _split_csvish(s: str) -> List[str]:
    if not isinstance(s, str):
        return []
    raw = s.replace(";", ",")
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _parse_page_numbers(val: Any) -> List[int]:
    pages: List[int] = []
    if val is None:
        return pages
    if isinstance(val, int):
        return [val] if val > 0 else []
    if isinstance(val, list):
        for x in val:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i > 0:
                pages.append(i)
        return pages
    if isinstance(val, str):
        for p in _split_csvish(val):
            try:
                i = int(p)
            except (TypeError, ValueError):
                continue
            if i > 0:
                pages.append(i)
        return pages
    try:
        i = int(val)
        return [i] if i > 0 else []
    except (TypeError, ValueError):
        return []


def _parse_sections(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        out: List[str] = []
        for x in val:
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    if isinstance(val, str):
        parts = _split_csvish(val)
        return parts if parts else ([val.strip()] if val.strip() else [])
    s = str(val).strip()
    return [s] if s else []


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for s in items:
        ss = str(s).strip()
        if not ss:
            continue
        low = ss.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(ss)
    return out


def _collect_doc_refs_from_any_shape(raw: Any, fallback_doc_id: str) -> List[Tuple[str, int, str]]:
    """
    Return a flat list of (docId, page, section) refs from mixed citation shapes:
    - [{docId, references:[{page,section}]}]
    - [{pageNumbers:[...], section:[...]}] (+ optional top-level docId elsewhere)
    - [{page, section}]
    - [{docId, pageNumbers:"4,5", section:"A,B"}] (already-new shape)
    """
    out: List[Tuple[str, int, str]] = []
    if not isinstance(raw, list) or not raw:
        return out
    fb = str(fallback_doc_id or "").strip()
    for block in raw:
        if not isinstance(block, dict):
            continue
        doc = str(block.get("docId") or block.get("doc_id") or "").strip() or fb
        # Shape 1: nested references
        refs = block.get("references")
        if isinstance(refs, list) and refs:
            for r in refs:
                if not isinstance(r, dict):
                    continue
                pages = _parse_page_numbers(r.get("page"))
                secs = _parse_sections(r.get("section"))
                if not pages:
                    pages = [0]
                if not secs:
                    secs = [""]
                for p in pages:
                    for s in secs:
                        if p > 0 or s:
                            out.append((doc, int(p), str(s)))
            continue

        # Shape 2: direct page/section
        if "page" in block or "section" in block:
            pages = _parse_page_numbers(block.get("page"))
            secs = _parse_sections(block.get("section"))
            if not pages:
                pages = [0]
            if not secs:
                secs = [""]
            for p in pages:
                for s in secs:
                    if p > 0 or s:
                        out.append((doc, int(p), str(s)))
            continue

        # Shape 3: pageNumbers + section
        pages = _parse_page_numbers(block.get("pageNumbers") or block.get("page_numbers"))
        secs = _parse_sections(block.get("section"))
        if not pages:
            pages = [0]
        if not secs:
            secs = [""]
        for p in pages:
            for s in secs:
                if p > 0 or s:
                    out.append((doc, int(p), str(s)))
    return out


def combine_citations_to_comma_separated(
    raw: Any,
    *,
    fallback_doc_id: str = "",
) -> List[Dict[str, str]]:
    """
    Combine multiple responsibility-level citations into one per docId, using comma-separated strings:

    [
      { "docId": "...pdf", "pageNumbers": "4,5,6", "section": "6,5.rent,6.kjsnak" }
    ]
    """
    refs = _collect_doc_refs_from_any_shape(raw, fallback_doc_id)
    if not refs:
        return []
    by_doc: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for doc, page, sec in refs:
        d = str(doc or fallback_doc_id or "").strip()
        if d not in by_doc:
            by_doc[d] = {"pages": set(), "sections": []}
        if isinstance(page, int) and page > 0:
            by_doc[d]["pages"].add(page)
        s = str(sec or "").strip()
        if s:
            by_doc[d]["sections"].append(s)
    out: List[Dict[str, str]] = []
    for doc, acc in by_doc.items():
        pages_sorted = sorted(acc["pages"])
        secs = _dedupe_preserve_order(acc["sections"])
        out.append(
            {
                "docId": doc,
                "pageNumbers": ",".join(str(p) for p in pages_sorted),
                "section": ",".join(secs),
            }
        )
    return out
