# QRBank Architecture Proposal

**Status:** Architecture Review Draft  
**Source of truth:** `SPEC.md`  
**Scope:** Architecture only — no migrations, no production code

---

# 1. Executive Summary

QRBank should be built as a multi-tenant web application with a Python/FastAPI backend, PostgreSQL database, and a lightweight server-rendered frontend enhanced with targeted JavaScript.

The most important architectural principle is that **all balance-changing operations flow through one authoritative financial transaction service**. That service must be responsible for locking the affected enrollment/balance row, validating the request, writing the ledger entry, updating the stored balance, enforcing idempotency, and committing atomically.

The recommended initial deployment is a single Linux VPS running:

- Nginx
- FastAPI application
- PostgreSQL
- a dedicated background-job runner invoked through systemd timers or equivalent
- TLS/HTTPS
- scheduled PostgreSQL backups

The frontend should avoid a large SPA initially. QRBank benefits more from predictable forms, accessibility, fast server-rendered pages, and small JavaScript modules for scanner mode, asynchronous financial actions, camera scanning, audio feedback, and live UI updates.

The database should use immutable internal UUID identifiers while preserving teacher-facing student IDs such as `01`. Students should be distinct from classroom enrollments so school-year rollover, future student accounts, transfers, and multi-classroom use remain straightforward.

QR payloads should use high-entropy opaque tokens rather than teacher/student identifiers. Tokens should be revocable and rotatable without changing the underlying student, Quick Action, or historical transactions.

The system should be designed for future co-teachers, student/parent accounts, and paid entitlements, but none of those future features should add unnecessary complexity to the first release.

---

# 2. Requirements Observed

The architecture is driven by these non-negotiable requirements:

1. Financial correctness must survive retries, concurrency, crashes, and simultaneous requests.
2. Tenant isolation must be enforced server-side on every classroom-scoped resource.
3. QR scanning is a primary workflow, not an optional add-on.
4. USB scanner workflows must remain fast and require minimal mouse interaction.
5. Mobile camera scanning must reuse the same server-side financial logic.
6. Financial history is append-oriented and corrections use reversal transactions.
7. A reversal must itself be reversible.
8. Weekly allowance and wage processing must be idempotent and crash-safe.
9. Teachers may own multiple classrooms.
10. The data model must accommodate future co-teachers, student accounts, parent accounts, and school administrators.
11. Rewards are first-class purchasable entities, not merely generic deductions.
12. QR codes must support bulk generation, PDF output, SVG/PNG downloads, regeneration, and invalidation.
13. Behavior tracking must not slow the rapid QR transaction path.
14. Historical transactions must preserve snapshots of names, prices, wages, reasons, and other context as it existed at transaction time.
15. The exact database schema is intentionally not fixed yet.

No architectural contradiction prevents proceeding. Several product decisions still require approval and are listed at the end.

---

# 3. Proposed Technology Stack

## Backend

**Recommendation: Python + FastAPI**

Reasons:

- strong request validation
- clear dependency injection patterns
- automatic OpenAPI documentation
- async support where useful
- excellent PostgreSQL ecosystem
- easy testability
- suitable for AI-assisted development

Recommended supporting libraries:

- SQLAlchemy 2.x
- Alembic
- psycopg
- Pydantic
- pytest
- httpx/TestClient for API tests
- passlib/argon2-cffi or another modern password-hashing library
- python-multipart only where needed for uploads
- qrcode/segno for QR generation
- ReportLab or WeasyPrint for PDF generation after evaluation

## Frontend

**Recommendation: server-rendered HTML plus progressive JavaScript**

Suggested approach:

- Jinja2 templates
- HTMX or small fetch-based modules for partial updates
- Alpine.js only if lightweight state management becomes useful
- dedicated vanilla JavaScript module for scanner capture
- CSS built with a small maintainable design system; Tailwind is optional, not required

Why not a large SPA initially:

- QRBank is form/dashboard heavy rather than complex client-state heavy
- SSR simplifies authentication and authorization
- less frontend duplication
- easier accessibility
- faster iteration
- simpler deployment
- easier to keep financial authority on the server

A SPA can be reconsidered later if the student/parent experience becomes much more interactive.

## Database

**PostgreSQL**

Required capabilities:

- ACID transactions
- row-level locking
- unique constraints for idempotency
- strong foreign-key support
- partial indexes / exclusion strategies if needed
- reliable reporting

## Web Server / OS

