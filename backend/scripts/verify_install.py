"""Quick import check after pip install. Exit 0 on success, 1 on failure."""
from __future__ import annotations

import sys

CHECKS = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("chromadb", "chromadb"),
    ("qdrant_client", "qdrant_client"),
    ("litellm", "litellm"),
    ("mem0", "mem0ai"),
    ("sentence_transformers", "sentence-transformers"),
    ("legal_rag.pipeline", "legal_rag (chat RAG)"),
    ("memory_management", "memory_management"),
    ("process_legal_documents", "process_legal_documents"),
    ("query_system", "query_system (API)"),
]


def main() -> int:
    failed = []
    for module, label in CHECKS:
        try:
            __import__(module)
            print(f"  OK  {label}")
        except Exception as e:
            print(f"  FAIL {label}: {e}")
            failed.append(label)

    if failed:
        print(f"\n{len(failed)} check(s) failed.", file=sys.stderr)
        return 1
    print("\nAll import checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
