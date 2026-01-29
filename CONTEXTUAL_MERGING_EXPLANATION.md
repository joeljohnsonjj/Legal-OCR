# Contextual Merging: Explanation for HEB-Legal-OCR System

## What is "Contextual Merging"?

**Contextual merging** is the ability to recognize that when a **new document is uploaded**, obligations in that new document may be **updates/amendments** to existing obligations from previously processed documents, rather than completely new, independent obligations.

---

## The Problem (Current Behavior)

### Scenario: Document Lifecycle

**Timeline:**
1. **Document A** (Original Lease Agreement) - Uploaded on Jan 1, 2025
   - Contains: "Rent Payment: $1,000/month"
   - System extracts: `{DutyType: "Rent Payment", Amount: "$1,000/month", ...}`

2. **Document B** (Lease Amendment) - Uploaded on Feb 1, 2025
   - Contains: "Rent Payment: $1,200/month (amended)"
   - System extracts: `{DutyType: "Rent Payment", Amount: "$1,200/month", ...}`

### Current System Behavior (Without Contextual Merging):

When you query for "Rent payment", the system returns:

```json
{
  "results": [
    {
      "DutyType": "Rent Payment",
      "Owner Responsibility": ["Pay $1,000/month"],
      "Citation": [{"docId": "DocumentA.pdf", "pageNumbers": [3]}]
    },
    {
      "DutyType": "Rent Payment", 
      "Owner Responsibility": ["Pay $1,200/month"],
      "Citation": [{"docId": "DocumentB.pdf", "pageNumbers": [2]}]
    }
  ]
}
```

**Problem**: You now have **TWO separate "Rent Payment" obligations**:
- One from Document A ($1,000)
- One from Document B ($1,200)

This creates **data redundancy** and confusion:
- Which one is current?
- Are both valid?
- Is Document B an update or a separate agreement?

---

## What Contextual Merging Should Do

### With Contextual Merging:

When Document B is processed, the system should:

1. **Recognize** that Document B is an **amendment/update** to Document A
2. **Identify** that the "Rent Payment" in Document B is an **update** to the existing "Rent Payment" from Document A
3. **Merge/Update** the existing obligation instead of creating a duplicate

**Result**:

```json
{
  "results": [
    {
      "DutyType": "Rent Payment",
      "Owner Responsibility": ["Pay $1,200/month"],  // Updated value
      "Citation": [
        {"docId": "DocumentA.pdf", "pageNumbers": [3], "section": ["Original"]},
        {"docId": "DocumentB.pdf", "pageNumbers": [2], "section": ["Amendment"]}
      ],
      "version_history": {
        "original": {"docId": "DocumentA.pdf", "value": "$1,000/month", "date": "2025-01-01"},
        "updated": {"docId": "DocumentB.pdf", "value": "$1,200/month", "date": "2025-02-01"}
      }
    }
  ]
}
```

**Benefits**:
- ✅ **Single source of truth**: One obligation with current value
- ✅ **Version tracking**: Can see what changed and when
- ✅ **No redundancy**: No duplicate obligations
- ✅ **Lifecycle awareness**: System understands document relationships

---

## How This Applies to Your System

### Current Architecture:

Your system has **two separate phases**:

1. **Document Processing Phase** (`process_legal_documents.py`):
   - Each document is processed **independently**
   - Creates a consolidated JSON per document
   - No awareness of previously processed documents
   - No tracking of updates/amendments

2. **Query Phase** (`query_system.py`):
   - Loads all consolidated JSONs
   - Filters by query
   - Merges results, but only if obligations are **identical/similar**
   - Doesn't understand that Document B might be an **amendment** to Document A

### The Gap:

**What's Missing:**
- No **document relationship tracking** (which document amends which)
- No **obligation versioning** (tracking changes over time)
- No **update detection** (recognizing that a new obligation is an update to an old one)
- No **lifecycle management** (understanding document evolution)

---

## Example: Real-World Scenario

### Scenario: Lease Agreement with Multiple Amendments

**Document 1**: "Commercial Lease Agreement - Original.pdf" (Jan 2025)
- Rent: $5,000/month
- Term: 5 years
- Security Deposit: $10,000

**Document 2**: "Commercial Lease Agreement - Amendment 1.pdf" (Mar 2025)
- Rent: $5,500/month (increased)
- Term: 5 years (unchanged)
- Security Deposit: $10,000 (unchanged)

**Document 3**: "Commercial Lease Agreement - Amendment 2.pdf" (Jun 2025)
- Rent: $5,500/month (unchanged)
- Term: 7 years (extended)
- Security Deposit: $12,000 (increased)

### Without Contextual Merging:

Querying "Rent payment" returns:
- Rent from Document 1: $5,000/month
- Rent from Document 2: $5,500/month
- Rent from Document 3: $5,500/month

**Problem**: Which is current? All three appear as separate obligations.

