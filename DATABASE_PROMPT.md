# QRBank Database Design Task

You are acting as the lead data architect for QRBank.

Before doing anything else, read these repository files completely:

1. `SPEC.md`
2. `ARCHITECTURE.md`

Treat both as authoritative.

Your task at this stage is **database design only**.

Do not create migrations.

Do not generate SQLAlchemy models.

Do not write production code.

Do not create Alembic revisions.

Do not silently change approved architecture decisions.

---

# Objective

Design a normalized PostgreSQL schema for QRBank that satisfies the approved product specification and architecture.

The schema must support the required V1 features while leaving reasonable extension points for:

- multiple teachers per classroom
- assistants and school administrators
- student accounts
- parent accounts
- future subscriptions/entitlements
- richer analytics
- future APIs

Financial correctness, tenant isolation, auditability, concurrency safety, and historical preservation are the highest priorities.

---

# 1. Start With Requirements Review

Before proposing tables:

1. Read `SPEC.md`.
2. Read `ARCHITECTURE.md`.
3. Identify the database-relevant requirements.
4. Identify any remaining ambiguities that materially affect schema design.
5. Do not invent major product behavior where clarification is required.

If there is a serious unresolved design question, surface it before finalizing the schema.

---

# 2. Approved Architecture Decisions You Must Preserve

The following are already approved:

1. PostgreSQL is the production database.
2. FastAPI will be the backend.
3. V1 uses server-rendered Jinja2 + progressive JavaScript.
4. Browser authentication uses server-side sessions with secure cookies.
5. A Student is separate from an Enrollment.
6. The classroom balance belongs to the Enrollment, not the global Student.
7. Users relate to classrooms through a ClassroomMembership model.
8. V1 may expose only Owner permissions, but the schema must support future classroom roles.
9. QR codes use generic classroom-contextual commands:
   - `STUDENT:<StudentID>`
   - `QUICK:<QuickActionID>`
   - `ACTION:<ActionID>`
10. QR commands are interpreted within the currently active classroom.
11. QR images are generated on demand rather than stored in PostgreSQL.
12. Scheduled automation uses systemd timers calling QRBank management commands.
13. The system stores the current Enrollment balance for fast access.
14. Only the central financial transaction service may modify that stored balance.
15. The financial ledger is the immutable audit history.
16. Reward inventory tracking is optional per reward.
17. When inventory tracking is enabled, inventory is strictly enforced and cannot go below zero.
18. Reward financial reversal and inventory restoration are separate decisions.
19. PostgreSQL Row Level Security is not required in V1.
20. V1 tenant isolation is enforced through strict service-layer authorization and database relationships.

Do not contradict these decisions.

---

# 3. Identifier Strategy

Propose an identifier strategy.

Evaluate appropriate use of:

- UUID primary keys
- teacher-facing human-readable IDs
- database-generated sequence/display transaction IDs

Requirements:

- internal relationships must not depend on StudentID values such as `01`
- teacher-facing IDs may be reused in different classrooms
- QuickActionID values such as `QA1` may be reused across classrooms
- ActionID values such as `A01` may be reused across classrooms
- internal IDs should remain stable if display identifiers change

Explain your recommendation.

---

# 4. User and Authentication Data

Design the schema concepts needed for:

- user accounts
- email address
- email verification
- secure password hash
- account status
- creation/update timestamps
- password reset tokens
- email verification tokens
- server-side sessions

Teacher verification must be separate from email verification.

The schema should support a future teacher-verification process without requiring redesign.

Do not store plaintext verification/reset tokens if hashing is more appropriate.

Explain token expiration and uniqueness constraints.

---

# 5. Classroom Membership and Roles

Design the relationship among:

- User
- Classroom
- ClassroomMembership

The schema must support:

- one user owning multiple classrooms
- one classroom eventually having multiple adults
- future roles such as Owner, Teacher, Assistant, School Administrator

V1 may only expose Owner.

Explain:

- role representation
- active/inactive membership
- ownership handling
- uniqueness constraints
- safe ownership transfer possibilities

Do not rely solely on `classrooms.owner_user_id` if that would undermine the approved membership model.

---

# 6. Classroom and Settings

Design the classroom and settings model.

Support concepts including:

- classroom name
- school year
- archive state
- timezone
- starting balance
- weekly allowance
- weekly allowance enabled
- allowance schedule
- weekly wage automation enabled
- sound enabled
- duplicate scan interval
- negative balance policy
- student-selection timeout
- confirmation settings

Discuss whether settings should be:

- columns on Classroom
- a separate one-to-one ClassroomSettings table
- another normalized approach

Explain the tradeoffs.

---

# 7. Student and Enrollment Model

Design separate Student and Enrollment concepts.

Support:

