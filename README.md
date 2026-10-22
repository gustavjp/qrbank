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
- classroom-wide read-only positive/negative behavior event history, student/type/local-date filters, full-filter summary counts, and stable pagination
- eight-week classroom-local positive/negative behavior trend chart using the same authorized student/type/date filters
- classroom-scoped job definitions with audited create/edit/deactivate/reactivate and nonnegative weekly wages
- classroom-scoped student job assignments with duplicate-active prevention, end dates, and durable audit history
- classroom-configured positive/negative behavior reasons and audited post-scan reason/detail attachment, without financial ledger mutation
- classroom-wide read-only ledger browsing with tenant-bound student/type/source/category/local-date filters and stable keyset pagination
- authorized streaming CSV export of all filtered posted ledger entries, with immutable identifiers, numeric balances, local/UTC timestamps, and spreadsheet formula safeguards
- authorized classroom roster/balance CSV export with active/all selection, integer balances, enrollment status and dates, and spreadsheet formula safeguards
- audited set-balance adjustments (server-computed difference) and append-only transaction reversals
- exact inverse reversals, reversal-of-reversal support, and duplicate undo protection
- central opening-balance, QR Action, and manual financial posting services with PostgreSQL row locking and idempotency
- append-oriented balance-changing ledger history with stored balance cache updates in the same transaction
- health and readiness endpoints
- PostgreSQL-backed automated tests

Automatic wage payments, mobile camera integration, rewards UI, scheduled payments, password reset, and a production email provider are not implemented yet. Scan Mode can post QR Action/Quick Action transactions to the ledger.

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

## Classroom jobs

From a classroom's Jobs page, owners and teachers can define jobs with classroom-scoped
codes (such as J01), names, nonnegative integer weekly wages, and optional notes.
Existing jobs can be edited, deactivated and reactivated; assistants can view the
catalog but cannot change it. Each meaningful change writes a before/after
administrative audit event. Duplicate job codes are rejected within a classroom.

Job definitions are configuration only: they never change student balances,
create financial requests, or pay wages. Teachers can also open Student Job
Assignments to assign an active job to an active student, and end assignments
without removing history. A student may hold multiple different active jobs;
the same student/job pair cannot be actively assigned twice. Dates are recorded
in the classroom's IANA timezone. Deactivated jobs and students cannot receive
new assignments, but their existing assignments can still be ended. Meaningful
assignment creation/end changes are administratively audited, and assistants
have read-only access.

Job assignments do not pay wages or modify balances. Automatic weekly wage
transactions will be introduced separately through the central financial
service, with PostgreSQL idempotency and append-only ledger guarantees.

## Weekly behavior analytics

The classroom Behavior history includes a read-only eight-week positive/negative
event chart. Each week begins on Monday in the classroom's configured IANA
timezone; PostgreSQL aggregates by local calendar week so daylight-saving
transitions do not shift events into the wrong week. The chart follows the
selected student, behavior type and classroom-local date filters.

The chart ends with the current classroom-local week, or the week containing
a selected end date; future-only start dates anchor their own week. Each of
the eight weeks appears even with zero events. Chart counts include zero-point
behavior events and remain unchanged when associated points are reversed.
They do not represent point totals or balances. The separate summary above
the chart still counts *all* matching events, not just the eight chart weeks.
The feature is read-only and does not change behavior, audit or financial data.

## Classroom behavior history

From a classroom page, open **Behavior** to browse recorded positive and
negative events and view total/positive/negative counts across the entire
filtered period, not just one page. Filter by student, behavior type, and
classroom-local date range. Pages are bounded to 50 events, with a stable
timestamp/UUID cursor. Manual behavior entries show their transaction notes;
Action behavior entries retain historical action name/code snapshots. Events
that change no points remain visible. Linked ledger changes and any later
financial reversal are labeled, but a reversal does not erase a recorded
behavior event. This reporting page cannot write ledger or behavior records.

Use **Download matching behavior events (CSV)** to export every event matching
the selected student, behavior type, and classroom-local date range; the
50-event on-screen page limit does not apply. Columns include immutable event
IDs, stored action/reason snapshots, event and financial notes, UTC and local
timestamps, the linked posted integer point amount (if any), and whether its
points were reversed. Zero-point behavior events remain included. Student
codes tied to ledger transactions use their historical financial snapshots.
CSV exports require classroom membership, recheck it inside a read-only
repeatable-read PostgreSQL transaction, escape spreadsheet formulas in text,
and do not modify balances or history. Treat student codes as text on import.

## Behavior reasons and post-scan detail

From a classroom's Behavior history, open **View and manage behavior reasons**
to create positive or negative reasons with classroom-specific codes, names,
descriptions, and active/inactive states. Classroom owners and teachers can
create, edit and deactivate reasons; assistants have read-only access.
Reason type is fixed after creation. Inactive reasons remain in historical
records and cannot be newly attached.

Use **View or update behavior detail** on an event to attach an active reason of
the matching type and an optional note after a QR scan or manual behavior post.
A reason's code/name are snapshotted into the behavior event at attachment and
remain unchanged when its catalog definition is renamed. Subsequent changes
to event reason/note are recorded atomically in administrative audit history,
including before and after values. Zero-point behavior events are supported.

These operations never change FinancialTransaction rows, balances, behavior
event types or timestamps. All writes are classroom-scoped, owner/teacher-only,
CSRF-validated, and backed by PostgreSQL transactions.

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
