from __future__ import annotations

import re
from typing import Dict


BLOCKED_ENTITIES = {"SANCTIONED ENTITY LLC", "BLOCKED CORP"}


def check_ofac(entity_name: str) -> Dict[str, object]:
    normalized = (entity_name or "").strip().upper()
    if normalized in BLOCKED_ENTITIES:
        return {
            "name": "OFAC",
            "status": "HIT",
            "detail": f"Entity matched sanctions list entry: {normalized}",
            "latency_ms": 120,
        }
    return {
        "name": "OFAC",
        "status": "CLEAR",
        "detail": "No sanctions match found",
        "latency_ms": 120,
    }


def check_aba_routing(routing_number: str) -> Dict[str, object]:
    value = (routing_number or "").strip()
    if re.fullmatch(r"\d{9}", value):
        return {
            "name": "ABA_ROUTING",
            "status": "VALID",
            "detail": "Routing number has valid 9-digit format",
            "latency_ms": 120,
        }
    return {
        "name": "ABA_ROUTING",
        "status": "INVALID",
        "detail": "Routing number must be exactly 9 digits",
        "latency_ms": 120,
    }
