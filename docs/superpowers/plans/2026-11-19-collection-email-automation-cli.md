# Collection Email Automation CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained Python CLI that reads the supplied AR workbook, applies exact reminder rules, evaluates synthetic customer replies, produces safe dry-run email artifacts, and preserves invoice-level audit evidence.

**Architecture:** A Typer entry point calls one application service that orchestrates focused, typed modules. Domain services are pure where practical; workbook, fixture, SQLite, and filesystem access sit behind explicit adapters. The application always runs offline and records simulated actions without any email transport dependency.

**Tech Stack:** Python 3.12, Typer, Pydantic 2, openpyxl, Jinja2, sqlite3, standard-library email, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-19-collection-email-automation-design.md`

## Global Constraints

- Runtime must require Python 3.12 or later within the 3.12 series.
- Runtime must not require network access, credentials, a mailbox, or an external AI service.
- The application must never send real email; it may only create `.eml` files and simulated action records.
- Use the workbook `Controls` as-of date unless `--as-of YYYY-MM-DD` is explicitly supplied.
- Preserve exact assessment stages: day 4 initial, day 6 first, day 8 second, day 10 third, and over day 10 escalation.
- Treat days 5, 7, and 9 as `ignored/no_stage_for_aging_day`; do not round or promote them.
- Treat day 0 as `ignored/not_overdue`; do not infer payment status.
- Process one email action per invoice; do not consolidate by customer.
- Mark every occurrence of a duplicate document number invalid.
- Default all ambiguous, conflicting, weakly matched, and no-meaningful-response cases to human review.
- Keep each run directory self-contained, including copied artifacts for reused idempotent actions.
- Keep business rules in Python modules and versioned configuration, never in prompts.

## Review Focus

- Duplicate document numbers: every duplicate occurrence is invalid and no action is produced; covered in Task 2 tests.
- As-of override: effective aging is recomputed while source aging remains audit evidence; covered in Task 2 tests.
- Sender-only response: one candidate is a weak ambiguous match, while several candidates remain unresolved and require review; covered in Task 4 tests.
- Conflicting reply signals or relative payment dates: classification is ambiguous and cannot auto-send; covered in Task 5 tests.
- Identical rerun: one logical action is retained while the new run receives its own copied `.eml`; covered in Tasks 7 and 8 tests.

---

## Planned File Structure

```text
pyproject.toml
README.md
src/collection_email/
├── __init__.py
├── cli.py
├── errors.py
├── domain/
│   ├── enums.py
│   └── models.py
├── workbook/
│   └── reader.py
├── eligibility/
│   └── service.py
├── mailbox/
│   ├── fixture_reader.py
│   └── matcher.py
├── responses/
│   ├── classifier.py
│   └── vocabulary.json
├── decisions/
│   └── service.py
├── emails/
│   ├── renderer.py
│   ├── executor.py
│   └── templates/
│       ├── initial.txt.j2
│       ├── initial.html.j2
│       ├── first.txt.j2
│       ├── first.html.j2
│       ├── second.txt.j2
│       ├── second.html.j2
│       ├── third.txt.j2
│       ├── third.html.j2
│       ├── escalation.txt.j2
│       └── escalation.html.j2
├── audit/
│   └── repository.py
├── outputs/
│   └── writer.py
└── application/
    └── run_service.py
fixtures/
└── mailbox_responses.json
tests/
├── conftest.py
├── unit/
│   ├── test_models.py
│   ├── test_workbook_reader.py
│   ├── test_eligibility.py
│   ├── test_fixture_reader.py
│   ├── test_response_matcher.py
│   ├── test_response_classifier.py
│   ├── test_decision_service.py
│   ├── test_email_renderer.py
│   ├── test_audit_repository.py
│   └── test_output_writer.py
└── integration/
    ├── test_run_service.py
    └── test_cli.py
