from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


DocType = Literal[
    "COI",
    "W9",
    "MSA",
    "SOW",
    "DPA",
    "SOC2",
    "BANK",
    "BANK_DETAILS",
    "FINANCIAL_STATEMENT",
    "BENEFICIAL_OWNERSHIP",
    "UNKNOWN",
]

Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
GateStatus = Literal["PASS", "FAIL"]
SessionStatus = Literal["BLOCKED", "REVIEW_REQUIRED", "REVIEW_RECOMMENDED", "CLEAR"]
QueueName = Literal["STANDARD_ANALYST", "SENIOR_ANALYST", "LEGAL_AND_COMPLIANCE"]


class IngestedDocument(BaseModel):
    doc_id: str
    filename: str
    file_hash: str
    raw_text: str
    page_count: int = 1
    ocr_used: bool = False
    format: str


class IngestError(BaseModel):
    filename: str
    error: Literal["PASSWORD_PROTECTED", "UNSUPPORTED_FORMAT", "INGEST_FAILED"]
    detail: Optional[str] = None


class GateResult(BaseModel):
    gate: GateStatus
    missing: List[str] = Field(default_factory=list)
    message: str = ""


class ClassificationResult(BaseModel):
    doc_id: str
    filename: str
    doc_type: DocType
    confidence: float
    reasoning: str
    needs_human_confirm: bool = False


class ExtractionBase(BaseModel):
    confidence: Dict[str, float] = Field(default_factory=dict)
    spot_check: bool = False


class COIExtraction(ExtractionBase):
    insured_entity_name: Optional[str] = None
    insurer_name: Optional[str] = None
    coverage_type: Optional[List[str]] = None
    coverage_amount_usd: Optional[float] = None
    expiry_date: Optional[date] = None
    additional_insured: Optional[str] = None


class W9Extraction(ExtractionBase):
    legal_entity_name: Optional[str] = None
    tin: Optional[str] = None
    entity_type: Optional[str] = None
    signature_date: Optional[date] = None


class MSAExtraction(ExtractionBase):
    party_a: Optional[str] = None
    party_b: Optional[str] = None
    effective_date: Optional[date] = None
    term_length: Optional[str] = None
    liability_cap_usd: Optional[float] = None
    termination_notice_days: Optional[int] = None
    governing_law: Optional[str] = None
    auto_renewal: Optional[bool] = None


class SOC2Extraction(ExtractionBase):
    report_type: Optional[str] = None
    audit_period_start: Optional[date] = None
    audit_period_end: Optional[date] = None
    covered_services: Optional[List[str]] = None
    auditor_name: Optional[str] = None


class DPAExtraction(ExtractionBase):
    data_categories: Optional[List[str]] = None
    retention_period: Optional[str] = None
    sub_processors: Optional[List[str]] = None
    breach_notification_hours: Optional[int] = None


class BANKExtraction(ExtractionBase):
    account_holder_name: Optional[str] = None
    routing_number: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None


class UnknownExtraction(ExtractionBase):
    pass


ExtractionPayload = Union[
    COIExtraction,
    W9Extraction,
    MSAExtraction,
    SOC2Extraction,
    DPAExtraction,
    BANKExtraction,
    UnknownExtraction,
]


class DocumentReviewResult(BaseModel):
    doc_id: str
    filename: str
    classification: ClassificationResult
    extraction: Optional[ExtractionPayload] = None


class ReasonCode(BaseModel):
    code: str
    severity: Severity
    category: str
    title: str
    detail: str
    evidence: List[str] = Field(default_factory=list)
    action: str
    waiveable: bool = False


class RoutingDecision(BaseModel):
    queue: QueueName
    sla_hours: int
    notify: List[str] = Field(default_factory=list)
    pipeline_frozen: bool = False


class ExternalCheckResult(BaseModel):
    name: str
    status: str
    detail: str
    latency_ms: int


class ValidationResult(BaseModel):
    flags: List[ReasonCode] = Field(default_factory=list)
    external_checks: List[ExternalCheckResult] = Field(default_factory=list)


class AnalystDecision(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "WAIVED", "NEEDS_INFO"]
    analyst_notes: str = ""
    flags_waived: List[str] = Field(default_factory=list)
    analyst_id: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("decided_at", mode="after")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class ReviewSession(BaseModel):
    session_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    vendor_name: str
    vendor_tier: Literal["T1", "T2", "T3", "T4"]
    contact_email: str
    gate_result: GateResult
    documents: List[IngestedDocument] = Field(default_factory=list)
    ingest_errors: List[IngestError] = Field(default_factory=list)
    document_reviews: List[DocumentReviewResult] = Field(default_factory=list)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    routing: Optional[RoutingDecision] = None
    status: SessionStatus = "REVIEW_REQUIRED"
    evidence_pack_markdown: str = ""
    analyst_decision: Optional[AnalystDecision] = None

    @field_validator("created_at", mode="after")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class TierQuestionnaire(BaseModel):
    handles_data: bool = False
    system_access: bool = False
    contract_value_band: Literal["under_50k", "50k_to_500k", "over_500k"] = "under_50k"
    regulated_industry: bool = False
    facilities_only: bool = False
    engagement_type: Literal["one_time", "ongoing"] = "ongoing"


class ReviewRequestMetadata(BaseModel):
    entity_name: str
    dba_name: Optional[str] = None
    entity_type: Optional[str] = None
    tin: Optional[str] = None
    contact_name: str
    contact_email: str
    questionnaire: TierQuestionnaire


class SessionListItem(BaseModel):
    session_id: str
    vendor_name: str
    vendor_tier: str
    status: SessionStatus
    queue: Optional[str] = None
    sla_hours: Optional[int] = None
    flag_count: int = 0
    created_at: datetime
    contact_email: str


class SessionDetailResponse(BaseModel):
    session: ReviewSession
    audit_trail: List[Dict[str, Any]] = Field(default_factory=list)
