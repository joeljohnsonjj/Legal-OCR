#!/usr/bin/env python3
"""
Debug: show the exact page text the pipeline feeds to analyze_page (same as PDFProcessor.process_pdf).

Usage:
  python scripts/debug_pdf_page_text.py path/to/file.pdf --page 4
  python scripts/debug_pdf_page_text.py path/to/file.pdf --page 4 --no-cache

From repo root; loads .env if present. Uses ocr_cache next to repo unless PDF_OCR_CACHE_FOLDER is set.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

os.chdir(_REPO)

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Dump PDF page text as seen by process_legal_documents.")
    ap.add_argument("pdf", type=Path, help="Path to PDF")
    ap.add_argument("--page", type=int, default=4, help="1-based page number (default: 4)")
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore ocr_cache *_ocr.txt so you re-run OCR/text extraction fresh",
    )
    args = ap.parse_args()

    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        print(f"ERROR: PDF not found: {pdf}")
        sys.exit(1)

    cache_root = Path(os.getenv("PDF_OCR_CACHE_FOLDER", str(_REPO / "ocr_cache"))).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    # Import after chdir
    from process_legal_documents import PDFProcessor  # noqa: E402

    proc = PDFProcessor(cache_folder=str(cache_root))

    if args.no_cache:
        cache_file = proc.get_cache_path(str(pdf))
        if cache_file.exists():
            cache_file.unlink()
            print(f"Removed cache file: {cache_file}")

    print("=== PDF page text debug ===")
    print("pdf:", pdf)
    print("cache_folder:", cache_root)
    print()

    is_text = proc.is_text_based_pdf(str(pdf))
    print("is_text_based_pdf (first pages heuristic):", is_text)
    print()

    page_texts = proc.process_pdf(str(pdf))
    if not page_texts:
        print("ERROR: process_pdf returned no pages (empty dict).")
        sys.exit(2)

    keys = sorted(page_texts.keys())
    print("pages returned:", len(keys), "range:", keys[0], "..", keys[-1])
    p = args.page
    if p not in page_texts:
        print(f"ERROR: page {p} not in keys. Available: {keys[:20]}{'...' if len(keys) > 20 else ''}")
        sys.exit(3)

    text = page_texts[p] or ""
    stripped = text.strip()
    print()
    print(f"--- Page {p} ---")
    print("char_len (raw):", len(text))
    print("char_len (stripped):", len(stripped))
    print("lines:", text.count("\n") + 1 if text else 0)
    # Heuristic: almost empty?
    if len(stripped) < 80:
        print("WARNING: very little text — LLM will often return [].")
    # Markers user expects on lease p4
    markers = [
        "LANDLORD",
        "Landlord",
        "HAZARDOUS",
        "hazardous",
        "OPERATING EXPENSE",
        "Operating Expense",
        "Section 6",
        "Section 7",
        "utilities",
        "HVAC",
    ]
    low = text.lower()
    print("marker hits (substring):")
    for m in markers:
        hit = m.lower() in low
        print(f"  {m!r}: {'yes' if hit else 'no'}")
    print()
    print("--- first 1200 chars (repr-ish preview) ---")
    preview = text[:1200].replace("\r\n", "\n")
    print(preview)
    if len(text) > 1200:
        print(f"\n... [{len(text) - 1200} more chars] ...\n")
        print("--- last 600 chars ---")
        print(text[-600:].replace("\r\n", "\n"))

    # Optional: show cache source
    cp = proc.get_cache_path(str(pdf))
    print()
    print("OCR cache path:", cp)
    print("cache exists:", cp.exists())


if __name__ == "__main__":
    main()