```

## Task 1: Project Foundation and Domain Contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/collection_email/__init__.py`
- Create: `src/collection_email/errors.py`
- Create: `src/collection_email/domain/enums.py`
- Create: `src/collection_email/domain/models.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: The approved design's model fields and enum values.
- Produces: `ARRecord`, `RecordEnvelope`, `ValidationIssue`, `WorkbookReadResult`, `EligibilityResult`, `EmailMessage`, `MailboxLoadResult`, `MatchResult`, `ResponseAssessment`, `DecisionResult`, `RenderedEmail`, `ActionResult`, `RunRequest`, and `RunResult`; enums `EligibilityStatus`, `ReminderStage`, `ResponseClassification`, and `IntendedAction`.

- [ ] **Step 1: Initialize version control if the workspace is still not a repository**

Run: `git rev-parse --is-inside-work-tree || git init`

Expected: the command reports a Git work tree.

- [ ] **Step 2: Create packaging metadata and the failing domain-model test**

Define Python `>=3.12,<3.13`, runtime dependencies `typer`, `pydantic>=2`, `openpyxl`, and `jinja2`, development dependencies `pytest`, `pytest-cov`, and `ruff`, plus the console script `collection-email = collection_email.cli:app`.

```python
def test_ar_record_preserves_money_and_both_aging_values():
    record = ARRecord(
        source_row=2,
        customer_name="Example Ltd",
        customer_email="ar@example.test",
        document_number="INV-1",
        amount=Decimal("100.50"),
        issue_date=date(2026, 1, 1),
        due_date=date(2026, 3, 21),
        source_aging_days=4,
        effective_aging_days=4,
        aging_bucket="0-30",
    )
    assert record.amount == Decimal("100.50")
    assert record.effective_aging_days == 4
```

- [ ] **Step 3: Create the isolated development environment**

Run: `python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'`

Expected: the editable package and all declared runtime/development dependencies install successfully.

- [ ] **Step 4: Run the model test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_models.py -v`

Expected: FAIL because `collection_email.domain.models` does not exist.

- [ ] **Step 5: Implement enums, errors, and Pydantic models**

Use string-valued enums with the exact values in the design. Make money `Decimal`, dates real `date`/`datetime` values, and artifact paths `Path`. Define `FileValidationError` for exit-code-2 failures and retain source/effective aging separately on `ARRecord`.

Lock these cross-task fields now:

- `ValidationIssue`: `code`, `message`, `severity`, `source_row`, `field`.
- `RecordEnvelope`: `source_row`, `raw_values`, `record: ARRecord | None`, `issues`; this preserves invalid rows that cannot satisfy `ARRecord`.
- `WorkbookReadResult`: `file_fingerprint`, `source_as_of_date`, `effective_as_of_date`, `as_of_source`, `source_row_count`, `records`, `valid_records`, `issues`, `observations`.
- `EligibilityResult`: `source_row`, `document_number: str | None`, `status`, `stage`, `reason_code`, `reason`; missing identifiers remain `None` on invalid rows.
- `MailboxLoadResult`: `messages`, `issues`.
- `MatchResult`: `messages`, `match_reason`, `strength` (`none | weak | strong`), `unresolved_message_ids`, `requires_ambiguous_assessment`.
- `RenderedEmail`: `to_address`, `subject`, `text_body`, `html_body`, `stage`, `template_version`.
- `ActionResult`: `action_id`, `idempotency_key`, `intended_action`, `artifact_path`, `action_reused`, `reused_from_action_id`.
- `RunRequest`: `input_path`, `mailbox_path`, `output_root`, `database_path`, `as_of_override`.
- `RunResult`: `run_id`, `exit_code`, `manifest`, `traces`, `validation_issues`, `errors`, `output_dir`.

- [ ] **Step 6: Run focused verification**

Run: `.venv/bin/pytest tests/unit/test_models.py -v && .venv/bin/ruff check src/collection_email/domain tests/unit/test_models.py`

Expected: PASS and no lint findings.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/collection_email tests/conftest.py tests/unit/test_models.py
git commit -m "feat: define collection workflow domain contracts"
```

## Task 2: Workbook Ingestion and Validation

**Files:**
- Create: `src/collection_email/workbook/__init__.py`
- Create: `src/collection_email/workbook/reader.py`
- Create: `tests/unit/test_workbook_reader.py`

