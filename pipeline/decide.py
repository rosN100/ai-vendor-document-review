from __future__ import annotations

from datetime import date
from typing import List

from models.schemas import ReasonCode, ReviewSession, RoutingDecision


VALID_TIERS = {"T1", "T2", "T3", "T4"}


def determine_routing(flags: List[ReasonCode]) -> RoutingDecision:
    if any(flag.code == "OFAC_HIT_001" for flag in flags):
        return RoutingDecision(
            queue="LEGAL_AND_COMPLIANCE",
            sla_hours=1,
            notify=["legal@internal", "compliance@internal"],
            pipeline_frozen=True,
        )
    if any(flag.severity == "CRITICAL" for flag in flags):
        return RoutingDecision(
            queue="SENIOR_ANALYST",
            sla_hours=4,
            notify=["senior-analyst@internal"],
        )
    return RoutingDecision(
        queue="STANDARD_ANALYST",
        sla_hours=24,
        notify=["analyst-queue@internal"],
    )


def compute_status(vendor_tier: str, flags: List[ReasonCode]) -> str:
    if any(flag.code == "OFAC_HIT_001" for flag in flags):
        return "BLOCKED"
    if any(flag.severity in {"CRITICAL", "HIGH"} for flag in flags):
        return "REVIEW_REQUIRED"
    if flags:
        return "REVIEW_RECOMMENDED"
    if vendor_tier in {"T3", "T4"}:
        return "CLEAR"
    return "CLEAR"


def build_evidence_pack(session: ReviewSession) -> str:
    lines = [
        f"# Evidence Pack: {session.vendor_name}",
        "",
        f"- Session ID: {session.session_id}",
        f"- Vendor Tier: {session.vendor_tier}",
        f"- Review Date: {date.today().isoformat()}",
        f"- Gate Result: {session.gate_result.gate}",
        f"- Status: {session.status}",
        "",
        "## Documents",
    ]
    for document in session.documents:
        lines.append(
            f"- {document.filename} ({document.format}, pages={document.page_count}, ocr_used={document.ocr_used})"
        )
    lines.extend(["", "## Flags"])
    if not session.validation.flags:
        lines.append("- None")
    else:
        for flag in session.validation.flags:
            lines.append(f"- [{flag.severity}] {flag.code}: {flag.title}")
            lines.append(f"  Detail: {flag.detail}")
    lines.extend(["", "## External Checks"])
    if not session.validation.external_checks:
        lines.append("- None")
    else:
        for check in session.validation.external_checks:
            lines.append(f"- {check.name}: {check.status} ({check.detail})")
    return "\n".join(lines)


def finalize_session(session: ReviewSession) -> ReviewSession:
    if session.vendor_tier not in VALID_TIERS:
        raise ValueError("Invalid vendor tier supplied")
    session.routing = determine_routing(session.validation.flags)
    session.status = compute_status(session.vendor_tier, session.validation.flags)  # type: ignore[assignment]
    session.evidence_pack_markdown = build_evidence_pack(session)
    return session
