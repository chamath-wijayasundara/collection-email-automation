# Collection Email Automation (offline assessment)

This is a local, dry-run command-line prototype for assessing invoice reminder
eligibility, synthetic customer responses, review decisions, generated email
artifacts, and audit evidence. It is deliberately self-contained: it sends no
email, opens no network connection, requires no credentials, and does not call
an external service.

## Requirements and setup

Use Python 3.12 (the supported range is `>=3.12,<3.13`). From the repository
root, create an environment and install the local package with its test tools:

```sh
python3.12 -m venv .venv
.venv/bin/pip install ".[dev]"
```

The local install provides the `collection-email` command. Re-run the second
command after changing source files. For one-off source-tree verification only,
prefix commands with `PYTHONPATH=src`; the normal assessor workflow uses the
installed command.

## Assessor workflow

Validate the workbook without creating a run, audit database, actions, or
email artifacts:

```sh
.venv/bin/collection-email validate \
  --input "docs/Collection Email Automation Synthetic Data.xlsx"
```

Run the complete offline workflow. The command creates missing output and
database parent directories:

```sh
.venv/bin/collection-email run \
  --input "docs/Collection Email Automation Synthetic Data.xlsx" \
  --mailbox fixtures/mailbox_responses.json \
  --output outputs/demo \
  --database outputs/collection_email.db
```

The concise run summary includes a generated `Run ID`, record and action
counts, errors, and the exact output directory. With the supplied workbook it
reports these stage counts:

```text
Stage counts: initial=1, first=2, second=5, third=6, escalation=202
Actions: 216
Errors: 0
```

Use that printed run ID in the following commands. The first request shows an
escalation trace with a payment-promised response that is drafted for human
review; the second shows a normal initial-reminder/no-response case:

```sh
.venv/bin/collection-email inspect \
  --database outputs/collection_email.db \
  --run-id RUN_ID \
  --invoice INV-2026-10001

.venv/bin/collection-email inspect \
  --database outputs/collection_email.db \
  --run-id RUN_ID \
  --invoice INV-2026-10204
```

`inspect` prints the normalized input, raw source envelope (including linked
validation issues for invalid rows), eligibility, match evidence, response
assessment, review decision, final dry-run action, and its artifact path. An
unknown invoice/run trace exits with status 1.

For aging-boundary testing, `validate` and `run` accept a canonical ISO date:

```sh
.venv/bin/collection-email validate \
  --input "docs/Collection Email Automation Synthetic Data.xlsx" \
  --as-of 2026-03-25
```

Only `YYYY-MM-DD` is accepted. An override recalculates effective aging from
the due date while retaining the workbook aging as source evidence.

## Exit codes and output

- `0` — a command completed. Row-level validation issues, ignored records, and
  isolated malformed mailbox entries do not make an otherwise processable run
  fail.
- `1` — an individual invoice had an unexpected processing failure, or an
  `inspect` trace was not found. Other invoices remain auditable.
- `2` — a workbook, mailbox fixture, local configuration, or `--as-of` input
  could not be processed.

Each run writes `outputs/demo/<RUN_ID>/` (or the chosen output root) with:

```text
manifest.json
validation_issues.csv
initial_reminders.csv       first_reminders.csv
second_reminders.csv        third_reminders.csv
escalations.csv             ignored_records.csv
response_assessments.csv    actions.csv    errors.csv
emails/simulated_send/*.eml
emails/drafts/*.eml
```

The `.eml` files are local evidence only. Files under `simulated_send` mean
the deterministic decision was `ready_to_send`; they were **not** sent. Files
under `drafts` require human review. Every artifact has an
`X-Collection-Automation-Mode: DRY-RUN` header.

Runs are idempotent by the input fingerprint, effective as-of date, invoice,
stage, template version, matched mailbox evidence, response assessment, and
rendered content. Repeating identical evidence records a new run directory,
copies the existing `.eml` artifact into it, and reuses one logical action.
Changing a matched reply or its rendered draft creates a new action rather than
copying stale evidence. `manifest.json` records the application version, run
start/completion times, mailbox fingerprint, and complete normalized arguments.

## Data rules and assumptions

The workbook contains 300 AR records: 295 invoices and 5 credit memos. Its
control date is 2026-03-25. Only `INV-` documents are candidates. The exact
rules are day 4 = initial, day 6 = first, day 8 = second, day 10 = third, and
day greater than 10 = escalation. There is intentionally no nearest-stage
rounding: overdue invoices on days **5, 7, and 9** are ignored with
`no_stage_for_aging_day`. The 74 day-zero records are `not_overdue`, not an
assertion that they are paid. Credit memos are ignored.

The supplied expected counts are 1 initial, 2 first, 5 second, 6 third, and
202 escalation candidates (216 eligible actions in total). The remaining
records are preserved as evidence rather than silently removed.

`fixtures/mailbox_responses.json` is a JSON array of synthetic messages.
Normal entries contain `message_id`, optional `thread_id`, `sender`,
`recipients`, `subject`, `body`, timezone-aware `received_at`, and optional
`in_reply_to`. Synthetic delivery-status notifications additionally contain a
validated `delivery_status` object with the original message ID, recipient, and
document number; this defensibly associates a mailbox-daemon bounce with its
invoice.
It demonstrates each deterministic response classification, a multi-message
thread, ambiguous/wrong-invoice/sender-only cases, and one malformed entry.
That malformed entry is isolated into `validation_issues.csv`; valid fixture
entries and invoice processing continue.

CSV evidence is formula-neutralized when a text cell begins with `=`, `+`,
`-`, or `@`, so opening exported evidence in a spreadsheet does not evaluate
input-controlled formulas.

Matching is intentionally conservative. An exact invoice reference plus the
customer sender is strong evidence; sender-only evidence is ambiguous and
requires review. The offline classifier uses versioned phrase vocabulary and
does not invent payment dates or response details. Human review is required
for every meaningful, ambiguous, or uncertain customer response; only a
no-response assessment may produce a `ready_to_send` dry-run artifact.

## Verification

Run lint and tests after installation:

```sh
.venv/bin/ruff check .
.venv/bin/pytest -v --cov=collection_email --cov-report=term-missing
```

The integration tests cover expected supplied-data counts, malformed-fixture
isolation, CLI exit codes, trace inspection, per-record failure isolation, and
rerun idempotency. Unit tests cover the workbook, eligibility, matching,
classification, decisions, rendering, output writing, and SQLite audit
repository.

## Architecture and production extension points

`collection_email.cli` is a thin Typer boundary. `RunService` orchestrates
the workbook reader, eligibility service, mailbox reader/matcher, deterministic
classifier, decision service, renderer, dry-run executor, output writer, and
SQLite audit repository. Domain models define the contracts between those
components.

The assessment intentionally keeps integrations offline. A production system
could replace the fixture reader with a mailbox adapter, the deterministic
classifier with an approved AI or rules adapter, and the dry-run executor with
an authenticated delivery adapter. Those additions must preserve the existing
audit, idempotency, human-review, retry, credential-management, and safety
boundaries; none is included here.