**Interfaces:**
- Consumes: `ARRecord`, `ValidationIssue`, `WorkbookReadResult`, and `FileValidationError` from Task 1.
- Produces: `read_workbook(path: Path, as_of_override: date | None = None) -> WorkbookReadResult` and `fingerprint_file(path: Path) -> str`.

- [ ] **Step 1: Write failing file-level validation tests**

Add tests that create small temporary workbooks and assert:

```python
with pytest.raises(FileValidationError, match="AR_Data"):
    read_workbook(path_without_ar_data)

with pytest.raises(FileValidationError, match="As Of Date"):
    read_workbook(path_without_control_date)
```

- [ ] **Step 2: Run the file-level tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_workbook_reader.py -k "missing" -v`

Expected: FAIL because `read_workbook` is undefined.

- [ ] **Step 3: Implement workbook structure and control reading**

Load the workbook once with `data_only=False` for formula evidence and once with `data_only=True` for cached values. Require `AR_Data`, `Controls`, the eight source headers, and a valid `Controls!B2` unless an override is supplied.

- [ ] **Step 4: Write failing row-validation tests**

Cover required blanks, malformed email, negative amount, issue date after due date, negative/mismatched aging, and duplicate document numbers. Assert both duplicate rows receive `duplicate_document_number` and neither appears in `valid_records`.

- [ ] **Step 5: Run row-validation tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_workbook_reader.py -k "row or duplicate" -v`

Expected: FAIL on missing row validation behaviour.

- [ ] **Step 6: Implement normalization and row-level issue isolation**

Normalize strings by trimming outer whitespace without changing case-sensitive identifiers. Convert amounts through `Decimal(str(value))`. Validate email with a deliberately small `local@domain.tld` syntax check. Return all source rows, valid records, validation issues, source/effective as-of dates, and the SHA-256 file fingerprint.

- [ ] **Step 7: Write and run the as-of override test**

```python
def test_override_recomputes_effective_aging_without_invalidating_source_aging(tmp_path):
    result = read_workbook(workbook_with_due_date("2026-03-21", source_age=4), date(2026, 3, 27))
    assert result.valid_records[0].source_aging_days == 4
    assert result.valid_records[0].effective_aging_days == 6
    assert result.observations[0].code == "source_aging_differs_under_override"
```

Run: `.venv/bin/pytest tests/unit/test_workbook_reader.py::test_override_recomputes_effective_aging_without_invalidating_source_aging -v`

Expected: PASS.

- [ ] **Step 8: Verify the supplied workbook**

Add a test using `docs/Collection Email Automation Synthetic Data.xlsx` and assert 300 source rows, 295 invoice-prefixed rows, 5 credit memos, and no validation issues.

Run: `.venv/bin/pytest tests/unit/test_workbook_reader.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/collection_email/workbook tests/unit/test_workbook_reader.py
git commit -m "feat: validate and normalize AR workbooks"
```

## Task 3: Reminder Eligibility Engine

**Files:**
- Create: `src/collection_email/eligibility/__init__.py`
- Create: `src/collection_email/eligibility/service.py`
- Create: `tests/unit/test_eligibility.py`

**Interfaces:**
- Consumes: `ARRecord`, `EligibilityResult`, `EligibilityStatus`, and `ReminderStage` from Task 1.
- Produces: `classify_eligibility(envelope: RecordEnvelope) -> EligibilityResult` and `classify_all(records: Sequence[RecordEnvelope]) -> list[EligibilityResult]`.

- [ ] **Step 1: Write the failing boundary table test**

```python
@pytest.mark.parametrize(
    ("age", "status", "stage", "reason_code"),
    [
        (0, "ignored", "none", "not_overdue"),
        (1, "ignored", "none", "no_stage_for_aging_day"),
        (4, "eligible", "initial", "initial_reminder_due"),
        (5, "ignored", "none", "no_stage_for_aging_day"),
        (6, "eligible", "first", "first_reminder_due"),
        (7, "ignored", "none", "no_stage_for_aging_day"),
        (8, "eligible", "second", "second_reminder_due"),
        (9, "ignored", "none", "no_stage_for_aging_day"),
        (10, "eligible", "third", "third_reminder_due"),
        (11, "eligible", "escalation", "escalation_due"),
    ],
)
def test_exact_assessment_boundaries(age, status, stage, reason_code, record_factory):
    result = classify_eligibility(envelope_factory(age=age))
    assert (result.status.value, result.stage.value, result.reason_code) == (status, stage, reason_code)
```

