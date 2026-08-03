# Legal OCR Integration Plan

## Complete User Flow

#### Step 1: User Enables AI Mode
- **Action:** User toggles "AI Fill" switch in the form interface
- **Prerequisite:** At least one document must be selected from the document list
- **UI Feedback:** Search bar appears at the top of the screen
- **System State:** AI mode activated, ready to receive search queries

#### Step 2: User Enters Keywords
- **Action:** User types keywords in the search bar (e.g., "Landlord HVAC maintenance responsibility")
- **Minimum Length:** System requires 3+ characters before triggering search
- **UI Feedback:** 
  - Search icon visible in input field
  - Placeholder text: "Search contracts for evidence... (e.g., 'maintenance responsibility')"
  - Real-time character count validation

**Example User Queries:**
- "Landlord HVAC Hazardous Materials"
- "Tenant insurance requirements"
- "Maintenance owner responsibility"
- "Structural repairs"

#### Step 3: Query Validation & API Call
- **Trigger:** User types 3+ characters and pauses (or presses Enter)
- **Frontend Action:**
  1. Validates query length (minimum 3 characters)
  2. Shows "Analyzing documents..." animation
  3. Encodes query for URL-safe transmission
  4. Makes HTTP GET request to backend API

**API Request:**
```
GET http://localhost:8000/query?q={encoded_keywords}
```


#### Step 4: Backend ML Processing
- **Backend Actions:**
  1. Receives search query
  2. Searches through processed legal documents using ML models
  3. Identifies relevant legal obligations matching the query
  4. Ranks results by relevance and monetary value
  5. Extracts structured data (responsible party, obligations, reasoning)
  6. Returns JSON response with matched obligations

**Backend Response Structure:**
```json
{
  "query": "Landlord HVAC Hazardous Materials",
  "total_documents_searched": 3,
  "total_obligations_found": 4,
  "results": [
    {
      "DutyType": "Hazardous Materials Remediation",
      "Responsible Party": "Landlord",
      "Owner Responsibility": [
        "Removal of hazardous materials",
        "Environmental compliance",
        "Safety inspections"
      ],
      "Reasoning": [
        "Per Section 3.2 of lease agreement",
        "Commercial lease standards require landlord compliance"
      ],
      "Citation": "Document: Commercial Lease Agreement.pdf | Page 13"
    }
  ],
  "processed_at": "2025-01-20T10:30:00Z"
}
```

#### Step 5: Data Transformation
- **Frontend Processing:**
  1. Receives backend JSON response
  2. Transforms each obligation into snippet format
  3. Extracts document name and page number from citation
  4. Maps backend fields to frontend form fields

**Transformation Mapping:**

// Frontend Output (Snippet)
```json
{
  id: "backend-0",
  title: "Hazardous Materials Remediation",
  documentId: "doc-lease-pdf",
  fieldMappings: {
    responsibleParty: "Landlord",
    maintenanceOwnerResponsibility: "Removal; Compliance; Inspections",
    maintenanceReasoning: "Section 3.2; Commercial standards"
  },
  pdfReference: {
    page: 13,
    fullText: "...",
  },
}
```

#### Step 6: Snippet Rendering
- **UI Components:**
  1. **Snippet Carousel** - Displays one snippet at a time
  2. **Navigation Controls** - Previous/Next buttons, counter (e.g., "1 / 4")


## 📊 Data Flow Diagram

┌─────────────────┐
│  User Types     │
│  Keywords       │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Frontend        │
│ Validates Query │     
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ API Call        │
│ GET /query?q=   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Backend ML      │
│ Processes Query │
│ Searches Docs   │
│ Ranks Results   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ JSON Response   │
│ with Obligations│
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Frontend        │
│ Transforms Data │
│ Maps Fields     │    
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ UI Renders      │
│ Snippet Cards   │
│ with PDF Refs   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ User Selects   │
│ & Accepts      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Form Populated  │
│ with AI Data    │
└─────────────────┘