## Student

- stable internal identity
- minimal optional profile information
- future link to authenticated student account
- future parent relationships

## Enrollment

- classroom
- student
- teacher-facing StudentID such as `01`
- current integer balance
- active/inactive/archive state
- notes
- enrollment/start/end dates where useful

The teacher-facing StudentID must be unique within the appropriate classroom scope, not globally.

Explain how school-year rollover and archived classrooms preserve history.

---

# 8. Financial Ledger

This is critical.

Design the transaction/ledger model.

Every balance-changing event must be recorded.

The ledger must support:

- immutable historical records
- enrollment
- classroom
- signed integer amount
- balance before
- balance after
- transaction type/category
- source
- timestamp
- actor/user
- Action reference
- Quick Action reference
- Behavior reason reference
- Job reference
- Reward purchase reference
- note
- reversal relationship
- idempotency key
- historical snapshot fields
- automatic-payment metadata where needed

Exact nullable relationships should be justified.

Do not design a ledger that depends entirely on mutable foreign records for historical display.

---

# 9. Transaction IDs

The UI may display transaction identifiers similar to:

`TX000001`

Design a safe way to provide human-readable transaction IDs without using them as the primary key.

Explain whether this should use:

- PostgreSQL sequence
- numeric display number + formatted UI prefix
- another mechanism

The design must be concurrency-safe and unique.

---

# 10. Reversal Model

Design reversals so that:

- original transaction remains
- reversal is a new transaction
- reversal references the transaction it reverses
- a reversal itself can be reversed
- full chains remain auditable

Avoid a simplistic design where a permanent `is_reversed` flag becomes the only source of truth.

Explain constraints that prevent obviously invalid reversal relationships while still allowing reversal-of-reversal.

---

# 11. Idempotency

Design server-side idempotency storage.

Financial retries must not accidentally create duplicate transactions.

Consider whether idempotency belongs:

- directly on ledger rows
- in a dedicated idempotency/request table
- in both

Discuss:

- uniqueness scope
- request status
- result replay
- partial failure
- cleanup/retention

Scheduled payment idempotency requires special attention.

---

# 12. Actions

Design classroom-specific Actions.

Support:

- classroom
- ActionID such as `A01`
- name
- category
- integer amount
- active/inactive
- description

ActionID may be reused in different classrooms.

Historical ledger entries must preserve action name/category/amount as they existed at transaction time.

---

# 13. Quick Actions

Design Quick Actions.

Support:

- classroom
- QuickActionID such as `QA1`
- current assigned Action
- active/inactive
- display order

QuickActionID may be reused across classrooms.

A Quick Action QR is:

`QUICK:<QuickActionID>`

Changing the mapping must not require changing the QR code.

Explain the necessary uniqueness constraints.

---

# 14. Direct Action Scanning

The system supports:

`ACTION:<ActionID>`

The schema must permit efficient lookup of an active Action within the active classroom.

No separate QR-token table is required for V1.

Explain the required indexes for:

- `STUDENT:<StudentID>`
- `QUICK:<QuickActionID>`
- `ACTION:<ActionID>`

---

# 15. Behavior Model

Design behavior reason/event storage.

Support:

- classroom-specific reasons
- positive/negative type
- description
- active/inactive

A fast behavior financial transaction may complete before detailed reason attachment.

Propose how optional behavior detail should relate to the ledger.

Explain whether behavior should be:

- additional fields on the financial transaction
- a linked BehaviorEvent table
- another design

The design must support analytics and historical reason snapshots.

---

# 16. Jobs

Design:

- Job
- JobAssignment

Support:

## Job
- classroom
- classroom-facing JobID
- name
- integer weekly wage
- active/inactive
- notes

## JobAssignment
- enrollment
- job
- start date
- end date
- active/history state

A student may have multiple jobs.

Prevent accidental duplicate active assignment of the same job to the same enrollment.

Explain the PostgreSQL constraint strategy.

---

# 17. Weekly Job Payments

Each weekly payment must be individually idempotent.

A crash halfway through a class payroll must allow the job to be retried without duplicating already completed payments.

Propose schema support for a unique logical payment key based on concepts such as:

- job assignment
- payment period

A high-level AutomationRun record may also exist for observability.

Explain the relationship between run records and per-payment uniqueness.

---

# 18. Weekly Allowance Payments

Each student's allowance payment must also be individually idempotent.

Propose a uniqueness strategy based on:

- enrollment
- allowance period

A failed partial run must be safely retryable.

---

# 19. Automation Runs

Design an AutomationRun or equivalent observability model.

Support:

- classroom
- run type
- period
- started
- completed
- failed
- timestamps
- diagnostic information

Do not make this run-level table the only protection against duplicate student payments.

---

# 20. Rewards

