# QRBank Architecture Design Task

You are acting as the lead software architect for QRBank.

Before doing anything else, read the complete `SPEC.md` in this repository.

The specification is authoritative for product behavior.

Your task at this stage is **architecture design only**.

Do not build the application yet.

Do not create database migrations yet.

Do not generate a large implementation scaffold merely to demonstrate the architecture.

Do not silently simplify requirements.

---

## Objective

Design a production-ready architecture for QRBank that satisfies the current requirements in `SPEC.md` while leaving reasonable extension points for the explicitly identified future capabilities.

QRBank is expected to become a multi-tenant web application serving independent teachers, multiple classrooms per teacher, potentially multiple adults per classroom, and possibly student/parent accounts later.

Financial correctness, multi-tenant security, QR scanning reliability, and auditability are the highest priorities.

---

# 1. Review the Requirements First

Before proposing architecture:

1. Read `SPEC.md` completely.
2. Identify requirements that materially affect architecture.
3. Identify any contradictions, ambiguities, or important unanswered questions.
4. Do not invent missing product behavior where clarification is necessary.
5. Distinguish:
   - current required functionality
   - architect-for-later functionality
   - optional future functionality

If there is a serious architectural ambiguity, call it out explicitly rather than silently choosing an interpretation.

---

# 2. Proposed Technology Stack

Evaluate and recommend the technical stack.

Current preferred direction:

- Python
- FastAPI
- PostgreSQL
- HTML/CSS/JavaScript web frontend
- Linux deployment
- Nginx
- HTTPS

Do not blindly accept these choices if an alternative would materially improve the project.

However, do not replace them merely because another framework is fashionable.

Discuss the tradeoffs of your recommendation.

For the frontend specifically, evaluate whether QRBank should use:

- server-rendered HTML with progressive JavaScript
- a lightweight frontend framework
- a larger SPA framework
- another approach

Take into account:

- USB QR scanner behavior
- fast UI updates
- mobile camera scanning
- maintainability
- accessibility
- AI-assisted development
- deployment complexity

---

# 3. Overall System Architecture

Propose the main application architecture.

Explain major layers/components such as:

- web frontend
- API/backend
- authentication
- authorization
- classroom domain logic
- financial transaction engine
- scanner subsystem
- QR generation
- rewards
- behavior
- jobs
- scheduled automation
- reporting/analytics
- database
- background jobs
- audit logging

Explain which responsibilities belong in each layer.

Avoid duplicated business logic.

---

# 4. Central Financial Transaction Architecture

This section is critical.

Design one authoritative mechanism through which all balance-changing operations pass.

This includes:

- QR Quick Actions
- manual deposits
- manual deductions
- balance adjustments
- reward purchases
- reward refunds/reversals
- weekly allowance
- weekly wages
- bulk transactions
- future student-initiated purchases

Explain:

- transaction boundaries
- row locking
- authoritative balance lookup
- validation
- ledger insertion
- balance update
- idempotency
- rollback behavior
- concurrency handling
- error propagation

No feature may independently modify balances outside this architecture.

Include pseudocode or a sequence diagram if helpful, but do not implement production code yet.

---

# 5. Multi-Tenant Architecture

Explain how teacher/classroom data will be isolated.

The design must prevent one tenant from accessing another tenant's resources through:

- modified URLs
- manipulated API requests
- guessed identifiers
- frontend bypasses

Discuss:

- ownership
- classroom membership
- future co-teachers
- permissions
- authorization checks
- query scoping
- internal identifiers

Explain where authorization should occur.

Do not rely solely on frontend protections.

---

# 6. Authentication Architecture

Propose authentication for teacher accounts.

Initial requirements include:

- registration
- email verification
- login
- logout
- password reset
- secure password storage
- account status

Future architecture should accommodate:

- student accounts
- parent accounts
- assistants
- school administrators

Explain whether you recommend:

- server sessions
- JWT
- another established approach

Explain why.

Include:

- cookie security
- CSRF considerations
- brute-force protection
- email verification tokens
- password-reset tokens

Do not implement authentication yet.

---

# 7. Teacher Verification Architecture

QRBank may later distinguish between:

- email verified
- teacher verified

Design this as an extensible concept.

Do not make school-domain email the sole permanent proof of teacher status.

Explain how future verification methods could be added without redesigning the user account system.

No complex teacher credential system needs to be implemented in V1.

---

# 8. Classroom Architecture

Explain how the architecture supports:

- one teacher with multiple classrooms
- archived classrooms
- school-year rollover
- future multiple teachers per classroom
- classroom-specific settings

Classroom data must not be globally shared accidentally.

---

# 9. Student and Enrollment Architecture

Evaluate the requirement that:

- a student is an independent concept
- classroom enrollment/membership is a separate concept

