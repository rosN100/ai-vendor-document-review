from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from models.schemas import (
    AnalystDecision,
    DocumentReviewResult,
    ReviewSession,
    SessionDetailResponse,
    SessionListItem,
)
from pipeline.classify import classify_document
from pipeline.decide import finalize_session
from pipeline.extract import extract_fields
from pipeline.gate import load_rules, run_completeness_gate
from pipeline.ingest import ingest_files
from pipeline.validate import validate_reviews
from utils.audit import append_audit_log, list_summaries, read_audit_log, read_summary, write_summary


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")
LOG_DIR = BASE_DIR / "logs"
RULES = load_rules(BASE_DIR / "config")

app = FastAPI(title="Vendor Review Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def compute_vendor_tier(
    handles_data: bool,
    system_access: bool,
    contract_value_band: str,
    regulated_industry: bool,
    facilities_only: bool,
    engagement_type: str,
) -> str:
    if handles_data or system_access or contract_value_band == "over_500k" or regulated_industry:
        return "T1"
    if facilities_only and contract_value_band == "under_50k":
        return "T4"
    if engagement_type == "one_time" and contract_value_band == "under_50k":
        return "T3"
    return "T2"


def _session_paths(session_id: str) -> tuple[Path, Path]:
    return LOG_DIR / f"{session_id}.log", LOG_DIR / f"{session_id}.summary.json"


def _persist_session(session: ReviewSession) -> None:
    _, summary_path = _session_paths(session.session_id)
    write_summary(summary_path, session.model_dump(mode="json"))


def _audit(session_id: str, event: str, data: dict) -> None:
    log_path, _ = _session_paths(session_id)
    append_audit_log(log_path, session_id, event, data)


def _load_session(session_id: str) -> ReviewSession:
    _, summary_path = _session_paths(session_id)
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return ReviewSession.model_validate(read_summary(summary_path))


def _sort_ready_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "classify_model": os.getenv("OPENAI_MODEL_CLASSIFY", "gpt-4o-mini"),
        "extract_model": os.getenv("OPENAI_MODEL_EXTRACT", "gpt-4o"),
    }


@app.get("/vendor")
def vendor_page() -> FileResponse:
    return FileResponse(BASE_DIR / "ui" / "vendor.html")


@app.get("/analyst")
def analyst_page() -> FileResponse:
    return FileResponse(BASE_DIR / "ui" / "analyst.html")


@app.get("/sessions", response_model=List[SessionListItem])
def list_sessions() -> List[SessionListItem]:
    items: List[SessionListItem] = []
    for path in list_summaries(LOG_DIR):
        session = ReviewSession.model_validate(read_summary(path))
        items.append(
            SessionListItem(
                session_id=session.session_id,
                vendor_name=session.vendor_name,
                vendor_tier=session.vendor_tier,
                status=session.status,
                queue=session.routing.queue if session.routing else None,
                sla_hours=session.routing.sla_hours if session.routing else None,
                flag_count=len(session.validation.flags),
                created_at=session.created_at,
                contact_email=session.contact_email,
            )
        )
    status_order = {"BLOCKED": 0, "REVIEW_REQUIRED": 1, "REVIEW_RECOMMENDED": 2, "CLEAR": 3}
    return sorted(
        items,
        key=lambda item: (
            status_order.get(item.status, 99),
            item.sla_hours or 999,
            _sort_ready_datetime(item.created_at),
        ),
    )


@app.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str) -> SessionDetailResponse:
    session = _load_session(session_id)
    log_path, _ = _session_paths(session_id)
    return SessionDetailResponse(session=session, audit_trail=read_audit_log(log_path))


@app.get("/sessions/{session_id}/audit")
def get_session_audit(session_id: str) -> dict:
    _load_session(session_id)
    log_path, _ = _session_paths(session_id)
    events = read_audit_log(log_path)
    classify_calls = sum(1 for event in events if event.get("event") == "CLASSIFICATION")
    extract_calls = len(
        {
            event.get("data", {}).get("doc_id")
            for event in events
            if event.get("event") == "FIELD_EXTRACTED" and event.get("data", {}).get("doc_id")
        }
    )
    return {
        "session_id": session_id,
        "events": events,
        "total_events": len(events),
        "llm_calls": classify_calls + extract_calls,
    }


@app.post("/sessions/{session_id}/decision", response_model=ReviewSession)
def record_decision(session_id: str, decision: AnalystDecision) -> ReviewSession:
    session = _load_session(session_id)
    session.analyst_decision = decision
    _persist_session(session)
    _audit(session_id, "ANALYST_DECISION", decision.model_dump(mode="json"))
    return session