- [ ] **Step 2: Run the boundary test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_eligibility.py -v`

Expected: FAIL because the eligibility service does not exist.

- [ ] **Step 3: Implement the ordered eligibility rules**

Return `invalid` first when the envelope contains validation issues or no normalized record. Otherwise use `effective_aging_days`, check the `INV-` prefix before aging stages, and use `non_invoice` for all other prefixes. Return exactly one result per source envelope.

- [ ] **Step 4: Add supplied-data count assertions**

Read the supplied workbook through Task 2 and assert stage counts are `initial=1`, `first=2`, `second=5`, `third=6`, `escalation=202`; assert five invoice records use `no_stage_for_aging_day` and 74 use `not_overdue`.

- [ ] **Step 5: Run verification**

Run: `.venv/bin/pytest tests/unit/test_eligibility.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/collection_email/eligibility tests/unit/test_eligibility.py
git commit -m "feat: implement exact reminder eligibility rules"
```

## Task 4: Fixture Mailbox Loading and Response Matching

**Files:**
- Create: `src/collection_email/mailbox/__init__.py`
- Create: `src/collection_email/mailbox/fixture_reader.py`
- Create: `src/collection_email/mailbox/matcher.py`
- Create: `fixtures/mailbox_responses.json`
- Create: `tests/unit/test_fixture_reader.py`
- Create: `tests/unit/test_response_matcher.py`

**Interfaces:**
- Consumes: `ARRecord`, `EmailMessage`, `MailboxLoadResult`, and `MatchResult` from Task 1.
- Produces: `load_mailbox_fixture(path: Path) -> MailboxLoadResult` and `match_responses(record: ARRecord, customer_candidates: Sequence[ARRecord], messages: Sequence[EmailMessage], prior_thread_id: str | None = None) -> MatchResult`.

- [ ] **Step 1: Write failing fixture-isolation tests**

Create a temporary JSON array with one valid and one malformed message. Assert the loader returns one `EmailMessage`, one fixture issue, and does not raise for the malformed entry.

- [ ] **Step 2: Run the fixture test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_fixture_reader.py -v`

Expected: FAIL because the fixture reader is undefined.

- [ ] **Step 3: Implement fixture loading and create the assessment fixture**

Parse each array element independently through `EmailMessage.model_validate`. Populate `fixtures/mailbox_responses.json` with at least one message for every classification, a multi-reply thread, wrong-invoice content, weak sender-only cases, and one malformed entry.

- [ ] **Step 4: Write failing matching-priority tests**

Test exact priority in separate assertions: prior thread ID; invoice in subject plus sender; invoice in body plus sender; invoice with wrong sender; sender-only with one candidate; sender-only with multiple candidates; and no defensible match.

For the Review Focus cases assert:

```python
assert single_candidate_match.strength == "weak"
assert single_candidate_match.requires_ambiguous_assessment is True
assert multi_candidate_match.messages == []
assert multi_candidate_match.unresolved_message_ids == ["sender-only-1"]
```

- [ ] **Step 5: Run matcher tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_response_matcher.py -v`

Expected: FAIL because `match_responses` is undefined.

- [ ] **Step 6: Implement deterministic matching**

Compare normalized email addresses exactly and document numbers case-insensitively. Sort matched messages by `received_at`. Preserve match reasons and unresolved IDs; never attach sender-only text to several invoices.

- [ ] **Step 7: Run verification**

Run: `.venv/bin/pytest tests/unit/test_fixture_reader.py tests/unit/test_response_matcher.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/collection_email/mailbox fixtures/mailbox_responses.json tests/unit/test_fixture_reader.py tests/unit/test_response_matcher.py
git commit -m "feat: load and match synthetic mailbox responses"
```

## Task 5: Deterministic Response Classification and Summaries

**Files:**
- Create: `src/collection_email/responses/__init__.py`
- Create: `src/collection_email/responses/classifier.py`
- Create: `src/collection_email/responses/vocabulary.json`
- Create: `tests/unit/test_response_classifier.py`

**Interfaces:**
- Consumes: `MatchResult`, `ResponseAssessment`, and `ResponseClassification` from Task 1.
- Produces: protocol `ResponseClassifier.assess(match: MatchResult) -> ResponseAssessment` and implementation `RuleBasedResponseClassifier(vocabulary_path: Path).assess(match: MatchResult) -> ResponseAssessment`.

- [ ] **Step 1: Write failing tests for all classifications and precedence**

Parameterize representative phrases from the approved fixture for bounce, out of office, dispute, already paid, partial payment, payment promise, document request, and no meaningful response. Assert empty matches produce `no_response`.

- [ ] **Step 2: Run classification tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_response_classifier.py -k "classification or precedence" -v`