- Ubuntu or Debian
- Nginx reverse proxy
- FastAPI served by Gunicorn + Uvicorn workers or Uvicorn directly under systemd depending on measured needs
- HTTPS via Let's Encrypt / Certbot
- PostgreSQL local to the VPS initially, with later migration to managed PostgreSQL possible

---

# 4. Overall Architecture

QRBank should use a modular monolith for V1.

A modular monolith is preferred over microservices because it provides:

- simple deployment
- one transaction boundary
- one database
- straightforward debugging
- fewer failure modes
- lower operational cost

Recommended logical modules:

- `auth`
- `users`
- `classrooms`
- `students`
- `enrollments`
- `finance`
- `actions`
- `scanner`
- `qr`
- `behavior`
- `jobs`
- `rewards`
- `automation`
- `analytics`
- `audit`
- `exports`
- `integrity`

Important separation:

**Routes/controllers** accept and validate HTTP requests.

**Application/domain services** implement use cases.

**Repositories/data-access layer** performs database queries.

**Database models** represent persistence.

The financial service is the only component allowed to mutate balances.

---

# 5. Financial Transaction Architecture

This is the core of the system.

## Central service

All balance-changing actions should invoke a single service boundary, conceptually:

`FinancialTransactionService.apply(...)`

Inputs may include:

- enrollment/student account
- signed integer amount
- transaction category
- source
- actor
- optional action snapshot
- optional behavior snapshot
- optional reward snapshot
- optional job snapshot
- optional reversal reference
- idempotency key
- metadata/note

## Transaction flow

Within one PostgreSQL transaction:

1. Validate authenticated actor can access the classroom.
2. Lock the relevant balance-holding row with `SELECT ... FOR UPDATE`.
3. Read authoritative current balance and classroom settings.
4. Validate:
   - enrollment active where required
   - amount valid integer
   - action/reward/job active when applicable
   - negative-balance policy
   - reversal validity
   - idempotency key not already used
5. Calculate new balance.
6. Insert immutable ledger row containing balance before/after and historical snapshots.
7. Update stored current balance.
8. Insert/update related domain record if required:
   - reward purchase
   - inventory reservation/change
   - automation payment marker
9. Commit.
10. Return authoritative new balance and transaction ID.

If any step fails, rollback everything.

## Idempotency

Each financial request should carry a server-generated or client-generated opaque request ID.

The ledger or a dedicated request table should enforce uniqueness over an appropriate scope.

If the same idempotency key is submitted again:

- return the existing result if the original committed
- do not create another ledger entry

## Reversals

A reversal is a new transaction with opposite financial effect.

The ledger stores a reference to the transaction being reversed.

To support reversal-of-reversal:

- do not use a simple permanent boolean `is_reversed` as the sole source of truth
- model reversal relationships explicitly
- compute or track current reversal state carefully

The database design phase should choose the exact constraints.

## Stored balance vs ledger

Keep a stored current balance for fast UI access.

The ledger remains the audit trail.

An integrity job should periodically verify:

`expected balance = configured opening basis + sum(completed ledger amounts)`

The exact opening-balance strategy must be chosen during schema design.

---

# 6. Authentication and Authorization

## Authentication

Recommendation: **server-side sessions using secure cookies**, not browser-stored JWTs.

Why:

- browser-first application
- easier revocation
- simpler secure logout
- lower token leakage risk
- easier CSRF controls
- no need for stateless mobile API in V1

Cookie requirements:

- HttpOnly
- Secure
- SameSite=Lax by default
- short enough session lifetime
- server-side session revocation support

Passwords should use Argon2id or an equivalently modern password hashing algorithm.

## Email verification and reset tokens

Use:

- cryptographically random tokens
- database-stored token hashes, not raw token values
- single use
- expiration
- purpose-specific token type

## Authorization

Authorization should operate at the service/query layer, not just route decorators.

Every classroom-scoped operation must prove that the user has an active classroom membership with sufficient role.

Future roles should map to capabilities such as:

- classroom.view
- classroom.manage
- students.manage
- finance.transact
- finance.reverse
- rewards.manage
- jobs.manage
- analytics.view
- settings.manage

V1 can expose only Owner while preserving the membership/capability architecture.

---

# 7. Multi-Tenant Model

The recommended tenant boundary is **classroom membership / account ownership**, not separate databases per teacher.

Use one PostgreSQL database with strong relational scoping.

Every classroom-owned entity should link directly or indirectly to a classroom.

All service queries should be scoped through the authenticated user's membership.