Explain how this helps with:

- multiple classrooms
- school years
- transfers
- future student accounts
- historical preservation

Do not lock the product to teacher-facing IDs such as `01`.

Internal relationships should use stable immutable identifiers.

---

# 10. QR Scanning Architecture

QR scanning is a primary interface.

Design the scanner subsystem for:

- USB keyboard-emulating QR scanners
- automatic Enter/carriage return
- fast repeated scanning
- selected student state
- Quick Action scans
- malformed input handling
- processing lock
- duplicate scan timing protection
- application-level audio feedback
- automatic scanner readiness
- avoiding focus theft while forms are being edited

Explain how scanner input should reach the backend.

Explain how financial scans use the central financial transaction architecture.

The scanner subsystem must not contain its own independent balance-changing logic.

---

# 11. Mobile QR Scanning

Explain how future/current mobile camera QR scanning should work.

Desktop USB scanning and mobile camera scanning must use the same server-side scan/action processing logic wherever possible.

Do not create duplicate financial implementations.

---

# 12. QR Command Protocol Architecture

QRBank V1 uses generic, human-readable, classroom-contextual QR commands.

Required formats:

- `STUDENT:<StudentID>`
- `QUICK:<QuickActionID>`
- `ACTION:<ActionID>`

Requirements:

- the teacher must have an active classroom selected before scanning
- IDs are resolved only within that authenticated classroom context
- STUDENT selects the matching active enrollment
- QUICK resolves that classroom's Quick Action mapping
- ACTION directly resolves a configured Action
- QUICK and ACTION require a selected student
- QR commands are not authentication credentials
- do not encode student names or sensitive personal data
- physical student cards should print a classroom identifier separately from the QR
- Scan Mode must make the active classroom prominent and stable
- inactive/unknown identifiers must be rejected
- no opaque-token generation/rotation subsystem is required for V1

Discuss the safety implications of generic commands and how UI, authorization, validation, and scanner state reduce wrong-classroom and malformed-scan risk.

The exact database implementation will be reviewed separately.

---

# 13. QR Generation Architecture

Explain how QRBank should generate:

- individual student QR codes
- individual Quick Action QR codes
- individual direct Action QR codes
- PNG
- SVG
- bulk QR packages
- printable PDFs
- student cards
- action cards
- US Letter
- A4

QR generation should occur locally within QRBank rather than through unnecessary external QR-generation services.

Discuss whether images should be generated on demand, cached, or stored.

Do not store large collections of redundant QR images in PostgreSQL without a strong reason.

---

# 14. Reward Store Architecture

Design the reward subsystem.

Support:

- classroom-specific rewards
- price
- active/inactive state
- reward type
- optional inventory
- optional images
- future purchase limits
- future availability dates
- reward purchasing from student page
- dedicated store page
- refunds through standard financial reversal logic
- future student self-service purchasing

Distinguish:

- financial purchase
- reward fulfillment/redemption

Explain how physical rewards and privileges can both fit the model.

---

# 15. Behavior Architecture

Explain the structure for:

- behavior rewards
- behavior deductions
- optional behavior reasons
- positive/negative classifications
- historical snapshots
- analytics

Behavior reason entry must not block the rapid QR transaction workflow.

---

# 16. Jobs Architecture

Design for:

- classroom jobs
- configurable wages
- active/inactive jobs
- multiple jobs per student
- job assignment history
- start/end dates
- weekly wage processing

Prevent accidental duplicate active assignment where appropriate.

---

# 17. Scheduled Automation Architecture

Design server-side scheduling for:

- weekly allowance
- weekly job wages

The browser must not need to be open.

Discuss suitable scheduling approaches for an initial single-VPS deployment.

Avoid unnecessary distributed complexity.

Important requirements:

- idempotency
- duplicate-run prevention
- partial failure recovery
- crash recovery
- daylight-saving-aware scheduling
- multiple application worker safety

Explain how scheduled operations invoke the same financial transaction engine used by manual activity.

---

# 18. Audit Architecture

Separate:

1. financial ledger
2. administrative audit log
3. technical application logs

Explain what belongs in each.

Administrative audit examples:

- QR regeneration
- reward price change
- job wage change
- classroom setting change
- permission change
- student archive/deactivation

Do not confuse application logs with permanent financial history.

---

# 19. Analytics Architecture

Explain how the architecture supports:

- current balances
- transaction history
- behavior analytics
- job history
- reward purchase history
- CSV exports
- future richer reporting

Avoid premature data-warehouse complexity.

The PostgreSQL model should be usable for common reporting.

---

# 20. Monetization Readiness

Do not implement billing yet.

Propose an architecture that could later support:

- Free
- Pro
- School

Feature access should eventually use a centralized entitlement/capability model.

Avoid scattering subscription-plan checks throughout the application.