Expected: FAIL because the classifier is undefined.

- [ ] **Step 3: Add versioned vocabulary and implement ordered signal detection**

Store `version`, classification phrase arrays, and summary labels in JSON. Normalize text by lowercasing and replacing punctuation with spaces. Remove conventional quoted history lines beginning `>` and blocks introduced by an `On ... wrote:` marker before signal detection. Implement the exact precedence from the design while returning `ambiguous` when multiple strong business classes remain.

- [ ] **Step 4: Write failing date and conflict tests**

Assert `2026-03-27`, `27/03/2026`, and `27 March 2026` parse to the same date. Assert `tomorrow`, `27 March` without year, an invalid date, conflicting dispute/payment signals, and every weak match produce `ambiguous` with `confidence < Decimal("0.80")`.

- [ ] **Step 5: Run date/conflict tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_response_classifier.py -k "date or ambiguous or conflict" -v`

Expected: FAIL on missing conservative handling.

- [ ] **Step 6: Implement fact extraction and deterministic summaries**

Extract only facts supported by the selected messages. The latest relevant reply controls the class; earlier messages may contribute context but never override a later explicit status. Produce short summaries from fixed templates and list every source message ID.

- [ ] **Step 7: Run verification**

Run: `.venv/bin/pytest tests/unit/test_response_classifier.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/collection_email/responses tests/unit/test_response_classifier.py
git commit -m "feat: classify and summarize customer responses"
```

## Task 6: Review Decisions and Email Rendering

**Files:**
- Create: `src/collection_email/decisions/__init__.py`
- Create: `src/collection_email/decisions/service.py`
- Create: `src/collection_email/emails/__init__.py`
- Create: `src/collection_email/emails/renderer.py`
- Create: `src/collection_email/emails/templates/initial.txt.j2`
- Create: `src/collection_email/emails/templates/initial.html.j2`
- Create: `src/collection_email/emails/templates/first.txt.j2`
- Create: `src/collection_email/emails/templates/first.html.j2`
- Create: `src/collection_email/emails/templates/second.txt.j2`
- Create: `src/collection_email/emails/templates/second.html.j2`
- Create: `src/collection_email/emails/templates/third.txt.j2`
- Create: `src/collection_email/emails/templates/third.html.j2`
- Create: `src/collection_email/emails/templates/escalation.txt.j2`
- Create: `src/collection_email/emails/templates/escalation.html.j2`
- Create: `tests/unit/test_decision_service.py`
- Create: `tests/unit/test_email_renderer.py`

**Interfaces:**
- Consumes: `ARRecord`, `EligibilityResult`, `ResponseAssessment`, `DecisionResult`, and `RenderedEmail` from Task 1.
- Produces: `decide(eligibility: EligibilityResult, response: ResponseAssessment | None) -> DecisionResult` and `EmailRenderer(template_dir: Path, template_version: str).render(record: ARRecord, eligibility: EligibilityResult, response: ResponseAssessment, decision: DecisionResult) -> RenderedEmail`.

- [ ] **Step 1: Write the failing decision-table test**

Assert no response yields `human_review_needed=False` and `ready_to_send`. Parameterize every other response classification to yield `True` and `drafted_for_review`. Assert ignored eligibility produces `ignored` without needing a response assessment.

- [ ] **Step 2: Run the decision test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_decision_service.py -v`

Expected: FAIL because `decide` is undefined.

- [ ] **Step 3: Implement the explicit decision map**

