# Legal-OCR chatbot — overview

## What this system does

Lease PDFs are processed into text and **extracted obligations** (who pays what, who maintains what, and similar duties). The chatbot lets someone **ask a question in plain language** and get an answer that is **based on the processed document**, not on general legal advice from the internet.

---

## The router: financial vs general questions

Before anything is retrieved, a **router** (a small language-model step) reads the user’s message once and chooses **one** of two search modes. It does not answer the question yet—it only decides **where** to look.

| Mode | Typical questions | What we search |
|------|-------------------|----------------|
| **Financial / obligations** | Rent, deposits, fees, insurance costs, operating expenses, who pays whom, late charges, reimbursement | **Extracted obligations** — the structured duties that were consolidated from the lease (each duty is stored as its own searchable item) |
| **General document** | Pets, smoking, termination notice, definitions, parties, use of premises, notices, dispute clauses, non-money rules | **Full page text** — the raw text of each page, as if you flipped through the PDF |

**How the router decides:** It is instructed to send money-related questions (anything where payment, cost, or monetary obligation is central) to the **financial** track. It sends everything else that is about clauses, rules, or wording to the **general** track. If a question mixes both, **financial wins** when money is central.

---

## How information is fetched (end to end)

Nothing is “read” from the PDF at question time. The chat uses **indexes** that were built when the document was processed. The flow is:

### 1. Turn the question into a search vector

The user’s question is converted into a **numerical embedding** (a fingerprint of meaning). Same kind of embedding was used when the document was indexed, so “security deposit” and “deposit amount” can match related content even if the words differ slightly.

### 2. Search only the chosen track

The system searches a **vector database** with a **hard filter**:

- After a **financial** routing, search only **obligation** records.
- After a **general** routing, search only **full-page** records.

So financial and general answers do not get mixed in the same search step—the router picks one lane, and only that lane is queried.

Optionally, the request can **limit to one document** (for example a specific PDF name), so results are not blended across many leases.

### 3. Take the best matches (“top” results)

The database returns the **most similar** items to the question, up to a set limit (for example the top 8). Each item includes a **score** (how close the match is).

### 3a. Summary mode for list-all questions

When the user asks to **list all obligations** or asks for an overview, the router enables **summary mode**:

- The search uses a higher **top-k** (200) to estimate coverage.
- A **relative cutoff** keeps only results within a score band of the best match so weak hits are dropped.
- Response style is controlled by the number of **relevant obligations**:
  - **0–8**: list obligations with citations.
  - **9–30**: summarize obligation themes instead of listing each one.
  - **>30**: return an approximate count and ask the user to narrow by topic.

### 4. Add full page context for obligation hits

When the hit is an **obligation**, the system does not stop at the short duty summary. It also **loads the full text of the page** that obligation was tied to (the same page stored during indexing). That way the answering model sees both:

- A **plain-language summary** of the duty, and  
- The **surrounding lease language** on that page,

so answers can align with how the clause is actually written.

If several obligations point to the **same page**, the full page text is **not pasted repeatedly**—it appears once to avoid duplication.

### 5. Build the answer

A **separate** language model receives:

- The **assembled context** (blocks of page text and/or obligation summaries), and  
- The **user’s question**,

with instructions to answer **only from that context**, use normal sentences, and **not** invent clauses that are not there.

---

## Document summary requests

If the user asks to **summarize the whole document**, the router switches to **document summary** intent:

- The system pulls **only the first three pages** of the document.
- The model returns a concise high-level overview based on those pages.

---

## What must exist beforehand

- The PDF must have been **processed** with indexing enabled for the chat path, so both **per-page text** and **per-obligation** entries exist in the vector store.
- If nothing was indexed or nothing matches the question, the user may get a short reply saying there is **not enough information** in the provided material.

---

## How this differs from “search the JSON file”

Some endpoints work by loading **saved JSON** from disk and ranking obligations. The **chat** path described here uses the **vector index** and the **router → filtered search → context → answer** flow above, so retrieval is **semantic** and **scoped** to financial vs general content by design.

---

## Summary

| Piece | Role |
|-------|------|
| **Router** | Labels the question as **financial** or **general** and picks **one** search lane. |
| **Embeddings** | Turn the question (and indexed content) into comparable vectors for similarity search. |
| **Vector store** | Holds **obligations** and **pages** separately; search is **filtered** by the router’s choice. |
| **Parent page fetch** | For obligation hits, pulls **full page text** for grounding. |
| **Answer model** | Writes the final reply using **only** the retrieved context. |

For low-level technical detail (API names, environment variables, exact schemas), see `rag_architecture.md` in the project root.
