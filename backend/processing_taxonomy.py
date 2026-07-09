"""
Extraction / processing taxonomy: allowed high-level lease categories (matches prompt.txt).
Used to normalize category strings after LLM extraction and in consolidation merge.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LABELS_CACHE: Optional[Tuple[str, ...]] = None
_TAXONOMY_DICT_CACHE: Optional[Dict[str, Any]] = None

# Canonical category -> illustrative items (same keys as extraction prompt).
ALLOWED_CATEGORY_ITEMS: Dict[str, List[str]] = {
    "Building Structure & Envelope": [
        "Roof System",
        "Foundation",
        "Structural Components (Columns, Beams)",
        "Exterior Walls / Facade",
        "Windows & Glazing",
        "Doors & Shutters",
        "Weatherproofing & Insulation",
    ],
    "HVAC": [
        "Air Conditioning Systems",
        "Heating Systems",
        "Ventilation Systems",
        "Air Ducts & Filtration",
        "Chillers & Cooling Towers",
        "Thermostats & Control Systems",
        "Indoor Air Quality Systems",
    ],
    "Electrical Systems": [
        "Main Power Supply",
        "Electrical Panels & Wiring",
        "Lighting Systems (Interior & Exterior)",
        "Emergency Lighting",
        "Backup Generators",
        "UPS Systems",
        "Energy Monitoring Systems",
    ],
    "Plumbing": [
        "Internal Plumbing",
        "External Plumbing",
        "Water Supply Systems",
        "Drainage & Sewage Systems",
        "Restroom Fixtures",
        "Water Tanks",
        "Pumps & Valves",
    ],
    "Fire & Life Safety": [
        "Fire Alarm Systems",
        "Sprinkler Systems",
        "Fire Extinguishers",
        "Smoke Detectors",
        "Emergency Exit Systems",
        "Fire Compliance & Inspections",
    ],
    "Vertical Transportation": ["Elevators / Lifts", "Escalators", "Freight Lifts", "Dumbwaiters"],
    "Parking & External Areas": [
        "Parking Lots",
        "Pavement & Roadways",
        "Parking Lighting",
        "Entry / Exit Gates",
        "Landscaping & Green Areas",
        "Sidewalks & Pathways",
        "Loading Docks",
    ],
    "Housekeeping & Waste Management": [
        "General Cleaning",
        "Restroom Cleaning",
        "Waste Collection",
        "Recycling Management",
        "Pest Control",
        "Deep Cleaning Services",
        "Hazardous Waste Handling",
    ],
    "Security Systems": [
        "CCTV Surveillance",
        "Access Control Systems",
        "Security Personnel",
        "Intrusion Alarm Systems",
        "Visitor Management Systems",
        "Loss Prevention Systems",
    ],
    "IT & Communication Infrastructure": [
        "Network Cabling",
        "Server Rooms / Data Closets",
        "Wi-Fi Infrastructure",
        "Telephony Systems",
        "POS Connectivity",
        "Data Security Systems",
    ],
    "Retail-Specific Systems": [
        "Refrigeration Units",
        "Walk-in Freezers & Coolers",
        "Display Shelving & Fixtures",
        "Checkout / POS Systems",
        "Weighing & Labeling Systems",
        "Food Safety & Storage Systems",
    ],
    "Environmental & Sustainability": [
        "Wastewater Treatment Systems",
        "Energy Efficiency Systems",
        "Solar Power Systems",
        "Rainwater Harvesting",
        "Emission Control Systems",
        "Green Building Compliance",
    ],
    "Maintenance & Repairs": [
        "Preventive Maintenance",
        "Corrective Maintenance",
        "Emergency Repairs",
        "Annual Maintenance Contracts (AMC)",
        "Spare Parts Management",
        "Vendor Management",
    ],
    "Utilities": [
        "Electricity Supply",
        "Water Supply",
        "Gas Supply",
        "Internet & Telecom Services",
        "Utility Metering Systems",
    ],
    "Compliance & Regulatory": [
        "Building Code Compliance",
        "Safety Regulations",
        "Environmental Compliance",
        "Health Inspections",
        "Labor Compliance",
        "Insurance Requirements",
    ],
    "Building Interiors": [
        "Interior Walls & Partitions",
        "Flooring Systems",
        "Ceiling Systems",
        "Interior Finishes & Painting",
        "Fixtures & Fittings",
    ],
    "Signage & Branding": ["Exterior Signage", "Interior Signage", "Digital Displays", "Wayfinding Systems"],
    "Access & Mobility Infrastructure": [
        "Ramps & Accessibility Systems",
        "Handrails & Guardrails",
        "Automatic Doors",
        "Accessibility Compliance (ADA or Local Laws)",
    ],
    "Stormwater & Drainage Management": [
        "Stormwater Drainage Systems",
        "Gutters & Downspouts",
        "Flood Prevention Systems",
        "Water Runoff Management",
    ],
    "Fuel & Specialized Utility Systems": [
        "Fuel Storage Systems",
        "Fuel Dispensing Systems",
        "Compressed Air Systems",
        "Specialty Gas Systems",
    ],
    "Cold Chain & Food Processing Infrastructure": [
        "Food Processing Equipment",
        "Cold Storage Logistics Systems",
        "Temperature Monitoring Systems",
        "Sanitation Systems (Food Grade)",
    ],
    "Logistics & Material Handling": [
        "Conveyor Systems",
        "Dock Levelers",
        "Material Handling Equipment",
        "Storage Racking Systems",
    ],
    "Disaster Recovery & Business Continuity": [
        "Emergency Response Systems",
        "Disaster Recovery Plans",
        "Backup Facility Operations",
        "Business Continuity Infrastructure",
    ],
    "Asset Management & Inventory": [
        "Asset Tracking Systems",
        "Inventory Storage Systems",
        "Equipment Lifecycle Management",
        "Tagging & Identification Systems",
    ],
    "Vendor & Contractor Management": [
        "Third-Party Vendor Operations",
        "Service Level Agreements (SLA) Management",
        "Contractor Access Control",
        "Work Permit Systems",
    ],
    "Health & Safety (Non-Fire)": [
        "First Aid Systems",
        "Occupational Safety Equipment",
        "Hazard Communication Systems",
        "Workplace Safety Compliance",
    ],
    "Customer Experience Infrastructure": [
        "Public Address Systems",
        "Queue Management Systems",
        "Customer Seating Areas",
        "Rest Areas & Amenities",
    ],
    "Financial & Cost Allocation Systems": [
        "Base Rent",
        "Holdover Rent",
        "Rent Payment Schedule",
        "Prorated Rent",
        "Real Estate Taxes during lease term",
        "Operating Expenses (CAM)",
        "Estimated OpEx monthly payments",
        "Annual OpEx reconciliation",
        "Rent deficiency payments after default",
        "Rent offset rights",
    ],
    "Insurance & Risk Management": [
        "Property Insurance",
        "General Liability Insurance",
        "Casualty Insurance",
        "Fire Insurance",
        "Flood Insurance",
        "Vandalism Coverage",
        "Insurance Premiums",
        "Risk Coverage",
        "Self-Insurance",
    ],
    "Legal & Indemnification": [
        "Indemnification Clauses",
        "Hold Harmless Agreements",
        "Broker Commission Claims",
        "Attorney Fee Obligations",
        "Dispute Resolution Costs",
        "Warranty Obligations",
        "Guaranty Obligations",
    ],
    "Purchase Option & Closing Costs": [
        "Option to Purchase",
        "Purchase Price",
        "Closing Obligations",
        "Title Insurance",
        "Transfer Taxes",
        "Deed Stamps",
        "Survey Costs",
        "Due Diligence Costs",
        "Closing Agent Charges",
        "Warranty Deed",
        "Lien Releases",
        "Prepayment Penalties",
    ],
    "Leasehold Improvements": [
        "Tenant Improvements (TI)",
        "Fit-Out Works",
        "Alterations & Modifications",
        "Restoration Obligations",
    ],
    "Other": [
        "Miscellaneous Financial Obligations",
        "Unclassified Cost Obligations",
    ],
}


def load_taxonomy_dict() -> Dict[str, Any]:
    global _TAXONOMY_DICT_CACHE
    if _TAXONOMY_DICT_CACHE is not None:
        return _TAXONOMY_DICT_CACHE
    rows: List[Dict[str, Any]] = []
    for cat, items in ALLOWED_CATEGORY_ITEMS.items():
        rows.append({"category": cat, "items": [{"item": i} for i in items]})
    _TAXONOMY_DICT_CACHE = {"results": rows}
    return _TAXONOMY_DICT_CACHE


def taxonomy_json_for_prompt(indent: int = 2) -> str:
    return json.dumps(load_taxonomy_dict(), ensure_ascii=False, indent=indent)


def category_labels_ordered() -> Tuple[str, ...]:
    global _LABELS_CACHE
    if _LABELS_CACHE is not None:
        return _LABELS_CACHE
    labels = list(ALLOWED_CATEGORY_ITEMS.keys())
    _LABELS_CACHE = tuple(labels)
    return _LABELS_CACHE


def normalize_processing_category(value: Any) -> str:
    """Map a string to the closest canonical taxonomy category label."""
    labels = category_labels_ordered()
    if not labels:
        return "Compliance & Regulatory"
    s = str(value or "").strip()
    if not s:
        return "Compliance & Regulatory"
    low = s.lower()
    for lab in labels:
        if lab.lower() == low:
            return lab
    for lab in labels:
        ll = lab.lower()
        if len(ll) >= 4 and (ll in low or low in ll):
            return lab
    return "Compliance & Regulatory"


def merge_extraction_category_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge per-page category blocks: same normalized category -> combined obligations lists
    (party merge happens in merge_duplicate_party_within_category).
    """
    buckets: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for grp in groups or []:
        if not isinstance(grp, dict):
            continue
        cat = normalize_processing_category(grp.get("category"))
        obs = grp.get("obligations")
        if not isinstance(obs, list):
            continue
        for ob in obs:
            if isinstance(ob, dict):
                buckets.setdefault(cat, []).append(dict(ob))
    return [{"category": c, "obligations": v} for c, v in buckets.items() if v]