Use a dictionary keyed by `ResponseClassification`; do not infer decisions from confidence alone. Raise a domain error if an eligible record reaches the service without a response assessment.

- [ ] **Step 4: Write failing renderer tests**

For all five stages, assert text and HTML contain customer name, invoice number, formatted amount, due date, and stage. Assert escalation subject/body is distinct and firmer. Assert a draft includes the response summary while a simulated-send case does not add a response-context note.

- [ ] **Step 5: Run renderer tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_email_renderer.py -v`

Expected: FAIL because templates and renderer do not exist.

- [ ] **Step 6: Implement stage templates and renderer**

Return a `RenderedEmail` containing `to_address`, `subject`, `text_body`, `html_body`, `stage`, and `template_version`. Use a stable money format with two decimal places and ISO due dates. Do not add a send method or transport dependency.

- [ ] **Step 7: Run verification**

Run: `.venv/bin/pytest tests/unit/test_decision_service.py tests/unit/test_email_renderer.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/collection_email/decisions src/collection_email/emails tests/unit/test_decision_service.py tests/unit/test_email_renderer.py
git commit -m "feat: decide review needs and render reminder emails"
```

## Task 7: SQLite Audit Repository and Logical Idempotency

**Files:**
- Create: `src/collection_email/audit/__init__.py`
- Create: `src/collection_email/audit/repository.py`
- Create: `tests/unit/test_audit_repository.py`

**Interfaces:**
- Consumes: Task 1 domain models and JSON-serializable model dumps.
- Produces: `compute_action_key(file_fingerprint: str, as_of_date: date, document_number: str, stage: ReminderStage, template_version: str) -> str` and `AuditRepository` methods `initialize()`, `start_run(request: RunRequest) -> str`, `record_invoice(...)`, `find_action_by_key(key: str) -> ActionResult | None`, `record_action(action: ActionResult) -> None`, `complete_run(run_id: str, result: RunResult) -> None`, and `get_invoice_trace(run_id: str, document_number: str) -> dict[str, Any]`.

- [ ] **Step 1: Write failing schema and round-trip tests**

Using a temporary SQLite file, assert `initialize` is repeatable, a run can be created/completed, an invoice trace retains eligibility/response/decision evidence, and an unknown run/invoice returns `None` rather than an invented trace.

- [ ] **Step 2: Run repository tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_audit_repository.py -k "round_trip or initialize" -v`

Expected: FAIL because the repository does not exist.

- [ ] **Step 3: Implement schema creation and explicit transactions**

Create normalized `runs`, `invoice_traces`, and `actions` tables. Store complex evidence as canonical JSON, enforce a unique action idempotency key, use foreign keys, and roll back failed transactions.

- [ ] **Step 4: Write failing idempotency tests**

Assert the same five inputs produce the same 64-character SHA-256 key; changing as-of date, stage, or template version changes it. Insert one action, then assert a second lookup returns the existing logical action and action count remains one.

- [ ] **Step 5: Run idempotency tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_audit_repository.py -k "idempotency or action" -v`

Expected: FAIL on missing key and uniqueness behaviour.

- [ ] **Step 6: Implement action lookup and uniqueness handling**

Never silently overwrite an action. Return the prior `ActionResult` so the caller can copy its artifact and record `reused_from_action_id` in the new run trace.

- [ ] **Step 7: Run verification**

Run: `.venv/bin/pytest tests/unit/test_audit_repository.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/collection_email/audit tests/unit/test_audit_repository.py
git commit -m "feat: persist workflow audit and idempotent actions"
```

## Task 8: Dry-Run Executor and Human-Readable Outputs

**Files:**
- Create: `src/collection_email/emails/executor.py`
- Create: `src/collection_email/outputs/__init__.py`
- Create: `src/collection_email/outputs/writer.py`
- Create: `tests/unit/test_output_writer.py`

**Interfaces:**
- Consumes: `RenderedEmail`, `DecisionResult`, `ActionResult`, `RunResult`, prior `ActionResult | None`, and all invoice trace models.
- Produces: `DryRunExecutor.execute(rendered: RenderedEmail, decision: DecisionResult, destination: Path, idempotency_key: str, prior_action: ActionResult | None = None) -> ActionResult` and `OutputWriter.write_run(output_dir: Path, result: RunResult) -> None`.

- [ ] **Step 1: Write failing `.eml` safety tests**

Assert `ready_to_send` writes under `emails/simulated_send`, review cases write under `emails/drafts`, files contain `X-Collection-Automation-Mode: DRY-RUN`, and no executor method accepts SMTP/Graph credentials or exposes `send`.

- [ ] **Step 2: Run executor tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_output_writer.py -k "eml or dry_run" -v`

