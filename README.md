# Vendor Review Agent

FastAPI service for vendor document intake, AI-assisted document review, rules validation, routing, and analyst decisioning.

## What is included

- Six-stage review pipeline: ingest, gate, classify, extract, validate, decide
- Session persistence with append-only audit logs and summary snapshots
- Vendor submission portal and analyst dashboard as single-file HTML pages
- Render deployment file and test coverage for each stage
- Sample document generator for PDF, DOCX, PNG, JPG, and TXT examples, including a complete T1 doc set

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Add environment variables to `.env`:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL_CLASSIFY=gpt-4o-mini
OPENAI_MODEL_EXTRACT=gpt-4o
PROMPT_VERSION_CLASSIFY=1.0
PROMPT_VERSION_EXTRACT=1.0
```

4. Optionally generate the sample documents:

```bash
python sample_docs/generate_sample_docs.py
```

5. Run the app:

```bash
uvicorn main:app --reload
```

## Notes

- If no `OPENAI_API_KEY` is present, the app falls back to deterministic heuristics for classification and extraction so the local flow and tests still work.
- Image and scanned-PDF OCR requires the system `tesseract` binary. If it is missing, image ingestion is recorded as a structured ingest error instead of crashing the request.
- `logs/{session_id}.log` is append-only JSONL.
- `logs/{session_id}.summary.json` is the latest session snapshot used by the analyst queue APIs.
