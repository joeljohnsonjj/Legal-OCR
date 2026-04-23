"""
Session working-state schema (Phase 3). Strict Pydantic models; unknown fields rejected.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class InvestigationState(BaseModel):
    """Current investigation scope: incidents, services, time, environment."""

    model_config = ConfigDict(extra="forbid")

    incident_ids: List[str] = []
    primary_service: Optional[str] = None
    related_services: List[str] = []
    time_window: Optional[str] = None
    environment: Optional[str] = None


class Hypothesis(BaseModel):
    """Single hypothesis with status and optional evidence refs."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = ""
    description: str = ""
    status: Literal["active", "rejected", "confirmed"] = "active"
    confidence_hint: Optional[str] = None
    supporting_evidence_refs: List[str] = []


class Constraints(BaseModel):
    """Exclusions and required signals for the investigation."""

    model_config = ConfigDict(extra="forbid")

    excluded_services: List[str] = []
    excluded_time_ranges: List[str] = []
    required_signals: List[str] = []


class ProgressMarkers(BaseModel):
    """Completed steps, open questions, next actions."""

    model_config = ConfigDict(extra="forbid")

    completed_steps: List[str] = []
    open_questions: List[str] = []
    next_actions: List[str] = []


class CompactionState(BaseModel):
    """State used by compaction: last run time, summary ref, turn count, needs_compaction flag."""

    model_config = ConfigDict(extra="forbid")

    last_compaction_at: Optional[str] = None
    needs_compaction: bool = False
    summary_ref: Optional[str] = None
    turns_since_last_compaction: int = 0


class SessionState(BaseModel):
    """Root working state: investigation, hypotheses, constraints, progress, compaction."""

    model_config = ConfigDict(extra="forbid")

    investigation: Optional[InvestigationState] = None
    hypotheses: List[Hypothesis] = []
    constraints: Optional[Constraints] = None
    progress: Optional[ProgressMarkers] = None
    compaction: Optional[CompactionState] = None