Expected: FAIL because the executor is undefined.

- [ ] **Step 3: Implement standards-compliant `.eml` output**

Use `EmailMessage` from the standard library with `To`, `Subject`, plain text, HTML alternative, the dry-run header, run ID, and invoice number. Write atomically via a temporary file followed by replacement.

- [ ] **Step 4: Write failing complete-output-contract tests**

Construct a small `RunResult` and assert all required CSV/JSON files and both email directories exist, CSV headers are stable, and manifest counts match trace data.

- [ ] **Step 5: Write the failing reused-artifact test**

Execute with a prior action from another run. Assert the new run receives its own copied `.eml`, `action_reused is True`, `reused_from_action_id` is set, and the copied bytes equal the prior artifact.

- [ ] **Step 6: Run output tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_output_writer.py -v`

Expected: FAIL on missing output writer and reuse copy behaviour.

- [ ] **Step 7: Implement output writer and reused artifact copying**

Write `manifest.json`, validation, five stage files, ignored records, response assessments, actions, and errors. Create empty files with headers when a category has no records. Never point a new run's CSV at an artifact outside its run directory.

- [ ] **Step 8: Run verification**

Run: `.venv/bin/pytest tests/unit/test_output_writer.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/collection_email/emails/executor.py src/collection_email/outputs tests/unit/test_output_writer.py
git commit -m "feat: write dry-run emails and assessment outputs"
```

## Task 9: Application Orchestration and Error Isolation

**Files:**
- Create: `src/collection_email/application/__init__.py`
- Create: `src/collection_email/application/run_service.py`
- Create: `tests/integration/test_run_service.py`

**Interfaces:**
- Consumes: All services from Tasks 2-8 and `RunRequest`/`RunResult` from Task 1.
- Produces: `RunService.run(request: RunRequest) -> RunResult`, `RunService.validate(input_path: Path, as_of_override: date | None = None) -> WorkbookReadResult`, and `RunService.inspect(run_id: str, document_number: str) -> dict[str, Any] | None`.

- [ ] **Step 1: Write the failing happy-path integration test**

Run the supplied workbook with the fixture mailbox and a temporary database/output root. Assert the five stage counts are 1, 2, 5, 6, and 202; every eligible invoice has one response assessment, decision, and action artifact; credit memos, day-zero invoices, and gap-day invoices have the expected ignore reasons.

- [ ] **Step 2: Run the happy-path test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_run_service.py::test_supplied_workbook_end_to_end -v`

Expected: FAIL because `RunService` is undefined.

- [ ] **Step 3: Implement orchestration in one direction**

Read and validate, classify eligibility for every source envelope, skip response work for invalid/non-eligible records, load fixtures once, match/classify/decide/render eligible records, isolate unexpected exceptions per invoice, look up idempotent actions, execute dry-run artifacts, persist every source-row trace, write outputs, and complete the run. No downstream module may call back into `RunService`.

- [ ] **Step 4: Write failing per-record failure isolation test**

Inject a renderer test double that raises for one invoice. Assert the run continues, returns exit code 1, records one `failed` action/error, and produces normal artifacts for other eligible invoices.