Use non-sequential public identifiers where resources appear in URLs.

Do not rely on IDs being hard to guess as the security boundary.

Potential later hardening:

- PostgreSQL Row Level Security after the core model is stable

Recommendation for V1:

- implement explicit service-layer authorization first
- consider RLS as defense-in-depth after correctness tests are strong

---

# 8. Classroom / Student / Enrollment Model

## Classroom

A classroom is independent of users and is connected to users through memberships.

Conceptually:

- Classroom
- ClassroomMembership
- User

This supports future co-teachers without redesign.

## Student

A student represents a person/entity independent of a particular classroom.

## Enrollment

Enrollment represents that student participating in a classroom/school-year context.

The balance should most likely belong to the **enrollment**, not the global student, because classroom economies are classroom-specific.

This enables:

- same student in multiple classrooms
- independent balances
- school-year rollover
- archive preservation
- future linking of one authenticated student account to multiple enrollments

Teacher-facing StudentID like `01` belongs on enrollment/classroom context.

Recommended unique rule conceptually:

- teacher-facing student ID unique within a classroom

Exact schema awaits database review.

---

# 9. QR Scanning Architecture

## Scan Mode

Create a dedicated Scan Mode page optimized for USB scanners.

The browser should maintain a hidden or minimal scanner input / capture buffer while Scan Mode is active.

Key behaviors:

- capture rapid keyboard input
- Enter finalizes scan
- ignore ordinary typing when scanner mode is paused or focus is in editable controls
- normalize input
- submit token to backend
- show explicit READY / PROCESSING states
- play audio only after backend result

## State machine

Recommended client state:

- READY_NO_STUDENT
- READY_STUDENT_SELECTED
- PROCESSING
- ERROR
- PAUSED

Selected student may be held client-side for convenience, but the server must validate it on each financial action.

## Processing lock

While PROCESSING:

- scanner submission is disabled or queued
- duplicate Enter events are ignored locally
- backend idempotency still protects financial correctness

## Scanner parsing

The backend should expose a common scan-resolution endpoint:

`POST /scan/resolve`

It resolves an opaque token to a type and permitted action.

For a student token:
- returns/selects enrollment

For a Quick Action token:
- requires selected enrollment in the request/session context
- resolves configured action
- invokes financial service

The exact endpoint shape can be adjusted later.

## Focus behavior

Do not implement a global aggressive focus-stealing loop.

Instead:

- Scan Mode owns scanner capture
- scanner capture pauses automatically while an intentional editable control has focus
- resumes on blur / explicit resume
- provide a visible "Scanner Active" indicator

---

# 10. QR Token and Generation Architecture

## Token strategy

Use random high-entropy opaque tokens.

Recommended properties:

- at least 128 bits of entropy
- URL/QR-safe encoding
- no PII
- not derived from database IDs

Store either:

- raw token in database with strict access controls, or preferably
- token hash plus a short non-secret lookup prefix if efficient lookup is needed

Because QR scanning requires exact lookup, the database design should evaluate hashed-token lookup carefully.

Recommended practical design:

- store a keyed hash/HMAC of the presented token
- index the digest
- never expose stored digest to clients

## Rotation

A QR identity record should support:

- active
- revoked
- created_at
- revoked_at
- version/generation

Regeneration:

1. create new token
2. revoke prior active token(s)
3. write administrative audit event

## QR generation

Generate QR graphics on demand.

Do not store PNG/SVG blobs in PostgreSQL.

Generate:

- SVG for high-quality external design use
- PNG for convenience
- PDF for bulk printable packs

Bulk generation can create temporary files/archives server-side and stream them to the user.

Generated files need not be permanent unless later product requirements justify caching.

---

# 11. Rewards Architecture

Separate three concepts:

1. Reward definition
2. Reward purchase
3. Reward fulfillment/redemption

A reward definition belongs to a classroom.

A reward purchase records:

- student/enrollment
- reward reference
- price snapshot
- name snapshot
- financial transaction link
- purchase status
- actor
- timestamp

Fulfillment records whether the purchased reward has been used or delivered.

For physical inventory:

- inventory update must be inside the same transaction as purchase when inventory is enforced
- use row locking on the reward/inventory record
- do not allow stock to go below zero

Refund:

- financial effect uses the standard reversal engine
- inventory restoration must be explicit and idempotent
- fulfillment state determines whether restocking is valid

Exact refund/restock rules require product approval.

---

# 12. Behavior Architecture

Behavior reasons belong to a classroom and have:

- type: positive/negative
- active state
- description

A behavior financial action can complete first.

Optional behavior detail can be attached afterward to the transaction or a linked behavior event record.

This preserves scan speed.

Historical snapshots should preserve reason name/type used at the time.

Analytics should query structured behavior events/transactions rather than parse free text.

---

# 13. Jobs and Automation Architecture

## Jobs

Job definitions belong to a classroom.

Assignments link jobs to enrollments with start/end timestamps.

Use a uniqueness rule that prevents duplicate active assignment of the same job to the same enrollment.

## Weekly wage processing

Each assignment payment needs its own durable idempotency key derived from a logical payment period, for example conceptually:

`job_assignment_id + period_start`

Do not rely only on a single weekly run record.

This allows partial retry after a crash without duplicating successful payments.

## Weekly allowance

Likewise, each enrollment allowance payment should have its own unique logical payment key:

`enrollment_id + allowance_period`

A run-level record may still exist for observability, but per-payment uniqueness is the real correctness barrier.

## Scheduler

Recommendation for V1:

**systemd timers invoking dedicated application management commands**

Why:

- reliable on one VPS
- independent of web workers
- simple to inspect
- no risk of every FastAPI worker running a scheduler
- restart-safe
- low operational complexity

Later, if workload grows substantially, move to a queue/worker system.

---

# 14. Audit / Analytics Architecture

Maintain three distinct records:

## Financial ledger

Permanent business history of balance changes.

## Administrative audit log

Captures significant non-financial changes:

- settings
- permissions
- QR rotations
- job wage changes
- reward price changes
- student/enrollment deactivation

Audit entries should include actor, action type, target type/id, timestamp, and safe before/after metadata where useful.

## Technical logs

Used for debugging/operations.

Do not store secrets or unnecessary student data.

## Analytics

Use PostgreSQL queries/views initially.

Do not introduce a data warehouse in V1.

CSV export should run through authorized service-layer queries.

Potential future materialized views may accelerate analytics if needed.

---

# 15. Security and Privacy Considerations

Primary risks:

- broken object-level authorization
- tenant data leakage
- duplicate financial requests
- session theft
- CSRF
- XSS
- SQL injection
- brute-force login
- reset-token abuse
- QR-token guessing
- insecure reward-image upload
- secrets in source control
- unsafe backups

Mitigations:

- service-layer authorization on every resource
- parameterized ORM/database queries
- strict input validation
- secure cookies and CSRF tokens
- Content Security Policy
- output escaping
- login rate limiting
- hashed verification/reset tokens
- high-entropy QR tokens
- size/type validation for uploads
- store uploaded images outside executable/static source directories
- environment-managed secrets
- encrypted/offsite backup strategy where practical

Because QRBank may become a commercial product handling student-related information, a formal privacy/legal review should happen before broad public launch, particularly regarding child privacy, school contracts, retention/deletion policy, analytics, and any future advertising.

---

# 16. Deployment Architecture

Initial VPS layout:

```text
Internet
   |
Nginx :443
   |
FastAPI app (localhost)
   |
PostgreSQL (localhost/private socket)

systemd timers
   |
QRBank management commands
   |
same domain services + PostgreSQL
```

Recommended operational rules:

- PostgreSQL not exposed publicly
- SSH key authentication only
- firewall allows only SSH/HTTP/HTTPS
- app runs as non-root system user
- secrets in root-readable or service-readable environment file outside repo
- systemd Restart=on-failure
- structured logs through journald
- nightly database backup
- periodic off-server backup copy
- migration backup before release
- health endpoint for local monitoring

Use one app instance initially unless load testing justifies more.

If multiple web workers are used, database locking/idempotency must remain the correctness mechanism.

---

# 17. Testing Strategy

## Unit tests

Test:

- QR parsing/normalization
- policy decisions
- amount validation
- behavior type matching
- entitlement checks

## PostgreSQL integration tests

Use a real PostgreSQL test database for:

- balance updates
- ledger constraints
- reversals
- idempotency
- foreign keys
- reward inventory
- automation uniqueness

SQLite is not sufficient for these correctness tests.

## Concurrency tests

Use parallel transactions/processes to test:

- two simultaneous deductions
- two simultaneous deposits
- reward stock race
- manual transaction vs scheduler
- reversal races

## Authorization tests

Attempt cross-tenant access for every major resource type.

## Browser tests

Use Playwright for:

- Scan Mode keyboard capture
- Enter handling
- processing lock
- focus pause/resume
- audio trigger state
- responsive workflows

