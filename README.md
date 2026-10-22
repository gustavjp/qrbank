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
- classroom-scoped rewards catalog with positive integer prices, reward types, active/inactive status, and audited create/edit/status changes
- classroom reward purchasing from the student detail page or classroom Rewards page, through one central financial service
- idempotent purchase requests, immutable reward/price snapshots, fulfillment history, and optional stock decrement audit events
- coordinated reward refunds with exact linked reversal credits, cancelled purchase state, and atomic stock restoration when the original purchase consumed inventory
- owner/teacher reward fulfillment (redeem and reasoned reopening), immutable event history, optimistic stale-form protection, and classroom-wide status filtering
- optional per-reward stock setup and reasoned stock-count adjustments, with tenant-scoped history, audit records, concurrency safeguards, and purchase/refund coordination
- classroom-configured positive/negative behavior reasons and audited post-scan reason/detail attachment, without financial ledger mutation
- classroom-wide read-only ledger browsing with tenant-bound student/type/source/category/local-date filters and stable keyset pagination
- authorized streaming CSV export of all filtered posted ledger entries, with immutable identifiers, numeric balances, local/UTC timestamps, and spreadsheet formula safeguards
- authorized classroom roster/balance CSV export with active/all selection, integer balances, enrollment status and dates, and spreadsheet formula safeguards
- audited set-balance adjustments (server-computed difference) and append-only transaction reversals
- exact inverse reversals, reversal-of-reversal support, and duplicate undo protection
- central opening-balance, QR Action, manual, weekly wage, weekly allowance, reward purchase, and reward refund financial posting services with PostgreSQL row locking and idempotency
- append-oriented balance-changing ledger history with stored balance cache updates in the same transaction
- health and readiness endpoints
- PostgreSQL-backed automated tests

Weekly job wages and student allowances have external scheduler commands, but no scheduler is provisioned or enabled automatically. Inventory tracking cannot yet be disabled after activation; refund undo, reward images, mobile camera integration, password reset, and a production email provider are not implemented yet. Scan Mode can post QR Action/Quick Action transactions to the ledger.

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

## Classroom rewards catalog

Owners and teachers can create, edit, deactivate and reactivate reward definitions
from the classroom's Rewards page. A reward has a classroom-scoped code (for
example, R01), name, optional description, type (Physical, Privilege, Other),
and a positive integer price. Assistants can browse the catalog but cannot
change it. Inactive rewards remain visible for historical continuity.
All meaningful changes create before/after administrative audit entries.
Duplicate reward codes are rejected within a classroom but may be reused
by an unrelated classroom.

Catalog creation/edit/status operations only update reward definitions and
audit events; they do not change student balances. Owners and teachers can
purchase an active reward for an active enrolled student either on that
student's detail page or from the classroom's Rewards page. Both forms use
the same centrally controlled financial purchase service. Assistants cannot
purchase.

The server validates the displayed price against the current price before
charging, checks availability and current stock, and requires sufficient
points even when the classroom allows negative balances for other operations.
Within one PostgreSQL transaction it locks the student balance and reward,
posts a negative integer reward_purchase ledger entry and updated cached
balance, creates a reward purchase with the reward's code/name/type/price
snapshots, and records an initial `unused` fulfillment event. If inventory
tracking is enabled, it also decrements one unit and appends an auditable
inventory event. Duplicate request IDs replay the original result; reusing
an ID with different purchase inputs is rejected. Rejected attempts do not
deduct money or create a purchase, and keep an idempotent rejection record.

The Inventory link on each reward provides optional stock tracking. An owner
or teacher can enable it with a nonnegative initial physical count and a
required reason, then make reasoned adjustments by setting an exact new
available count. Both operations run under a PostgreSQL reward row lock and
append administrative audit entries; nonzero changes also append an inventory
movement with before/after quantities and actor. Enabling with zero stock is
recorded in the administrative audit without a zero-delta inventory event,
which the database deliberately forbids. A stale adjustment is rejected if
another purchase, refund, or edit changed stock since the form was opened.
Assistants can view inventory and movement history but cannot change it.
Tracking cannot be switched off once enabled, because a future refund may
need to restore stock from a prior purchase. Inventory changes never post
financial ledger entries or change student balances.