- [ ] **Step 5: Run isolation test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_run_service.py::test_one_invoice_failure_does_not_abort_batch -v`

Expected: FAIL until exception isolation is implemented.

- [ ] **Step 6: Implement per-record exception boundaries and run completion**

Catch only unexpected exceptions around one invoice's response-to-output pipeline. File-level validation continues to raise `FileValidationError`. Persist debug traceback text while returning concise business error fields.

- [ ] **Step 7: Write and run identical-rerun integration test**

Assert the second run has a distinct run ID and self-contained output files, reuses every unchanged logical action, and leaves the database action count unchanged.

Run: `.venv/bin/pytest tests/integration/test_run_service.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/collection_email/application tests/integration/test_run_service.py
git commit -m "feat: orchestrate the collection automation workflow"
```

## Task 10: CLI Commands, Documentation, and Acceptance Verification

**Files:**
- Create: `src/collection_email/cli.py`
- Create: `tests/integration/test_cli.py`
- Create: `README.md`

**Interfaces:**
- Consumes: `RunService` from Task 9 and `FileValidationError` from Task 1.
- Produces: Typer `app` with `validate`, `run`, and `inspect` commands and documented assessor workflow.

- [ ] **Step 1: Write failing CLI contract tests**

Use `typer.testing.CliRunner`. Assert `--help` lists all three commands; `validate` reports record/issue counts without creating actions; file-level failure exits 2; `run` reports run ID, stage/action/error counts and output path; a run with per-record failure exits 1; `inspect` prints the requested invoice trace and exits 1 for an unknown trace.

- [ ] **Step 2: Run CLI tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_cli.py -v`

Expected: FAIL because `collection_email.cli` does not exist.

- [ ] **Step 3: Implement the Typer entry point**

Accept `Path` arguments for input, mailbox, output, and database, plus an optional ISO `--as-of`. Print concise labelled summaries. Map successful runs to 0, per-record failures to 1, and file/configuration failures to 2 without swallowing programmer errors outside command boundaries.

- [ ] **Step 4: Write the assessor README**

Document Python requirement, environment setup, exact validate/run/inspect commands, generated outputs, dry-run safety, expected supplied-data counts, test/lint commands, assumptions, exact-day gap behaviour, synthetic mailbox structure, and production extension points. Do not claim that any email is sent.

- [ ] **Step 5: Run the complete automated verification**

Run: `.venv/bin/ruff check . && .venv/bin/pytest -v --cov=collection_email --cov-report=term-missing`

Expected: Ruff passes; all tests pass; coverage report identifies no untested core business-rule branch.

- [ ] **Step 6: Run the clean assessor workflow**

```bash
.venv/bin/collection-email validate --input "docs/Collection Email Automation Synthetic Data.xlsx"
.venv/bin/collection-email run --input "docs/Collection Email Automation Synthetic Data.xlsx" --mailbox fixtures/mailbox_responses.json --output outputs/demo --database outputs/collection_email.db
```

Expected: validate exits 0; run exits 0 because the deliberately malformed mailbox fixture is an isolated reported fixture issue rather than an unexpected processing failure; output reports stage counts `1, 2, 5, 6, 202`; no network connection or email transport is attempted.

- [ ] **Step 7: Inspect one normal and one escalation case**

Run `collection-email inspect` for `INV-2026-10204` and one generated escalation action from the run manifest.

Expected: each trace shows normalized input, match evidence, classification, review decision, final dry-run action, and artifact path.

- [ ] **Step 8: Confirm no transport code exists**

Run: `rg -n "smtplib|SMTP\(|graph.microsoft|requests\.|httpx\.|send_message|sendMail" src tests`

Expected: no runtime email/network transport matches.

- [ ] **Step 9: Commit**

```bash
git add src/collection_email/cli.py tests/integration/test_cli.py README.md
git commit -m "feat: deliver collection automation assessment CLI"
```

## Final Verification

- [ ] Run `git status --short` and confirm only intended generated outputs are untracked or ignored.
- [ ] Run `.venv/bin/ruff check .` and confirm success.
- [ ] Run `.venv/bin/pytest -v` and confirm success.
- [ ] Run the validate, run, and inspect acceptance commands from Task 10.
- [ ] Compare the generated stage counts to the approved design: 1 initial, 2 first, 5 second, 6 third, and 202 escalation.
- [ ] Open representative `.eml` files from `simulated_send` and `drafts`; confirm mandatory fields and `DRY-RUN` header.
- [ ] Run the same command twice and confirm one logical action per idempotency key with copied artifacts in both run directories.
- [ ] Confirm the README clearly states that no email is sent and no external service is required.