Hardware scanner testing still requires a human acceptance test because keyboard-emulating scanners vary.

---

# 18. Recommended Repository Structure

```text
qrbank/
├── README.md
├── SPEC.md
├── ARCHITECTURE.md
├── ARCHITECTURE_PROMPT.md
├── docs/
│   ├── QR_SCANNING.md
│   ├── FINANCIAL_RULES.md
│   ├── SECURITY.md
│   ├── AUTOMATION.md
│   └── ACCEPTANCE_TESTS.md
├── backend/
│   └── qrbank/
│       ├── auth/
│       ├── classrooms/
│       ├── students/
│       ├── finance/
│       ├── scanner/
│       ├── qr/
│       ├── behavior/
│       ├── jobs/
│       ├── rewards/
│       ├── automation/
│       ├── analytics/
│       └── audit/
├── templates/
├── static/
│   ├── css/
│   ├── js/
│   └── audio/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── concurrency/
│   └── browser/
├── scripts/
└── deployment/
    ├── nginx/
    └── systemd/
```

Keep modules domain-oriented rather than grouping the entire app into generic `models.py`, `services.py`, and `routes.py` files that become difficult to maintain.

---

# 19. Major Risks and Mitigations

## Critical — Financial corruption

Risk:
Concurrent operations or duplicated requests produce incorrect balances.

Mitigation:
Central financial service, row locks, idempotency constraints, PostgreSQL integration/concurrency tests.

## Critical — Cross-tenant data leakage

Risk:
Teacher accesses another teacher's classroom by changing an ID.

Mitigation:
Membership-based authorization in every service/query, systematic cross-tenant tests, optional future PostgreSQL RLS.

## Critical — Scheduled duplicate payments

Risk:
Retries or multiple workers pay allowance/wages twice.

Mitigation:
Per-payment unique period keys, systemd timer isolation, central financial service.

## High — QR scanner reliability

Risk:
Focus problems or repeated Enter events create missed/duplicated actions.

Mitigation:
Dedicated Scan Mode state machine, no aggressive global focus stealing, server idempotency, browser/hardware acceptance tests.

## High — Incorrect reversal semantics

Risk:
Simple reversed flag prevents reversal-of-reversal or creates inconsistent chains.

Mitigation:
Explicit reversal relationships and database constraints designed in schema review.

## High — Reward inventory races

Risk:
Two purchases consume the last item.

Mitigation:
Row lock inventory/reward row inside same purchase transaction.

## Medium — Premature SaaS complexity

Risk:
Future subscriptions/roles make V1 unnecessarily difficult.

Mitigation:
Provide clean extension points but do not implement full billing, parent/student portals, or school hierarchy yet.

## Medium — Frontend overengineering

Risk:
Large SPA introduces state duplication and slows development.

Mitigation:
SSR + progressive JavaScript initially.

---

# 20. DECISIONS REQUIRING APPROVAL

The following should be approved before detailed database schema design:

1. **Frontend approach:** server-rendered Jinja2 + progressive JavaScript rather than a React/Vue SPA for V1.
2. **Authentication:** server-side sessions with secure cookies rather than browser-stored JWT authentication.
3. **Balance ownership:** classroom balance belongs to the student's classroom enrollment, not the global student record.
4. **User/classroom relationship:** use a membership model from day one, even if V1 exposes only one classroom owner.
5. **QR payloads:** use opaque high-entropy tokens rather than `STUDENT:01` and `QUICK:QA1` in the new production system.
6. **QR storage:** generate QR PNG/SVG/PDF on demand rather than storing image blobs in PostgreSQL.
7. **Scheduler:** use systemd timers calling application management commands for V1 rather than embedding APScheduler inside web workers or introducing Celery.
8. **Financial balance cache:** retain a stored current enrollment balance for performance while treating the ledger as the immutable financial audit trail.
9. **Reward inventory:** decide whether V1 inventory is strict stock enforcement or optional informational inventory.
10. **Reward refund semantics:** decide whether reversing a physical reward purchase automatically restores inventory, requires explicit teacher confirmation, or depends on fulfillment state.
11. **Student identity scope:** confirm that a Student is independent from Enrollment even though most V1 teachers may only use students within one classroom/year.
12. **PostgreSQL RLS:** recommend service-layer authorization for V1 and revisit RLS as defense-in-depth after the schema and authorization tests are stable.

No production code or migrations should begin until these architectural choices are reviewed.