@app.post("/review", response_model=ReviewSession)
async def review_documents(
    entity_name: str = Form(...),
    dba_name: str = Form(""),
    entity_type: str = Form(""),
    tin: str = Form(""),
    contact_name: str = Form(...),
    contact_email: str = Form(...),
    handles_data: bool = Form(False),
    system_access: bool = Form(False),
    contract_value_band: str = Form(...),
    regulated_industry: bool = Form(False),
    facilities_only: bool = Form(False),
    engagement_type: str = Form(...),
    files: List[UploadFile] = File(...),
) -> ReviewSession:
    session_id = f"SES-{uuid.uuid4().hex[:8].upper()}"
    vendor_tier = compute_vendor_tier(
        handles_data,
        system_access,
        contract_value_band,
        regulated_industry,
        facilities_only,
        engagement_type,
    )
    _audit(
        session_id,
        "REVIEW_SESSION_STARTED",
        {
            "vendor_name": entity_name,
            "vendor_tier": vendor_tier,
            "contact_email": contact_email,
            "pipeline_version": "1.0",
        },
    )

    documents, ingest_errors = ingest_files(files)
    for document in documents:
        _audit(
            session_id,
            "DOCUMENT_RECEIVED",
            {
                "doc_id": document.doc_id,
                "filename": document.filename,
                "file_hash": document.file_hash,
                "format": document.format,
                "ocr_used": document.ocr_used,
            },
        )
    for error in ingest_errors:
        _audit(session_id, "DOCUMENT_REJECTED", error.model_dump(mode="json"))

    gate_result = run_completeness_gate(documents, vendor_tier, RULES)
    _audit(session_id, "COMPLETENESS_GATE", gate_result.model_dump(mode="json"))

    session = ReviewSession(
        session_id=session_id,
        vendor_name=entity_name,
        vendor_tier=vendor_tier,  # type: ignore[arg-type]
        contact_email=contact_email,
        gate_result=gate_result,
        documents=documents,
        ingest_errors=ingest_errors,
    )

    if gate_result.gate == "FAIL":
        session.status = "BLOCKED"
        session.evidence_pack_markdown = "Submission blocked at completeness gate."
        _persist_session(session)
        return session

    reviews: List[DocumentReviewResult] = []
    for document in documents:
        classification = classify_document(document)
        _audit(
            session_id,
            "CLASSIFICATION",
            {
                "doc_id": document.doc_id,
                "filename": document.filename,
                "classified_as": classification.doc_type,
                "confidence": classification.confidence,
                "model": os.getenv("OPENAI_MODEL_CLASSIFY", "gpt-4o-mini"),
                "prompt_version": os.getenv("PROMPT_VERSION_CLASSIFY", "1.0"),
                "reasoning": classification.reasoning,
            },
        )
        extraction = None
        if classification.doc_type != "UNKNOWN":
            extraction = extract_fields(classification, document)
            for field_name, confidence in getattr(extraction, "confidence", {}).items():
                _audit(
                    session_id,
                    "FIELD_EXTRACTED",
                    {
                        "doc_id": document.doc_id,
                        "filename": document.filename,
                        "doc_type": classification.doc_type,
                        "field_name": field_name,
                        "value": getattr(extraction, field_name, None),
                        "confidence": confidence,
                        "spot_check": getattr(extraction, "spot_check", False),
                        "source_page": 1,
                        "model": os.getenv("OPENAI_MODEL_EXTRACT", "gpt-4o"),
                        "prompt_version": os.getenv("PROMPT_VERSION_EXTRACT", "1.0"),
                    },
                )
        reviews.append(
            DocumentReviewResult(
                doc_id=document.doc_id,
                filename=document.filename,
                classification=classification,
                extraction=extraction,
            )
        )

    validation = validate_reviews(reviews, vendor_tier, RULES)
    for flag in validation.flags:
        _audit(
            session_id,
            "FLAG_RAISED",
            {
                **flag.model_dump(mode="json"),
                "rule_version": f"{flag.code} v1.0",
                "delta": flag.detail,
            },
        )
    for check in validation.external_checks:
        _audit(session_id, "EXTERNAL_CHECK_COMPLETED", check.model_dump(mode="json"))

    session.document_reviews = reviews
    session.validation = validation
    session = finalize_session(session)
    _audit(
        session_id,
        "ROUTING_ASSIGNED",
        {
            **(session.routing.model_dump(mode="json") if session.routing else {}),
            "status": session.status,
        },
    )
    _persist_session(session)
    return session
