# Architecture

This project uses a small FastAPI service with a six-stage document review pipeline, append-only audit logging, and a static HTML analyst dashboard.

## End-to-End Flow

```mermaid
flowchart LR
    subgraph Vendor["Vendor"]
        V1["Vendor Portal<br/>ui/vendor.html"]
        V2["Fill Form<br/>company details + tier questions"]
        V3["Upload Documents<br/>COI · W9 · MSA · DPA · SOC2 · Bank"]
        V4["Submit<br/>POST /review"]
        V5["Confirmation<br/>session ID shown"]
        V1 --> V2 --> V3 --> V4 --> V5
    end

    subgraph Pipeline["Automated Pipeline"]
        P0["REVIEW_SESSION_STARTED"]
        P1["1. Ingest<br/>PDF · DOCX · JPG · PNG · TXT<br/>OCR when needed<br/>SHA-256 per file"]
        P2["2. Completeness Gate<br/>tier-specific required docs"]
        P3{"Gate PASS?"}
        P4["Gate fail<br/>session blocked"]
        P5["3. Classify<br/>gpt-4o-mini<br/>JSON response + confidence"]
        P6["4. Extract<br/>gpt-4o<br/>per-field confidence"]
        P7["5. Validate<br/>field rules · cross-doc checks<br/>OFAC mock · ABA mock"]
        P8["6. Decide<br/>status + routing + evidence pack"]
        P9["Outputs saved to logs/<br/>session.log + session.summary.json"]

        P0 --> P1 --> P2 --> P3
        P3 -->|Fail| P4 --> P9
        P3 -->|Pass| P5 --> P6 --> P7 --> P8 --> P9
    end

    subgraph Analyst["Analyst"]
        A1["Analyst Dashboard<br/>ui/analyst.html<br/>GET /sessions"]
        A2["Queue Tab<br/>priority-sorted sessions"]
        A3["Review Tab<br/>flags · extracted fields · evidence pack"]
        A4["Audit Trail Tab<br/>chronological timeline"]
        A5["Decision Panel<br/>approve / reject / request more info"]

        A1 --> A2
        A1 --> A3
        A1 --> A4
        A3 --> A5
    end

    P9 --> A1

    subgraph Audit["Append-only Audit Log"]
        L1["REVIEW_SESSION_STARTED"]
        L2["DOCUMENT_RECEIVED / DOCUMENT_REJECTED"]
        L3["COMPLETENESS_GATE"]
        L4["CLASSIFICATION"]
        L5["FIELD_EXTRACTED"]
        L6["FLAG_RAISED"]
        L7["EXTERNAL_CHECK_COMPLETED"]
        L8["ROUTING_ASSIGNED"]
        L9["ANALYST_DECISION"]
    end
```

## Notes

- `session.summary.json` contains the structured session snapshot and embeds the evidence pack markdown. It is not written as a separate markdown file.
- Routing outcomes in code are:
  - `LEGAL_AND_COMPLIANCE` for OFAC hits
  - `SENIOR_ANALYST` for critical flags
  - `STANDARD_ANALYST` otherwise
- Status outcomes in code are:
  - `BLOCKED`
  - `REVIEW_REQUIRED`
  - `REVIEW_RECOMMENDED`
  - `CLEAR`
