# Collection Email Automation CLI Design

## Intent

Build a self-contained command-line assessment application that demonstrates accurate accounts-receivable reminder eligibility, synthetic customer-response handling, conservative human-review decisions, email generation, and a complete audit trail. The application must be easy for an assessor to run locally without credentials, cloud services, or a live mailbox.

The application is a working prototype, not a slideware architecture exercise. Its design should make the core rules obvious, testable, and replaceable with production adapters later.

## Goals

- Read the supplied Excel workbook and use its `Controls` as-of date.
- Validate and normalize mixed AR transactions without aborting the batch for one bad row.
- Consider only document numbers beginning `INV-` for reminder eligibility.
- Ignore invoices with aging day 0.
- Assign each eligible invoice to exactly one required reminder stage.
- Explain how overdue invoices outside the exact stage thresholds are handled.
- Read synthetic mailbox fixtures and associate responses with invoices conservatively.
- Classify and summarize relevant responses.
- Make a traceable human-review decision for every processed invoice.
- Render professional reminder or escalation emails.
- Simulate `send` and `draft` actions without contacting a real email service.
- Produce assessor-friendly output files and a persisted audit history.
- Demonstrate deterministic tests, idempotent processing, error isolation, and clear extension points.

## Non Goals

- Connecting to Outlook, Microsoft Graph, Gmail, or another live mailbox.
- Sending real email.
- Building a graphical interface, web API, background scheduler, or distributed worker system.
- Using a full workflow engine or message broker.
- Reconciling claimed payments against a bank, ERP, or payment ledger.
- Changing the assessment's exact aging-day rules to more resilient production rules.
- Grouping several invoices into one customer-level reminder.
- Requiring an external AI service for response classification.

## Anti Goals

- Hiding business rules inside prompts or opaque framework configuration.
- Treating an ambiguous response as permission to send automatically.
- Treating aging day 0 as proof that an invoice is paid.
- Silently assigning days 5, 7, or 9 to a nearby reminder stage.
- Stopping the entire run because one record or mailbox message is invalid.
- Claiming that a simulated action was actually sent.
- Producing only aggregate counts without invoice-level evidence.

## Source Facts and Constraints

- The supplied workbook contains 300 AR records: 295 `INV-` invoices and 5 `CRD-` credit memos.
- The workbook's as-of date is 2026-03-25 and its aging formulas are internally consistent.
- The assessment data has no customer-response or mailbox records, so response scenarios must be supplied separately as local fixtures.
- The exact eligibility rules are: day 4 initial, day 6 first, day 8 second, day 10 third, and over day 10 escalation.
- The supplied data produces 1 initial, 2 first, 5 second, 6 third, and 202 escalation candidates.
- Five overdue invoices at days 5, 7, and 9 have no stage under the stated rules and must be recorded as ignored with a specific reason.
- Seventy-four invoices at aging day 0 are not eligible. Most have future due dates; the application must not label them as paid.
- The solution must operate in dry-run mode only.
- The primary user is an assessor running the application locally and reviewing its outputs.

## Approaches Considered

### Python Modular Pipeline

A Typer CLI coordinates small Python modules for ingestion, validation, eligibility, mailbox matching, response classification, decisioning, rendering, action simulation, and auditing. Pydantic models define module contracts, SQLite stores run history, and pytest verifies business behaviour.

This approach gives the clearest rule traceability, quickest local setup, strongest Excel and text-processing support, and best assessment readability. It is the selected approach.

### Python Workflow State Machine

Every invoice would move through a persisted workflow state machine with explicit transitions. This offers strong retry semantics but introduces infrastructure and transition machinery that exceed the needs of a self-contained assessment.

### .NET CLI

A .NET implementation would provide strong typing and an easy future route to Microsoft Graph. It requires more supporting code and setup while providing little additional value for the offline prototype.

## Selected Technical Direction

- Python 3.12.
- Typer for command-line commands and help text.
- Pydantic 2 for typed records and validation.
- openpyxl for workbook reading with formulas and cached values preserved.
- Jinja2 for plain-text and HTML email templates.
- Python `sqlite3` for local audit persistence without an ORM.
- Python standard-library `email` package for `.eml` artifacts.
- pytest for unit and end-to-end tests.
- Ruff for linting and formatting.
- No network access is required at runtime.

