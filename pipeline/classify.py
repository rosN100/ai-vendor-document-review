from __future__ import annotations

import json
import os
import re
from typing import Optional

from openai import OpenAI

from models.schemas import ClassificationResult, IngestedDocument


CLASSIFICATION_PROMPT = """You are a compliance document classifier. Classify the document into exactly one of these types:
COI, W9, MSA, SOW, DPA, SOC2, BANK_DETAILS, FINANCIAL_STATEMENT, BENEFICIAL_OWNERSHIP, UNKNOWN

Return JSON only. No explanation. Format:
{"doc_type": "<TYPE>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}"""


def _truncate_text(raw_text: str, char_limit: int = 12000) -> str:
    return raw_text[:char_limit]


def _heuristic_classify(document: IngestedDocument) -> ClassificationResult:
    text = f"{document.filename}\n{document.raw_text}".lower()
    patterns = [
        ("COI", [r"certificate of insurance", r"\bcoi\b"]),
        ("W9", [r"\bw-?9\b", r"taxpayer identification number"]),
        ("MSA", [r"master services? agreement", r"\bmsa\b"]),
        ("DPA", [r"data processing agreement", r"\bdpa\b"]),
        ("SOC2", [r"soc 2", r"type ii", r"trust services criteria"]),
        ("BANK", [r"routing number", r"account holder", r"bank name"]),
    ]
    for doc_type, regexes in patterns:
        if any(re.search(pattern, text) for pattern in regexes):
            return ClassificationResult(
                doc_id=document.doc_id,
                filename=document.filename,
                doc_type=doc_type,  # type: ignore[arg-type]
                confidence=0.9,
                reasoning="Heuristic classifier matched strong document markers.",
                needs_human_confirm=False,
            )
    return ClassificationResult(
        doc_id=document.doc_id,
        filename=document.filename,
        doc_type="UNKNOWN",
        confidence=0.4,
        reasoning="No strong document markers were detected.",
        needs_human_confirm=True,
    )


def _get_client() -> Optional[OpenAI]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def classify_document(document: IngestedDocument) -> ClassificationResult:
    client = _get_client()
    if client is None:
        return _heuristic_classify(document)

    model = os.getenv("OPENAI_MODEL_CLASSIFY", "gpt-4o-mini")
    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": CLASSIFICATION_PROMPT}]},
                {"role": "user", "content": [{"type": "input_text", "text": _truncate_text(document.raw_text)}]},
            ],
            text={"format": {"type": "json_object"}},
        )
        payload = json.loads(response.output_text)
        confidence = float(payload.get("confidence", 0.0))
        doc_type = payload.get("doc_type", "UNKNOWN")
        if confidence < 0.60:
            doc_type = "UNKNOWN"
        return ClassificationResult(
            doc_id=document.doc_id,
            filename=document.filename,
            doc_type=doc_type,
            confidence=confidence,
            reasoning=payload.get("reasoning", ""),
            needs_human_confirm=0.60 <= confidence < 0.85 or doc_type == "UNKNOWN",
        )
    except Exception:
        return _heuristic_classify(document)