Student detail pages show historical purchases and their fulfillment status.
The classroom-wide Reward Purchases & Fulfillment page lists purchases by
status and provides a per-purchase event history with timestamps, actors, and
notes. Owners and teachers can mark an unused purchase redeemed, or reopen a
redeemed purchase with a required correction reason; assistants can view but
cannot change fulfillment. Each transition appends a fulfillment audit event
and changes only the purchase status, never its ledger amount, student balance,
or stock. The forms carry the latest event ID and reject stale submissions,
including an old redeem request submitted after a later correction. All
fulfillment changes lock student, reward, and purchase in the same order as
refunds to prevent conflicting concurrent operations. Cancelled purchases
cannot be redeemed or reopened.

For an unused purchase, owners and teachers can supply a refund reason and
apply a coordinated refund. The service appends an exact positive `reversal`
ledger entry linked to the original purchase transaction, updates the cached
student balance, marks the purchase `cancelled`, and appends a fulfillment
event. A refund restores one inventory unit *only when the original purchase
recorded a stock decrement*, with its own inventory event. Duplicate refund
requests replay the same result, while distinct attempts cannot refund the
same purchase twice. Inactive students and inactive/renamed rewards may still
be refunded using immutable purchase snapshots.

Redeemed or already-cancelled purchases are not eligible for this workflow.
Generic reversals of reward purchases, and generic undo of reward refund
reversals, remain blocked because they would desynchronize purchase and
inventory state. Disabling inventory tracking, undoing
a refund, availability scheduling, and image uploads are separate milestones.

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

Job assignment changes do not directly pay wages or modify balances. When weekly
wages are enabled in classroom settings, the independent scheduler command can
pay active assignments after the classroom-local weekly due time (default Monday
6:10 AM, America/New_York). Each assignment gets a distinct wage transaction,
with its job name, code, and wage snapshotted in the append-only ledger.
Payments use the central financial service with PostgreSQL enrollment row locks,
per-assignment/week request idempotency, and unique ScheduledPayment records.
Zero wages consume the week without adding a fake zero-point ledger transaction.

Invoke from a trusted external scheduler rather than a FastAPI web worker:

```bash
python -m app.tasks.weekly_wages
```

Run the command periodically (for example, every ten minutes) using a platform
cron job. Each invocation checks the classroom's own IANA timezone and schedule,
and pays only when the current local week's due time has passed. Repeated and
concurrent invocations cannot double-pay the same assignment/week. New jobs or
students created after that week's deadline are not backpaid for that week;
inactive or ended assignments and inactive jobs/students are skipped. A failure
for one student rolls back that student's payment but does not stop the others;
subsequent invocations retry missing payments. A nonzero command exit status
indicates that some payments failed and should trigger an operational alert.

No automatic task is installed by this code alone. Enable and monitor the
external scheduler before relying on weekly wages in a live classroom.

## Weekly student allowances

When weekly allowance is enabled in classroom settings, active enrolled
students can receive a nonnegative integer allowance at the classroom-local
payday (default 20 points each Monday at 6:00 AM in America/New_York).
The configuration is disabled by default. A trusted external scheduler should
invoke:

```bash
python -m app.tasks.weekly_allowances
```

Run periodically (for example, every ten minutes) and monitor nonzero exit
codes for partial failures. No task is automatically installed or launched
by FastAPI. Each eligible enrollment gets at most one allowance for each
classroom-local week, even with simultaneous runner invocations or retries.
The central financial service locks the enrollment row and atomically writes
its updated cached balance, idempotent FinancialRequest, ScheduledPayment,
and append-only allowance ledger transaction. A configured zero allowance
consumes the week without creating a zero-point financial transaction.

Inactive/ended enrollments and enrollments created after the scheduled
deadline are excluded. Editing an allowance amount after a completed payment
does not retroactively change the earlier paid amount; the next week's
payment uses the new amount. Errors on one student roll back that student's
entire payment and allow other students to be paid; missing payments can
be retried in a later invocation. The scheduler respects IANA timezones,
daylight saving transitions, and current classroom settings.

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