Design Reward entities.

Support:

- classroom
- classroom-facing RewardID if useful
- name
- description
- integer price
- active/inactive
- reward type
- optional image metadata/reference
- inventory tracking enabled/disabled
- optional available quantity
- future availability dates
- future purchase limits

Explain appropriate check constraints, especially around inventory.

---

# 21. Reward Purchases

Design RewardPurchase separately from the ledger transaction.

Support:

- enrollment
- reward
- price snapshot
- reward name snapshot
- purchase transaction
- fulfillment/redemption state
- purchase timestamp
- actor
- relevant inventory outcome

The financial deduction must occur through the standard ledger/financial service.

Explain why RewardPurchase and Transaction should remain separate linked concepts.

---

# 22. Reward Fulfillment

Design a model for reward fulfillment/redemption.

Possible states include:

- unused
- redeemed
- cancelled

Avoid mixing physical-delivery state with the immutable financial ledger.

Explain whether fulfillment state belongs directly on RewardPurchase or in a separate event/history table.

Prefer a design that can preserve fulfillment history if useful.

---

# 23. Reward Refund and Inventory Restoration

Financial refund must use normal transaction reversal.

Inventory restoration is a separate explicit teacher decision.

The default user experience will generally be:

- unused/unfulfilled reward -> restore stock by default
- already redeemed/fulfilled reward -> do not restore stock by default

Design schema support for recording whether inventory was restored during a reversal.

The operation must remain concurrency-safe and idempotent.

---

# 24. Inventory Concurrency

When inventory tracking is enabled:

- quantity cannot go below 0
- two simultaneous purchases of the last item must not both succeed

Propose database constraints and locking behavior that support this.

Do not rely on browser-side checks.

---

# 25. Administrative Audit Log

Design a separate administrative audit log.

It should support significant non-financial changes such as:

- settings changes
- reward price changes
- job wage changes
- role/membership changes
- student deactivation
- Action/Quick Action edits
- QR-related identifier changes where relevant

Support:

- actor
- classroom when applicable
- target entity type
- target entity ID
- action/event type
- timestamp
- safe before/after metadata

Do not use the financial ledger for these events.

---

# 26. Technical Logs

Technical application logs do not need to live in the main relational schema unless there is a strong reason.

Explain what operational logs should remain in system logging rather than PostgreSQL business tables.

---

# 27. Integrity Audits

The schema must make it possible to detect:

- stored Enrollment balance vs ledger-derived balance mismatch
- duplicate scheduled payments
- orphaned records
- invalid reversal relationships
- duplicate active job assignments
- impossible reward inventory values

Explain which constraints prevent these issues and which require periodic integrity queries.

---

# 28. Soft Deletion / Archival

Explain which entities should use:

- `active`
- `archived_at`
- `deleted_at`
- no deletion at all

Financial transactions must not be normally deletable.

Historical references must remain valid.

Do not add soft-delete columns everywhere without justification.

---

# 29. Historical Snapshots

Identify which mutable values should be copied into ledger/purchase history.

Consider:

- Action name
- Action amount
- Action category
- Quick Action ID
- Behavior reason name/type
- Job ID/name/wage
- Reward ID/name/price
- Student-facing ID
- classroom name/school-year snapshot where appropriate

Explain which values should be snapshots versus only foreign-key relationships.

Avoid unnecessary duplication while preserving correct historical interpretation.

---

# 30. Indexing Strategy

Propose indexes for common operations including:

- login by email
- active classroom memberships
- classroom student list
- QR command resolution:
  - classroom + StudentID
  - classroom + QuickActionID
  - classroom + ActionID
- transaction history by enrollment/date
- transaction history by classroom/date
- reversals
- idempotency keys
- active jobs/assignments
- behavior analytics
- rewards
- scheduled payments
- audit log

Explain any partial indexes or composite indexes that are especially important.

---

# 31. Constraints

Explicitly identify database constraints that should enforce correctness.

Examples to evaluate:

- integer balance semantics
- unique teacher-facing StudentID per classroom
- unique ActionID per classroom
- unique QuickActionID per classroom
- unique active job assignment
- reward quantity nonnegative
- unique scheduled-payment period
- unique idempotency key
- membership uniqueness
- reversal sanity checks

Do not rely solely on application code where PostgreSQL can safely enforce an invariant.

---

# 32. UUIDs and Public IDs

Recommend which entities should use UUID primary keys.

If UUIDv7, UUIDv4, PostgreSQL-generated UUIDs, or another approach is recommended, explain why.

The design should avoid exposing predictable sequential database IDs as the sole protection against unauthorized access.

Authorization remains mandatory regardless of identifier type.

---

# 33. Future Student Accounts