## Command Line Experience

The installed command is `collection-email`.

### Validate

```text
collection-email validate \
  --input "docs/Collection Email Automation Synthetic Data.xlsx"
```

This command validates workbook structure, controls, formulas, and records. It prints a concise summary and writes no workflow actions.

Exit code 0 means the file can be processed. Exit code 2 means a file-level error prevents processing. Row-level validation issues are reported but do not make the file unusable when valid rows remain.

### Run

```text
collection-email run \
  --input "docs/Collection Email Automation Synthetic Data.xlsx" \
  --mailbox fixtures/mailbox_responses.json \
  --output outputs/demo
```

This command executes the complete dry-run workflow. It prints the run ID, record counts, stage counts, action counts, failures, and output path.

`--as-of YYYY-MM-DD` may override the workbook control for boundary testing. When omitted, the workbook value is authoritative. The output manifest records whether the date came from the workbook or an override.

When an override is supplied, the application preserves the workbook's aging value as `source_aging_days`, recomputes `effective_aging_days` from the override and due date, and uses the effective value for validation and eligibility. A source-aging mismatch is reported as an informational audit observation rather than a row validation failure in override mode. Without an override, the workbook as-of date and aging value must reconcile or the row is invalid.

### Inspect

```text
collection-email inspect --run-id RUN_ID --invoice INV-2026-10010
```

This command reads SQLite audit data and prints the selected invoice's normalized input, eligibility reason, matched messages, classification, summary, review decision, and simulated final action.

## Processing Architecture

```text
CLI
 └─ Run service
     ├─ Workbook reader
     ├─ Record validator
     ├─ Eligibility engine
     ├─ Fixture mailbox reader
     ├─ Response matcher
     ├─ Response classifier and summarizer
     ├─ Human-review decision engine
     ├─ Email renderer
     ├─ Dry-run action executor
     ├─ Audit repository
     └─ Output writer
```

The run service is the only orchestration component. Domain modules return typed results and do not write files or database rows directly. Output and persistence happen at explicit boundaries.

## Core Domain Models

### ARRecord

- `source_row: int`
- `customer_name: str`
- `customer_email: str`
- `document_number: str`
- `amount: Decimal`
- `issue_date: date`
- `due_date: date`
- `aging_days: int`
- `aging_bucket: str`

### EligibilityResult

- `document_number: str`
- `status: eligible | ignored | invalid`
- `stage: initial | first | second | third | escalation | none`
- `reason_code: str`
- `reason: str`

### EmailMessage

- `message_id: str`
- `thread_id: str | None`
- `sender: str`
- `recipients: list[str]`
- `subject: str`
- `body: str`
- `received_at: datetime`
- `in_reply_to: str | None`

### ResponseAssessment

- `classification: no_response | payment_promised | payment_already_made | partial_payment | dispute_or_query | document_request | out_of_office | bounce_or_invalid_contact | no_meaningful_response | ambiguous`
- `summary: str`
- `payment_date: date | None`
- `dispute_details: str | None`
- `requested_action: str | None`
- `confidence: Decimal`
- `source_message_ids: list[str]`
- `match_reason: str`

### DecisionResult

- `human_review_needed: bool`
- `reason_code: str`
- `reason: str`
- `intended_action: ignored | ready_to_send | drafted_for_review | failed`

### AuditRecord

Contains the normalized input, eligibility result, response assessment, decision, artifact paths, errors, timestamps, run ID, and idempotency key.

## Validation Behaviour

File-level failures stop the run before processing:

- File cannot be opened.
- Required `AR_Data` or `Controls` sheet is absent.
- Required headers are absent.
- The as-of date is missing or invalid and no override was supplied.

Row-level failures quarantine only that record:

- Missing customer name, email, document number, amount, due date, or aging days.
- Malformed email address.
- Invalid or negative amount.
- Issue date after due date.
- Aging days is negative or differs from `max(as_of_date - due_date, 0)`.
- Duplicate document number.

