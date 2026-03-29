from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

from rapidfuzz import fuzz

from models.schemas import (
    DocumentReviewResult,
    ExternalCheckResult,
    ReasonCode,
    ValidationResult,
)
from utils.external import check_aba_routing, check_ofac

FILENAME_HINTS = {
    "COI": ["coi", "certificate", "insurance"],
    "W9": ["w9", "w-9"],
    "MSA": ["msa", "master service"],
    "DPA": ["dpa", "data processing"],
    "SOC2": ["soc2", "soc 2"],
    "BANK": ["bank", "routing", "voided_check"],
}


def _severity_value(severity: str) -> int:
    return {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}.get(severity, 0)


def _build_reason(code: str, severity: str, title: str, detail: str, evidence: List[str], waiveable: bool, category: str) -> ReasonCode:
    return ReasonCode(
        code=code,
        severity=severity,  # type: ignore[arg-type]
        category=category,
        title=title,
        detail=detail,
        evidence=evidence,
        action="Analyst review required" if severity in {"HIGH", "CRITICAL"} else "Spot check recommended",
        waiveable=waiveable,
    )


def _find_review(reviews: List[DocumentReviewResult], doc_type: str) -> Optional[DocumentReviewResult]:
    for review in reviews:
        if review.classification.doc_type == doc_type:
            return review
    return None


def _infer_expected_doc_type(filename: str) -> Optional[str]:
    lowered = filename.lower()
    for doc_type, hints in FILENAME_HINTS.items():
        if any(hint in lowered for hint in hints):
            return doc_type
    return None


def _evaluate_classification_flags(reviews: List[DocumentReviewResult], vendor_tier: str, rules: Dict) -> List[ReasonCode]:
    required_docs = set(rules.get("required_docs", {}).get(vendor_tier, []))
    flags: List[ReasonCode] = []
    unknown_severity = {"T1": "CRITICAL", "T2": "HIGH", "T3": "HIGH", "T4": "MEDIUM"}.get(vendor_tier, "HIGH")
    low_conf_severity = {"T1": "HIGH", "T2": "HIGH", "T3": "MEDIUM", "T4": "MEDIUM"}.get(vendor_tier, "MEDIUM")

    for review in reviews:
        expected_doc_type = _infer_expected_doc_type(review.filename)
        if expected_doc_type not in required_docs:
            continue

        if review.classification.doc_type == "UNKNOWN":
            flags.append(
                _build_reason(
                    "CLASSIFICATION_UNKNOWN_001",
                    unknown_severity,
                    "Required document could not be confidently classified",
                    f"{review.filename} appears to be a required {expected_doc_type} document but classified as UNKNOWN "
                    f"(confidence={review.classification.confidence:.2f}).",
                    [
                        f"filename={review.filename}",
                        f"expected_type={expected_doc_type}",
                        f"classified_as={review.classification.doc_type}",
                    ],
                    False,
                    "CLASSIFICATION",
                )
            )
            continue

        if review.classification.needs_human_confirm:
            flags.append(
                _build_reason(
                    "CLASSIFICATION_LOW_CONFIDENCE_001",
                    low_conf_severity,
                    "Required document classification needs human confirmation",
                    f"{review.filename} classified as {review.classification.doc_type} with low confidence "
                    f"({review.classification.confidence:.2f}).",
                    [
                        f"filename={review.filename}",
                        f"expected_type={expected_doc_type}",
                        f"classified_as={review.classification.doc_type}",
                    ],
                    True,
                    "CLASSIFICATION",
                )
            )

    return flags


def _evaluate_extraction_confidence_flags(reviews: List[DocumentReviewResult], vendor_tier: str, rules: Dict) -> List[ReasonCode]:
    required_docs = set(rules.get("required_docs", {}).get(vendor_tier, []))
    flags: List[ReasonCode] = []
    severity = {"T1": "HIGH", "T2": "HIGH", "T3": "MEDIUM", "T4": "MEDIUM"}.get(vendor_tier, "MEDIUM")

    for review in reviews:
        expected_doc_type = _infer_expected_doc_type(review.filename)
        if expected_doc_type not in required_docs:
            continue
        if not review.extraction:
            continue

        confidence_map = getattr(review.extraction, "confidence", {}) or {}
        low_conf_fields = sorted(
            [
                field_name
                for field_name, score in confidence_map.items()
                if float(score) < 0.85
            ]
        )
        if not low_conf_fields:
            continue

        flags.append(
            _build_reason(
                "EXTRACTION_SPOT_CHECK_001",
                severity,
                "Required document contains low-confidence extracted fields",
                f"{review.filename} has extracted fields below confidence threshold: {', '.join(low_conf_fields)}.",
                [
                    f"{review.filename}: {field_name}={confidence_map.get(field_name)}"
                    for field_name in low_conf_fields
                ],
                True,
                "EXTRACTION",
            )
        )

    return flags


