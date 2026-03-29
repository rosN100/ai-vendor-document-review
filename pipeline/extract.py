from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Dict, Optional, Type

from openai import OpenAI
from pydantic import BaseModel

from models.schemas import (
    BANKExtraction,
    COIExtraction,
    ClassificationResult,
    DPAExtraction,
    ExtractionPayload,
    IngestedDocument,
    MSAExtraction,
    SOC2Extraction,
    UnknownExtraction,
    W9Extraction,
)


MODEL_MAP: Dict[str, Type[BaseModel]] = {
    "COI": COIExtraction,
    "W9": W9Extraction,
    "MSA": MSAExtraction,
    "SOC2": SOC2Extraction,
    "DPA": DPAExtraction,
    "BANK": BANKExtraction,
    "BANK_DETAILS": BANKExtraction,
}


def _get_client() -> Optional[OpenAI]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def _schema_prompt(doc_type: str, schema_json: str) -> str:
    return (
        f"You are extracting structured compliance data from a {doc_type} document.\n"
        f"Return JSON only matching this exact schema: {schema_json}\n"
        "If a field is not found, return null for the value and 0.0 for confidence."
    )


def _safe_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for candidate in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(value, candidate).date()
        except ValueError:
            continue
    return None


def _float_from_text(text: str) -> Optional[float]:
    digits = re.sub(r"[^0-9.]", "", text or "")
    return float(digits) if digits else None