### With Contextual Merging:

Querying "Rent payment" returns:
- **One** Rent obligation: $5,500/month (current)
- Citations show all three documents
- Version history shows: $5,000 → $5,500 (Document 2)

Querying "Term" returns:
- **One** Term obligation: 7 years (current)
- Version history shows: 5 years → 7 years (Document 3)

---

## Implementation Approach for Your System

To implement contextual merging, you would need:

### 1. **Document Relationship Tracking**
- Track which documents are amendments/updates to which
- Store document metadata (upload date, document type, parent document)
- Example: `{document_name: "Amendment 1.pdf", parent_document: "Original.pdf", type: "amendment"}`

### 2. **Obligation Versioning**
- When processing a new document, check existing obligations
- Compare new obligations with existing ones
- If match found (same DutyType, same Responsible Party), treat as update
- Store version history

### 3. **Update Detection Logic**
- When processing Document B:
  - Load existing obligations from Document A
  - For each obligation in Document B:
    - Check if it matches an existing obligation (same type, same party)
    - If match: **UPDATE** existing obligation
    - If no match: **CREATE** new obligation

### 4. **Enhanced Merge Prompt**
- In `merge_and_rank_results()`, include document dates/timestamps
- Instruct Gemini to recognize updates: "If an obligation appears in a newer document with the same DutyType and Responsible Party, it may be an update to an older version"
- Prioritize newer document values when merging

### 5. **Lifecycle Metadata**
- Add fields to obligations:
  - `first_seen_in`: Original document
  - `last_updated_in`: Most recent document
  - `version_history`: Array of changes
  - `is_current`: Boolean (true if this is the latest version)

---

## Current Merge Behavior vs. Contextual Merging

### Current Behavior (in `merge_and_rank_results`):

**What it does:**
- Merges obligations that are **identical** or **highly similar**
- Combines citations from multiple documents
- Example: If "Rent Payment" appears in both Document A and Document B with **same values**, it merges them

**What it doesn't do:**
- Doesn't recognize that Document B might be an **amendment**
- Doesn't prioritize newer document values
- Doesn't track version history
- Doesn't understand document relationships

### Contextual Merging (What's Needed):

**What it should do:**
- When merging, check document dates/timestamps
- If same obligation appears in newer document → treat as **update**
- Use **newer document's values** as current
- Preserve **old values** in version history
- Track **which document updated which obligation**

---

## Example: How to Enhance Your Merge Prompt

### Current Prompt (Line 437-442):
```
1. MERGE DUPLICATE OR HIGHLY SIMILAR OBLIGATIONS:
   - If the same obligation appears in multiple documents (same DutyType, Responsible Party, and similar Owner Responsibility), merge them into a single obligation
```

### Enhanced Prompt (With Contextual Merging):
```
1. MERGE DUPLICATE OR HIGHLY SIMILAR OBLIGATIONS:
   - If the same obligation appears in multiple documents (same DutyType, Responsible Party, and similar Owner Responsibility), merge them into a single obligation
   
2. RECOGNIZE UPDATES AND AMENDMENTS:
   - If an obligation appears in multiple documents with DIFFERENT values (e.g., different amounts, different terms), recognize that the NEWER document likely contains an UPDATE to the older obligation
   - When merging updates:
     * Use the NEWEST document's values as the current obligation
     * Preserve the older document's values in a version_history field
     * Combine citations from all versions
     * Mark the obligation with the most recent document's timestamp
   - Example: If "Rent Payment" is $1,000 in Document A (Jan 2025) and $1,200 in Document B (Feb 2025), create ONE obligation with current value $1,200 and version history showing the change
```

---

## Summary

**Contextual Merging** means your system should:

1. ✅ **Recognize relationships**: Understand that Document B amends Document A
2. ✅ **Detect updates**: Identify when a new obligation is an update to an existing one
3. ✅ **Merge intelligently**: Update existing obligations rather than creating duplicates
4. ✅ **Track lifecycle**: Maintain version history of how obligations evolve
5. ✅ **Prioritize current**: Always show the most recent/current version

**Without contextual merging**, you get:
- ❌ Data redundancy (same obligation appears multiple times)
- ❌ Confusion (which version is current?)
- ❌ No lifecycle awareness (can't see how obligations changed over time)

**With contextual merging**, you get:
- ✅ Single source of truth (one obligation per type)
- ✅ Current values always shown
- ✅ Complete version history
- ✅ Lifecycle management

---

## Next Steps (If You Want to Implement)

1. **Add document metadata** to track relationships (parent documents, amendment types)
2. **Enhance consolidation** to check existing obligations when processing new documents
3. **Update merge prompt** to recognize updates and prioritize newer documents
4. **Add version tracking** fields to obligation structure
5. **Implement update detection** logic in document processing

This would require significant changes to both `process_legal_documents.py` and `query_system.py`.