def _evaluate_field_rules(reviews: List[DocumentReviewResult], vendor_tier: str, rules: Dict) -> List[ReasonCode]:
    today = date.today()
    flags: List[ReasonCode] = []
    for code, rule in rules.get("rules", {}).items():
        severity = rule["severity"].get(vendor_tier, "SKIP")
        if severity == "SKIP":
            continue
        review = _find_review(reviews, rule["doc_type"])
        if not review or not review.extraction:
            continue
        field_name = rule["field"]
        value = getattr(review.extraction, field_name, None)
        if value is None:
            flags.append(
                _build_reason(
                    code,
                    severity,
                    rule["description"],
                    f"Required field `{field_name}` was missing or low confidence.",
                    [f"{review.filename}: {field_name}=null"],
                    bool(rule.get("waiveable", False)),
                    "FIELD_RULE",
                )
            )
            continue
        passed = True
        if code == "COI_EXPIRY_001":
            passed = value > today + timedelta(days=30)
        elif code == "COI_COVERAGE_001":
            passed = float(value) >= 1000000
        elif code == "SOC2_AGE_001":
            passed = value > today - timedelta(days=365)
        elif code == "W9_SIGNATURE_001":
            passed = value > today - timedelta(days=1095)
        elif code == "DPA_BREACH_001":
            passed = int(value) <= 72
        if not passed:
            flags.append(
                _build_reason(
                    code,
                    severity,
                    rule["description"],
                    f"Validation failed for `{field_name}`.",
                    [f"{review.filename}: {field_name}={value}"],
                    bool(rule.get("waiveable", False)),
                    "FIELD_RULE",
                )
            )
    return flags


def _evaluate_cross_doc_checks(reviews: List[DocumentReviewResult], vendor_tier: str, rules: Dict) -> List[ReasonCode]:
    threshold = int(rules.get("fuzzy_match_threshold", 90))
    flags: List[ReasonCode] = []
    w9 = _find_review(reviews, "W9")
    msa = _find_review(reviews, "MSA")
    coi = _find_review(reviews, "COI")
    bank = _find_review(reviews, "BANK") or _find_review(reviews, "BANK_DETAILS")

    for code, rule in rules.get("cross_doc_checks", {}).items():
        severity = rule["severity"].get(vendor_tier, "LOW")
        if code == "ENTITY_NAME_MATCH" and all([w9, msa, coi]):
            values = [
                getattr(w9.extraction, "legal_entity_name", ""),
                getattr(msa.extraction, "party_b", ""),
                getattr(coi.extraction, "insured_entity_name", ""),
            ]
            normalized = [value for value in values if value]
            if len(normalized) >= 2:
                score = min(fuzz.ratio(normalized[0], value) for value in normalized[1:])
                if score < threshold:
                    flags.append(
                        _build_reason(
                            code,
                            severity,
                            rule["description"],
                            f"Entity names are inconsistent across documents (score={score}).",
                            normalized,
                            True,
                            "CROSS_DOC",
                        )
                    )
        elif code == "COI_COVERS_MSA_TERM" and all([coi, msa]):
            expiry = getattr(coi.extraction, "expiry_date", None)
            effective = getattr(msa.extraction, "effective_date", None)
            if expiry and effective and expiry <= effective:
                flags.append(
                    _build_reason(
                        code,
                        severity,
                        rule["description"],
                        "COI expiry date does not extend beyond the MSA effective date.",
                        [f"COI expiry={expiry}", f"MSA effective={effective}"],
                        False,
                        "CROSS_DOC",
                    )
                )
        elif code == "BANK_HOLDER_MATCH" and all([bank, w9]):
            bank_holder = getattr(bank.extraction, "account_holder_name", "")
            entity_name = getattr(w9.extraction, "legal_entity_name", "")
            if bank_holder and entity_name and fuzz.ratio(bank_holder, entity_name) < threshold:
                flags.append(
                    _build_reason(
                        code,
                        severity,
                        rule["description"],
                        "Bank account holder differs from the W-9 entity.",
                        [bank_holder, entity_name],
                        True,
                        "CROSS_DOC",
                    )
                )
    return flags


def _evaluate_external_checks(reviews: List[DocumentReviewResult]) -> tuple[List[ReasonCode], List[ExternalCheckResult]]:
    flags: List[ReasonCode] = []
    checks: List[ExternalCheckResult] = []

    entity_name = None
    routing_number = None
    for doc_type in ("W9", "COI", "MSA", "BANK", "BANK_DETAILS"):
        review = _find_review(reviews, doc_type)
        if not review or not review.extraction:
            continue
        entity_name = entity_name or getattr(review.extraction, "legal_entity_name", None) or getattr(review.extraction, "insured_entity_name", None) or getattr(review.extraction, "party_b", None) or getattr(review.extraction, "account_holder_name", None)
        routing_number = routing_number or getattr(review.extraction, "routing_number", None)

    if entity_name:
        ofac = ExternalCheckResult.model_validate(check_ofac(entity_name))
        checks.append(ofac)
        if ofac.status == "HIT":
            flags.append(
                _build_reason(
                    "OFAC_HIT_001",
                    "CRITICAL",
                    "OFAC sanctions match",
                    ofac.detail,
                    [entity_name],
                    False,
                    "EXTERNAL",
                )
            )

    if routing_number:
        aba = ExternalCheckResult.model_validate(check_aba_routing(routing_number))
        checks.append(aba)
        if aba.status != "VALID":
            flags.append(
                _build_reason(
                    "ABA_INVALID_001",
                    "HIGH",
                    "Invalid ABA routing number",
                    aba.detail,
                    [routing_number],
                    True,
                    "EXTERNAL",
                )
            )
    return flags, checks


def validate_reviews(reviews: List[DocumentReviewResult], vendor_tier: str, rules: Dict) -> ValidationResult:
    flags: List[ReasonCode] = []
    flags.extend(_evaluate_classification_flags(reviews, vendor_tier, rules))
    flags.extend(_evaluate_extraction_confidence_flags(reviews, vendor_tier, rules))
    flags.extend(_evaluate_field_rules(reviews, vendor_tier, rules))
    flags.extend(_evaluate_cross_doc_checks(reviews, vendor_tier, rules))
    external_flags, checks = _evaluate_external_checks(reviews)
    flags.extend(external_flags)
    return ValidationResult(
        flags=sorted(flags, key=lambda item: _severity_value(item.severity), reverse=True),
        external_checks=checks,
    )