def _int_from_text(text: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def _heuristic_extract(doc_type: str, document: IngestedDocument) -> ExtractionPayload:
    text = document.raw_text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lower = text.lower()
    if doc_type == "COI":
        payload = COIExtraction(
            insured_entity_name=next((line for line in lines if "insured" in line.lower()), None),
            insurer_name=next((line for line in lines if "insurer" in line.lower()), None),
            coverage_amount_usd=_float_from_text(next((line for line in lines if "coverage" in line.lower()), "")),
            expiry_date=_safe_date(next((line.split(":")[-1] for line in lines if "expiry" in line.lower()), "")),
            additional_insured=next((line.split(":")[-1].strip() for line in lines if "additional insured" in line.lower()), None),
            confidence={
                "insured_entity_name": 0.8,
                "insurer_name": 0.7,
                "coverage_amount_usd": 0.75,
                "expiry_date": 0.8,
                "additional_insured": 0.7,
            },
            spot_check=True,
        )
    elif doc_type == "W9":
        payload = W9Extraction(
            legal_entity_name=lines[0] if lines else None,
            tin=next((re.sub(r"[^0-9-]", "", line) for line in lines if "tin" in line.lower() or "taxpayer" in line.lower()), None),
            entity_type=next((line.split(":")[-1].strip() for line in lines if "entity type" in line.lower()), None),
            signature_date=_safe_date(next((line.split(":")[-1] for line in lines if "signature" in line.lower()), "")),
            confidence={"legal_entity_name": 0.8, "tin": 0.8, "entity_type": 0.7, "signature_date": 0.75},
            spot_check=True,
        )
    elif doc_type == "MSA":
        payload = MSAExtraction(
            party_a=next((line.split(":")[-1].strip() for line in lines if "party a" in line.lower()), None),
            party_b=next((line.split(":")[-1].strip() for line in lines if "party b" in line.lower()), None),
            effective_date=_safe_date(next((line.split(":")[-1] for line in lines if "effective date" in line.lower()), "")),
            term_length=next((line.split(":")[-1].strip() for line in lines if "term length" in line.lower()), None),
            liability_cap_usd=_float_from_text(next((line for line in lines if "liability cap" in line.lower()), "")),
            termination_notice_days=_int_from_text(next((line for line in lines if "termination notice" in line.lower()), "")),
            governing_law=next((line.split(":")[-1].strip() for line in lines if "governing law" in line.lower()), None),
            auto_renewal="auto renew" in lower or "auto-renew" in lower,
            confidence={
                "party_a": 0.7,
                "party_b": 0.8,
                "effective_date": 0.8,
                "term_length": 0.75,
                "liability_cap_usd": 0.75,
                "termination_notice_days": 0.7,
                "governing_law": 0.7,
                "auto_renewal": 0.7,
            },
            spot_check=True,
        )
    elif doc_type == "SOC2":
        payload = SOC2Extraction(
            report_type="Type II" if "type ii" in lower else ("Type I" if "type i" in lower else None),
            audit_period_start=_safe_date(next((line.split(":")[-1] for line in lines if "period start" in line.lower()), "")),
            audit_period_end=_safe_date(next((line.split(":")[-1] for line in lines if "period end" in line.lower()), "")),
            covered_services=[line.split(":")[-1].strip() for line in lines if "service" in line.lower()] or None,
            auditor_name=next((line.split(":")[-1].strip() for line in lines if "auditor" in line.lower()), None),
            confidence={
                "report_type": 0.85,
                "audit_period_start": 0.75,
                "audit_period_end": 0.75,
                "covered_services": 0.7,
                "auditor_name": 0.7,
            },
            spot_check=True,
        )
    elif doc_type == "DPA":
        payload = DPAExtraction(
            data_categories=[line.split(":")[-1].strip() for line in lines if "data categories" in line.lower()] or None,
            retention_period=next((line.split(":")[-1].strip() for line in lines if "retention" in line.lower()), None),
            sub_processors=[line.split(":")[-1].strip() for line in lines if "sub-processor" in line.lower()] or None,
            breach_notification_hours=_int_from_text(next((line for line in lines if "breach notification" in line.lower()), "")),
            confidence={
                "data_categories": 0.7,
                "retention_period": 0.7,
                "sub_processors": 0.7,
                "breach_notification_hours": 0.8,
            },
            spot_check=True,
        )
    elif doc_type in {"BANK", "BANK_DETAILS"}:
        payload = BANKExtraction(
            account_holder_name=next((line.split(":")[-1].strip() for line in lines if "account holder" in line.lower()), None),
            routing_number=next((re.sub(r"[^0-9]", "", line) for line in lines if "routing" in line.lower()), None),
            account_number=next((re.sub(r"[^0-9]", "", line) for line in lines if "account number" in line.lower()), None),
            bank_name=next((line.split(":")[-1].strip() for line in lines if "bank name" in line.lower()), None),
            confidence={
                "account_holder_name": 0.8,
                "routing_number": 0.85,
                "account_number": 0.7,
                "bank_name": 0.8,
            },
            spot_check=True,
        )
    else:
        payload = UnknownExtraction(confidence={}, spot_check=False)

    for field_name, confidence in list(payload.confidence.items()):
        if confidence < 0.60 and hasattr(payload, field_name):
            setattr(payload, field_name, None)
    return payload


def extract_fields(classification: ClassificationResult, document: IngestedDocument) -> ExtractionPayload:
    schema = MODEL_MAP.get(classification.doc_type)
    if schema is None or classification.doc_type == "UNKNOWN":
        return UnknownExtraction(confidence={}, spot_check=False)

    client = _get_client()
    if client is None:
        return _heuristic_extract(classification.doc_type, document)

    model = os.getenv("OPENAI_MODEL_EXTRACT", "gpt-4o")
    schema_json = schema.model_json_schema()
    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": _schema_prompt(classification.doc_type, json.dumps(schema_json))}]},
                {"role": "user", "content": [{"type": "input_text", "text": document.raw_text[:16000]}]},
            ],
            text={"format": {"type": "json_object"}},
        )
        payload = json.loads(response.output_text)
        parsed = schema.model_validate(payload)
        low_confidence = False
        for field_name, confidence in list(parsed.confidence.items()):
            if confidence < 0.60 and hasattr(parsed, field_name):
                setattr(parsed, field_name, None)
            if 0.60 <= confidence < 0.85:
                low_confidence = True
        parsed.spot_check = low_confidence
        return parsed
    except Exception:
        return _heuristic_extract(classification.doc_type, document)