Duplicate handling is deterministic: the first occurrence is retained for evidence but marked invalid together with every later occurrence. No duplicate is processed for an email action.

## Eligibility Behaviour

Rules are evaluated in this order:

1. Invalid record: `invalid`.
2. Document number does not start with `INV-`: `ignored/non_invoice`.
3. Aging day equals 0: `ignored/not_overdue`.
4. Aging day equals 4: `eligible/initial`.
5. Aging day equals 6: `eligible/first`.
6. Aging day equals 8: `eligible/second`.
7. Aging day equals 10: `eligible/third`.
8. Aging day is greater than 10: `eligible/escalation`.
9. Any remaining positive age: `ignored/no_stage_for_aging_day`.

The engine returns one and only one result for every row.

## Synthetic Mailbox Fixture

`fixtures/mailbox_responses.json` contains an array of `EmailMessage` objects. It must include representative examples for every response classification, multiple replies in one thread, one ambiguous message, one response referencing the wrong invoice, and one malformed fixture that is isolated and reported.

Matching priority is:

1. A stored prior `thread_id` associated with the invoice.
2. Exact document number in the subject plus sender equality with the customer email.
3. Exact document number in the body plus sender equality with the customer email.
4. Exact document number without sender equality, classified as ambiguous.
5. Sender equality without an invoice reference is never a strong match. When the customer has one candidate invoice, attach it as a weak ambiguous match that requires review. When the customer has multiple candidate invoices, do not attach it to any one invoice; record the message as unresolved and record an ambiguous response assessment against each affected candidate without copying the message body into their summaries.

Messages with no defensible link are not attached to an invoice. When several relevant messages exist, the latest relevant reply drives the classification while earlier relevant context is available to the summarizer and audit record.

## Classification and Summary Behaviour

The prototype classifier is deterministic and offline. Ordered rules detect:

- Delivery failure and invalid-recipient signals.
- Out-of-office signals.
- Dispute and query language.
- Claims that payment has already been made.
- Partial-payment language.
- Promised-payment language and parseable dates.
- Requests for invoice copies, statements, or supporting documents.
- Relevant replies with no actionable meaning.

Conflicting signals, unparseable critical dates, weak matches, or unmatched intent produce `ambiguous`. The summary is generated from extracted facts using deterministic templates and never invents a payment date, dispute detail, or requested action.

Classifier precedence is `bounce_or_invalid_contact`, `out_of_office`, `dispute_or_query`, `payment_already_made`, `partial_payment`, `payment_promised`, `document_request`, then `no_meaningful_response`. A message that contains strong signals from more than one business class is `ambiguous` unless one signal is only quoted history from an earlier message. Matching is case-insensitive and ignores punctuation for phrase detection.

The assessment vocabulary is stored as versioned configuration and includes representative phrases used by the mailbox fixtures. Payment-date extraction accepts ISO `YYYY-MM-DD`, UK `DD/MM/YYYY`, and unambiguous written dates such as `27 March 2026`. Relative dates such as `tomorrow`, dates without a year, and invalid calendar dates are not resolved; a promised-payment message containing only such a date is classified as `ambiguous` and requires review.

The classifier implements an interface so a future AI classifier can be added, but the assessment has no external AI dependency.

## Human Review Decision

| Response classification | Human review | Intended action |
|---|---:|---|
| No response | No | Ready to send |
| Payment promised | Yes | Drafted for review |
| Payment already made | Yes | Drafted for review |
| Partial payment | Yes | Drafted for review |
| Dispute or query | Yes | Drafted for review |
| Document request | Yes | Drafted for review |
| Out of office | Yes | Drafted for review |
| Bounce or invalid contact | Yes | Drafted for review |
| No meaningful response | Yes | Drafted for review |
| Ambiguous | Yes | Drafted for review |

Invalid, non-invoice, not-overdue, and no-stage records are ignored before response processing. An unexpected processing error produces `failed` for that invoice and does not abort other invoices.

## Email Generation

There are five stage-specific templates in both text and HTML form. Escalation wording is firmer than reminder wording but remains professional.

Every email includes:

- Customer name.
- Invoice number.
- Outstanding amount.
- Due date.
- Reminder stage.
- A response-context note only when human review is required and relevant context exists.

