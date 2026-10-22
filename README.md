# QRBank

QRBank is a teacher-focused classroom banking, behavior, jobs, rewards, and QR scanning web application.

The repository is being built incrementally from the approved product specification, architecture, database design, and migration plan.

## Current milestone

The current application includes:

- FastAPI application entry point
- PostgreSQL + SQLAlchemy 2.x database connectivity
- Alembic migrations
- User and classroom ORM mappings
- Argon2 password hashing
- teacher registration
- hashed one-time email-verification tokens
- resend-verification workflow
- login blocked until email verification
- opaque server-side session cookies
- CSRF-protected server-rendered forms
- login/logout and protected dashboard foundation
- classroom owner/membership creation
- authenticated classroom listing and creation
- active classroom selection
- owner-only classroom settings management
- strict server-side membership authorization for every classroom read/write
- student enrollment list/create/detail/edit flows
- classroom-scoped Action and Quick Action creation, editing, and activation controls
- owner/teacher management permissions and assistant read access for Actions
- action configuration audit history; configuration never changes a balance
- QA1–QA10 default Action/Quick Action sheet for new classrooms, with safe opt-in setup for older classrooms
- classroom-scoped read-only QR command resolution and keyboard-scanner preview mode
- classroom-scoped student codes with active/inactive status
- atomic manual credit/deduction flow with required reason, category, idempotency, and behavior-event linkage
- per-student ledger history with category, note, source, and balances
- classroom-wide read-only ledger browsing with tenant-bound student/type/source/category/local-date filters and stable keyset pagination
- authorized streaming CSV export of all filtered posted ledger entries, with immutable identifiers, numeric balances, local/UTC timestamps, and spreadsheet formula safeguards
- authorized classroom roster/balance CSV export with active/all selection, integer balances, enrollment status and dates, and spreadsheet formula safeguards
- audited set-balance adjustments (server-computed difference) and append-only transaction reversals
- exact inverse reversals, reversal-of-reversal support, and duplicate undo protection
- central opening-balance, QR Action, and manual financial posting services with PostgreSQL row locking and idempotency
- append-oriented balance-changing ledger history with stored balance cache updates in the same transaction
- health and readiness endpoints
- PostgreSQL-backed automated tests

Mobile camera integration, rewards UI, scheduled payments, password reset, and a production email provider are not implemented yet. Scan Mode can post QR Action/Quick Action transactions to the ledger.

## Local setup

Python 3.12+ and PostgreSQL are required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Set the database connection:

```bash
export QRBANK_DATABASE_URL="postgresql+psycopg://qrbank:qrbank@localhost:5432/qrbank"
```

Optional development settings:

```bash
export QRBANK_ENVIRONMENT="development"
export QRBANK_PUBLIC_BASE_URL="http://localhost:8000"
export QRBANK_EMAIL_DELIVERY_MODE="console"
```

In development, verification links are written to the application console. Console delivery is rejected in production; a real provider must be configured before deployment.

Apply the schema:

```bash
alembic upgrade head
```

Run the application:

```bash
uvicorn app.main:app --reload
```

Then open:

```text
http://localhost:8000/auth/register
```

System checks:

- `GET /health` confirms the web process is running.
- `GET /ready` confirms PostgreSQL is reachable.

Run tests:

```bash
pytest
```

The integration test suite requires a disposable PostgreSQL database. GitHub Actions runs the suite against PostgreSQL 16.

## Classroom transaction CSV export

From a classroom's transaction history, use **Download matching transactions (CSV)**.
The download includes all posted ledger entries matching the selected student, type,
source, manual category, and classroom-local dates—not only the displayed page.
It includes transaction/request IDs, enrollment IDs, historical codes and action
snapshots, integer amounts and balance changes, reversals, and both UTC and
classroom-local timestamps with the classroom timezone. Rejected financial requests
do not appear in the ledger export. Exports are read-only, membership-authorized
and generated from a consistent PostgreSQL snapshot; dangerous spreadsheet
formula prefixes in text fields are escaped. Student codes such as `01` should
be imported as text to preserve leading zeros in spreadsheet software.

## Classroom roster and balance CSV export

On a classroom's Students page, select **Active only** or **All**, then use
**Download roster and balances (CSV)**. The active view exports active
enrollments only; the full view includes inactive enrollments and their
preserved balances. The data includes classroom, student, and enrollment IDs,
student code, optional display name, active state, exact integer balance, and
enrollment start/end dates. Private notes are intentionally excluded.
Exports use a read-only, repeatable-read PostgreSQL snapshot, recheck active
membership, and escape spreadsheet formula prefixes in text cells. The
export never changes any balance, ledger row, or enrollment.

When importing into spreadsheet software, treat student codes as text to
preserve leading zeros such as `01`.

## Production security defaults

- passwords are Argon2-hashed
- raw session tokens are never stored in PostgreSQL
- raw email-verification tokens are never stored in PostgreSQL
- session cookies are HttpOnly and SameSite=Lax
- session cookies default to Secure in production
- state-changing web forms require CSRF validation
- pending/unverified accounts cannot create authenticated sessions
- the active-classroom cookie stores context only; it never grants access
- classroom access is re-checked against active ClassroomMembership rows on the server
- nonzero enrollment starting balances are posted as immutable opening-balance transactions
- zero starting balances do not create fake zero-value ledger rows
- student code/status changes are audited; financial history snapshots are not rewritten

## Design documents

- `SPEC.md`
- `ARCHITECTURE.md`
- `DATABASE_DESIGN.md`
- `MIGRATION_PLAN.md`

These documents remain authoritative for features and safety requirements.
