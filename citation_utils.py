"""
Normalize structured obligation citations: one object per document (merged pageNumbers + sections).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List


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