Explain how a future authenticated User can be linked to an existing Student without migrating historical financial records.

Do not require student accounts in V1.

---

# 34. Future Parent Accounts

Explain how future parent/guardian relationships could be added cleanly.

Do not fully design a parent portal now.

Only ensure the Student model does not block this future extension.

---

# 35. Future Subscription / Entitlement Data

Do not implement billing now.

Explain where future:

- subscription plans
- account subscriptions
- feature entitlements
- school plans

could fit without altering the classroom financial model.

Avoid creating unnecessary billing tables in V1 unless required for clean extension.

---

# 36. Multi-Tenant Isolation

Explain how the database structure supports strict service-layer tenant isolation.

Every classroom-owned record should have a clear authorization path back to a Classroom and its memberships.

Identify any tables where directly storing `classroom_id` in addition to an indirect relationship would improve:

- query safety
- performance
- constraint enforcement

but avoid unjustified denormalization.

---

# 37. Data Types

Recommend PostgreSQL data types for important fields including:

- monetary/point amounts
- timestamps
- timezone
- emails
- statuses
- JSON audit metadata
- IDs
- school year
- notes

Points must use integer types, never float.

Use timezone-aware timestamps.

---

# 38. Database Transaction Boundaries

Explain the database rows that should be locked for:

- manual financial transaction
- QR Action
- reward purchase
- reward reversal/refund
- weekly allowance
- weekly job payment
- balance adjustment

Highlight expected lock ordering if multiple rows must be locked to reduce deadlock risk.

---

# 39. Migration Readiness

The schema will eventually be implemented with Alembic.

Do not create migration code now.

Identify schema decisions that are particularly expensive to change later and deserve extra scrutiny before migration implementation.

---

# 40. Recommended Schema Diagram

Provide a readable ER-style text diagram or Mermaid ER diagram showing the main relationships.

The diagram should be understandable without reading every proposed field.

---

# 41. Table-by-Table Proposal

Provide a table-by-table schema proposal.

For each table include:

- purpose
- important columns
- primary key
- foreign keys
- unique constraints
- check constraints
- important indexes
- deletion/archive behavior

Do not output full executable migration SQL yet.

Pseudotype notation is acceptable.

---

# 42. Important Query Examples

Provide example conceptual queries/use cases showing that the schema supports:

1. resolve `STUDENT:01` in active classroom
2. resolve `QUICK:QA1`
3. resolve `ACTION:A01`
4. obtain a student's current balance
5. show student transaction history
6. derive expected balance from ledger
7. pay weekly allowance idempotently
8. pay multiple jobs idempotently
9. purchase the last inventory item safely
10. reverse a transaction
11. reverse a reversal
12. list behavior analytics
13. archive classroom without losing history
14. prove Teacher A cannot access Classroom B through legitimate query paths

Do not provide application code; demonstrate the relational design.

---

# 43. Risks and Tradeoffs

Identify database design risks such as:

- over-normalization
- excessive snapshot duplication
- reversal-chain complexity
- idempotency lifecycle complexity
- duplicated classroom_id values
- student identity ambiguity
- reward inventory race conditions
- future student account linkage

Explain mitigations.

---

# 44. Questions Requiring Human Approval

End with:

`DATABASE DECISIONS REQUIRING APPROVAL`

Only include genuinely important unresolved schema decisions.

Do not manufacture questions merely to create a list.

Examples might include:

- whether starting balance is represented as an opening ledger transaction
- whether fulfillment state needs full history or only current state
- whether human-readable IDs can be changed after transactions exist
- whether Student records can be shared across classrooms owned by unrelated teachers

These decisions must be reviewed before migration implementation.

---

# Required Output

Produce a database design document with these sections:

1. Executive Summary
2. Requirements and Approved Architecture Constraints
3. Identifier Strategy
4. ER Diagram
5. Proposed Tables
6. Financial Ledger Design
7. Reversal Design
8. Idempotency Design
9. Authentication / Sessions / Verification Data
10. Classroom Membership / Roles
11. Student / Enrollment Model
12. Actions / Quick Actions / QR Resolution
13. Behavior Model
14. Jobs / Job Assignments / Payroll
15. Rewards / Purchases / Inventory / Fulfillment
16. Automation / Scheduled Payment Idempotency
17. Audit Logging
18. Historical Snapshot Strategy
19. Constraints
20. Indexing Strategy
21. Transaction and Locking Strategy
22. Multi-Tenant Isolation
23. Future Extension Compatibility
24. Integrity Audit Support
25. Important Query Walkthroughs
26. Risks and Tradeoffs
27. Database Decisions Requiring Approval

Do not create migrations.

Do not create SQLAlchemy models.

Do not write production code.

The objective is to produce a schema proposal that can be reviewed and approved before implementation.
