from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytesseract
from fastapi.testclient import TestClient

from main import app
from models.schemas import ClassificationResult, IngestedDocument, ReviewSession
from pipeline.classify import classify_document
from pipeline.decide import finalize_session
from pipeline.extract import extract_fields
from pipeline.gate import load_rules, run_completeness_gate
from pipeline.ingest import ingest_document, ingest_files
from pipeline.validate import validate_reviews


BASE_DIR = Path(__file__).resolve().parents[1]
RULES = load_rules(BASE_DIR / "config")


def test_ingest_txt_email():
    result = ingest_document("submission.txt", b"hello vendor", "DOC-001")
    assert result.format == "EMAIL_TEXT"
    assert result.raw_text == "hello vendor"


def test_ingest_files_handles_missing_tesseract(monkeypatch):
    class UploadStub:
        filename = "scan.png"

        def __init__(self):
            self.file = BytesIO(b"fake-image-bytes")

    def fake_open(*args, **kwargs):
        class DummyImage:
            pass

        return DummyImage()

    def fake_ocr(*args, **kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr("pipeline.ingest.Image.open", fake_open)
    monkeypatch.setattr("pipeline.ingest.pytesseract.image_to_string", fake_ocr)

    documents, errors = ingest_files([UploadStub()])
    assert documents == []
    assert len(errors) == 1
    assert errors[0].error == "INGEST_FAILED"
    assert "tesseract" in errors[0].detail.lower()


def test_gate_detects_missing_docs():
    docs = [IngestedDocument(doc_id="DOC-001", filename="vendor_coi.pdf", file_hash="sha256:x", raw_text="x", page_count=1, ocr_used=False, format="PDF_TEXT")]
    gate = run_completeness_gate(docs, "T2", RULES)
    assert gate.gate == "FAIL"
    assert "MSA" in gate.missing


def test_classify_heuristic_w9():
    document = IngestedDocument(
        doc_id="DOC-001",
        filename="vendor_w9.txt",
        file_hash="sha256:x",
        raw_text="W-9 Taxpayer Identification Number Legal Entity Name: Acme LLC",
        page_count=1,
        ocr_used=False,
        format="EMAIL_TEXT",
    )
    result = classify_document(document)
    assert result.doc_type == "W9"
    assert result.confidence >= 0.85


def test_extract_heuristic_bank():
    document = IngestedDocument(
        doc_id="DOC-001",
        filename="bank.txt",
        file_hash="sha256:x",
        raw_text="Account Holder: Acme LLC\nRouting Number: 123456789\nAccount Number: 111222333\nBank Name: First Bank",
        page_count=1,
        ocr_used=False,
        format="EMAIL_TEXT",
    )
    classification = ClassificationResult(
        doc_id="DOC-001",
        filename="bank.txt",
        doc_type="BANK",
        confidence=0.99,
        reasoning="test",
        needs_human_confirm=False,
    )
    extraction = extract_fields(classification, document)
    assert extraction.routing_number == "123456789"


def test_validate_flags_cross_doc_mismatch():
    coi_doc = IngestedDocument(doc_id="DOC-001", filename="coi.txt", file_hash="sha256:x", raw_text="", page_count=1, ocr_used=False, format="EMAIL_TEXT")
    w9_doc = IngestedDocument(doc_id="DOC-002", filename="w9.txt", file_hash="sha256:x", raw_text="", page_count=1, ocr_used=False, format="EMAIL_TEXT")
    reviews = [
        {
            "doc_id": "DOC-001",
            "filename": "coi.txt",
            "classification": {"doc_id": "DOC-001", "filename": "coi.txt", "doc_type": "COI", "confidence": 0.99, "reasoning": "x", "needs_human_confirm": False},
            "extraction": {"insured_entity_name": "Alpha LLC", "expiry_date": "2027-01-01", "coverage_amount_usd": 2000000, "confidence": {"insured_entity_name": 0.9, "expiry_date": 0.9, "coverage_amount_usd": 0.9}, "spot_check": False},
        },
        {
            "doc_id": "DOC-002",
            "filename": "w9.txt",
            "classification": {"doc_id": "DOC-002", "filename": "w9.txt", "doc_type": "W9", "confidence": 0.99, "reasoning": "x", "needs_human_confirm": False},
            "extraction": {"legal_entity_name": "Beta LLC", "signature_date": "2026-01-01", "confidence": {"legal_entity_name": 0.9, "signature_date": 0.9}, "spot_check": False},
        },
        {
            "doc_id": "DOC-003",
            "filename": "msa.txt",
            "classification": {"doc_id": "DOC-003", "filename": "msa.txt", "doc_type": "MSA", "confidence": 0.99, "reasoning": "x", "needs_human_confirm": False},
            "extraction": {"party_b": "Gamma LLC", "effective_date": "2026-01-01", "confidence": {"party_b": 0.9, "effective_date": 0.9}, "spot_check": False},
        },
    ]
    from models.schemas import DocumentReviewResult

    parsed_reviews = [DocumentReviewResult.model_validate(review) for review in reviews]
    validation = validate_reviews(parsed_reviews, "T2", RULES)
    assert any(flag.code == "ENTITY_NAME_MATCH" for flag in validation.flags)


def test_validate_unknown_required_doc_raises_flag():
    from models.schemas import DocumentReviewResult

    reviews = [
        DocumentReviewResult.model_validate(
            {
                "doc_id": "DOC-001",
                "filename": "sample_coi.pdf",
                "classification": {
                    "doc_id": "DOC-001",
                    "filename": "sample_coi.pdf",
                    "doc_type": "UNKNOWN",
                    "confidence": 0.5,
                    "reasoning": "uncertain",
                    "needs_human_confirm": True,
                },
                "extraction": None,
            }
        )
    ]
    validation = validate_reviews(reviews, "T3", RULES)
    assert any(flag.code == "CLASSIFICATION_UNKNOWN_001" for flag in validation.flags)
    assert any(flag.severity == "HIGH" for flag in validation.flags)


def test_unknown_required_doc_prevents_clear_status():
    from models.schemas import DocumentReviewResult, GateResult, ReviewSession, ValidationResult

    reviews = [
        DocumentReviewResult.model_validate(
            {
                "doc_id": "DOC-001",
                "filename": "sample_coi.pdf",
                "classification": {
                    "doc_id": "DOC-001",
                    "filename": "sample_coi.pdf",
                    "doc_type": "UNKNOWN",
                    "confidence": 0.5,
                    "reasoning": "uncertain",
                    "needs_human_confirm": True,
                },
                "extraction": None,
            }
        )
    ]
    validation = validate_reviews(reviews, "T3", RULES)
    session = ReviewSession(
        session_id="SES-TEST",
        vendor_name="Brightline Marketing Group Inc",
        vendor_tier="T3",
        contact_email="test@example.com",
        gate_result=GateResult(gate="PASS", missing=[], message=""),
        document_reviews=reviews,
        validation=validation,
    )
    finalized = finalize_session(session)
    assert finalized.status == "REVIEW_REQUIRED"


def test_low_confidence_required_extraction_raises_flag():
    from models.schemas import DocumentReviewResult

    reviews = [
        DocumentReviewResult.model_validate(
            {
                "doc_id": "DOC-001",
                "filename": "acme_coi.txt",
                "classification": {
                    "doc_id": "DOC-001",
                    "filename": "acme_coi.txt",
                    "doc_type": "COI",
                    "confidence": 0.9,
                    "reasoning": "matched COI markers",
                    "needs_human_confirm": False,
                },
                "extraction": {
                    "insured_entity_name": "Insured Entity: Acme LLC",
                    "insurer_name": None,
                    "coverage_amount_usd": 1000000,
                    "expiry_date": "2027-01-01",
                    "confidence": {
                        "insured_entity_name": 0.8,
                        "insurer_name": 0.7,
                        "coverage_amount_usd": 0.95,
                        "expiry_date": 0.95,
                    },
                    "spot_check": True,
                },
            }
        )
    ]
    validation = validate_reviews(reviews, "T4", RULES)
    assert any(flag.code == "EXTRACTION_SPOT_CHECK_001" for flag in validation.flags)


def test_low_confidence_required_extraction_prevents_clear_status():
    from models.schemas import DocumentReviewResult, GateResult, ReviewSession

    reviews = [
        DocumentReviewResult.model_validate(
            {
                "doc_id": "DOC-001",
                "filename": "acme_coi.txt",
                "classification": {
                    "doc_id": "DOC-001",
                    "filename": "acme_coi.txt",
                    "doc_type": "COI",
                    "confidence": 0.9,
                    "reasoning": "matched COI markers",
                    "needs_human_confirm": False,
                },
                "extraction": {
                    "insured_entity_name": "Insured Entity: Acme LLC",
                    "insurer_name": None,
                    "coverage_amount_usd": 1000000,
                    "expiry_date": "2027-01-01",
                    "confidence": {
                        "insured_entity_name": 0.8,
                        "insurer_name": 0.7,
                        "coverage_amount_usd": 0.95,
                        "expiry_date": 0.95,
                    },
                    "spot_check": True,
                },
            }
        )
    ]
    validation = validate_reviews(reviews, "T4", RULES)
    session = ReviewSession(
        session_id="SES-T4",
        vendor_name="Acme LLC",
        vendor_tier="T4",
        contact_email="test@example.com",
        gate_result=GateResult(gate="PASS", missing=[], message=""),
        document_reviews=reviews,
        validation=validation,
    )
    finalized = finalize_session(session)
    assert finalized.status == "REVIEW_RECOMMENDED"


def test_review_endpoint_end_to_end():
    client = TestClient(app)
    response = client.post(
        "/review",
        data={
            "entity_name": "Acme LLC",
            "dba_name": "",
            "entity_type": "LLC",
            "tin": "12-3456789",
            "contact_name": "Jane Doe",
            "contact_email": "jane@example.com",
            "handles_data": "false",
            "system_access": "false",
            "contract_value_band": "under_50k",
            "regulated_industry": "false",
            "facilities_only": "true",
            "engagement_type": "one_time",
        },
        files=[("files", ("acme_coi.txt", b"Certificate of Insurance\nInsured Entity: Acme LLC\nCoverage Amount: 1000000\nExpiry Date: 2027-01-01", "text/plain"))],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["vendor_tier"] == "T4"
    assert payload["gate_result"]["gate"] == "PASS"
    audit = client.get(f"/sessions/{payload['session_id']}/audit")
    assert audit.status_code == 200
    audit_payload = audit.json()
    assert audit_payload["session_id"] == payload["session_id"]
    assert audit_payload["total_events"] >= 1


def test_session_model_normalizes_naive_datetime():
    session = ReviewSession.model_validate(
        {
            "session_id": "SES-TEST",
            "created_at": "2026-03-29T12:00:00",
            "vendor_name": "Acme LLC",
            "vendor_tier": "T2",
            "contact_email": "test@example.com",
            "gate_result": {"gate": "PASS", "missing": [], "message": ""},
        }
    )
    assert session.created_at.tzinfo is not None
