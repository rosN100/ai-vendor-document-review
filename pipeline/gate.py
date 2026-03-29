from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List

import yaml

from models.schemas import GateResult, IngestedDocument


FILENAME_HINTS = {
    "COI": ["coi", "certificate", "insurance"],
    "W9": ["w9", "w-9"],
    "MSA": ["msa", "master service"],
    "DPA": ["dpa", "data processing"],
    "SOC2": ["soc2", "soc 2"],
    "BANK": ["bank", "routing", "voided_check"],
}


def load_rules(config_dir: Path) -> Dict:
    with (config_dir / "rules.yaml").open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _guess_doc_types(documents: Iterable[IngestedDocument]) -> set[str]:
    guesses: set[str] = set()
    for document in documents:
        lowered = document.filename.lower()
        for doc_type, hints in FILENAME_HINTS.items():
            if any(hint in lowered for hint in hints):
                guesses.add(doc_type)
    return guesses


def run_completeness_gate(documents: List[IngestedDocument], vendor_tier: str, rules: Dict) -> GateResult:
    required_docs = rules.get("required_docs", {}).get(vendor_tier, [])
    guessed_doc_types = _guess_doc_types(documents)
    missing = [doc_type for doc_type in required_docs if doc_type not in guessed_doc_types]
    if missing:
        return GateResult(
            gate="FAIL",
            missing=missing,
            message=f"Submission incomplete. Please provide: {', '.join(missing)}",
        )
    return GateResult(gate="PASS", message="All required documents detected.")