Explain where this concept would fit later.

---

# 21. Future Student and Parent Accounts

Do not implement these now unless explicitly requested.

Explain how the proposed authentication/user model could later link:

- an authenticated user
- an existing student identity
- one or more parent/guardian relationships

This must not require rebuilding historical financial data.

---

# 22. Security Review

Identify major security risks and mitigations.

At minimum consider:

- multi-tenant data leakage
- broken object-level authorization
- session theft
- CSRF
- XSS
- SQL injection
- brute-force login attempts
- password-reset abuse
- forged or malformed QR commands
- wrong-classroom scanning with generic QR identifiers
- duplicate financial requests
- privilege escalation
- leaked secrets
- insecure file upload if reward images are supported

---

# 23. Privacy Review

QRBank may contain student-related information.

Propose privacy-conscious architectural practices such as:

- data minimization
- limited QR payload information
- sensible retention
- tenant isolation
- secure backups
- audit access
- optional use of student aliases/IDs rather than full names

Do not provide legal conclusions.

Identify areas where formal legal/privacy review may eventually be needed before broad commercial deployment.

---

# 24. Background Jobs and Worker Model

Explain whether the architecture needs a separate background worker.

Evaluate the tradeoffs of options such as:

- systemd timers
- cron-driven management commands
- APScheduler
- a task queue
- another approach

Favor the simplest architecture that still provides reliable scheduled financial activity.

---

# 25. Deployment Architecture

Propose an initial Linux VPS deployment.

Current likely components:

- Ubuntu/Debian
- Nginx
- FastAPI
- PostgreSQL
- HTTPS
- systemd
- environment-based secrets/configuration

Explain:

- application process model
- database placement
- backup strategy
- log handling
- TLS
- firewall exposure
- service restart behavior

Do not deploy anything yet.

---

# 26. Database Design Principles

Do not produce final migrations yet.

At this stage, identify database design principles required by the application.

These should include:

- PostgreSQL
- integer point balances
- stable immutable internal identifiers
- foreign-key integrity
- appropriate uniqueness constraints
- transaction ledger durability
- tenant isolation
- historical snapshots
- idempotency
- soft deletion/archive where appropriate
- versioned migrations
- indexes for important access patterns

A detailed schema proposal will be reviewed in the next stage.

---

# 27. Testing Strategy

Propose a layered testing strategy.

Include:

- unit tests
- database integration tests
- concurrency tests
- authorization tests
- scanner parsing tests
- financial transaction tests
- idempotency tests
- scheduled-job tests
- browser/UI tests where useful

Explain how to test scenarios such as:

- simultaneous deductions
- duplicate requests
- rollback on database failure
- cross-tenant access attempts
- reversal of reversal
- weekly job retry
- reward purchase retry

Financial correctness must be demonstrated by tests, not assumed.

---

# 28. Recommended Repository Structure

Propose a repository structure for the project.

For example, consider areas such as:

```text
backend/
frontend/
tests/
migrations/
docs/
deployment/
scripts/
```

You may recommend another structure if there is a clear reason.

Explain the important directories rather than generating large amounts of placeholder code.

---

# 29. Architecture Risks

Identify the most important implementation risks.

Rank them by severity.

Pay particular attention to:

- financial corruption
- duplicate transactions
- multi-tenant leaks
- QR scan reliability
- scheduler duplication
- overly complicated frontend architecture
- premature SaaS complexity
- database designs that make future student accounts difficult

For each major risk, propose mitigation.

---

# 30. Architecture Decisions That Need Human Approval

End the proposal with a concise list titled:

```text
DECISIONS REQUIRING APPROVAL
```

Include any decisions that should be explicitly approved before database/schema design begins.

Examples might include:

- frontend architecture
- authentication strategy
- internal student/enrollment model
- QR command protocol
- scheduling mechanism
- deployment process
- reward inventory behavior

Do not silently decide high-impact product questions merely to finish the document.

---

# Required Output

Produce an architecture document containing these sections:

1. Executive Summary
2. Requirements Observed
3. Proposed Technology Stack
4. Overall Architecture
5. Financial Transaction Architecture
6. Authentication and Authorization
7. Multi-Tenant Model
8. Classroom / Student / Enrollment Model
9. QR Scanning Architecture
10. QR Token and Generation Architecture
11. Rewards Architecture
12. Behavior Architecture
13. Jobs and Automation Architecture
14. Audit / Analytics Architecture
15. Security and Privacy Considerations
16. Deployment Architecture
17. Testing Strategy
18. Recommended Repository Structure
19. Major Risks and Mitigations
20. Decisions Requiring Approval

Do not implement the application.

Do not create migrations.

Do not create production code.

The objective is to produce an architecture proposal that can be reviewed before development begins.