Each eligible invoice generates one email artifact. Customer-level consolidation is deliberately excluded because it would complicate response matching and depart from the invoice-level requirement.

`ready_to_send` actions are written under `emails/simulated_send/`. Review cases are written under `emails/drafts/`. Both are `.eml` files, and neither path can transmit email.

## Audit and Idempotency

SQLite stores:

- Runs and input-file fingerprints.
- Normalized records and validation issues.
- Eligibility results.
- Matched mailbox message IDs and match reasons.
- Response assessments.
- Review decisions.
- Generated artifact paths and final actions.
- Per-record exceptions.

The action idempotency key is a SHA-256 hash of the input-file fingerprint, as-of date, document number, reminder stage, and template version. Re-running the same inputs does not create a second simulated action for the same key. A new run still records that the prior action was reused.

Every run remains self-contained. When an action is reused, the new run copies the original `.eml` artifact into its own output directory, records `action_reused = true` and `reused_from_action_id`, and does not insert a second logical action row. The copied file is an output artifact, not a newly simulated send or draft.

## Output Contract

Every run creates:

```text
outputs/<run-id>/
├── manifest.json
├── validation_issues.csv
├── initial_reminders.csv
├── first_reminders.csv
├── second_reminders.csv
├── third_reminders.csv
├── escalations.csv
├── ignored_records.csv
├── response_assessments.csv
├── actions.csv
├── errors.csv
└── emails/
    ├── simulated_send/
    └── drafts/
```

The manifest records source fingerprints, as-of provenance, command arguments, template version, counts, timestamps, and application version. CSV files use stable headers and one row per invoice.

## Error Handling and Exit Codes

- `0`: run completed; row-level ignores or validation issues may exist.
- `1`: run completed with one or more unexpected per-record failures.
- `2`: file-level validation or configuration failure prevented processing.

All exceptions are converted to specific error records at the nearest safe boundary. Stack traces are available in a debug log, while normal CLI output remains concise and business-readable.

## Testing Strategy

### Unit Tests

- Workbook schema and as-of date validation.
- Aging recalculation and mismatch detection.
- Prefix filtering and every aging boundary from 0 through 11.
- Duplicate document behaviour.
- Mailbox match priority and ambiguity.
- Every response classification.
- Latest relevant reply selection with earlier context retained.
- Every human-review decision.
- Template rendering and mandatory fields.
- Idempotency-key stability.

### Integration Tests

- Supplied workbook produces stage counts 1, 2, 5, 6, and 202.
- Credit memos and day-zero invoices are ignored with correct reasons.
- Days 5, 7, and 9 are ignored as `no_stage_for_aging_day`.
- A fixture set exercises normal send, escalation, and every review classification.
- One malformed row and one malformed mailbox fixture do not stop valid cases.
- A second identical run reuses actions rather than duplicating them.

### Acceptance Test

From a clean environment, an assessor can install dependencies, run one command, see the expected stage counts, inspect generated drafts and simulated-send emails, inspect one invoice's evidence, and run the complete test suite without network access.

## Production Extension Points

The design exposes interfaces for a live mailbox reader, AI response classifier, real action executor, and external audit repository. These are documentation-level extension points only for this assessment.

A production workflow should replace exact-day selection with persisted stage history and threshold-crossing logic so missed scheduled runs do not skip a reminder. That production improvement must not alter the assessment implementation's stated rules.

## Builder Calls

The following are deliberate implementation calls rather than user-specified requirements:

- Use `sqlite3` instead of an ORM to reduce setup and keep persistence visible.
- Default `no_meaningful_response` to human review for conservative behaviour.
- Mark every occurrence of a duplicate document number invalid rather than selecting one silently.
- Generate one email per invoice rather than grouping by customer.
- Use deterministic offline response classification and retain an adapter interface for future AI use.
- Persist audit data in a project-local database while keeping human-readable CSV outputs as the primary assessment evidence.
- Recompute effective aging when `--as-of` overrides the workbook and preserve source aging for audit comparison.
- Treat sender-only matches as ambiguous even when only one invoice is a candidate.
- Copy reused email artifacts into each new run directory while retaining one logical action.
