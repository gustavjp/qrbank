# QRBank Master Specification

**Status:** Product Requirements / Architecture Input  
**Version:** 0.1

---

# 1. Product Vision

QRBank is a web-based classroom banking, behavior-management, job-management, rewards, and QR transaction system for teachers.

It began as a Microsoft Power Apps / Microsoft Lists / Power Automate application and is being rebuilt as an independent web application.

The new application must preserve the useful behavior of the existing system while improving reliability, transaction safety, QR scanning, administration, scalability, multi-classroom support, account management, analytics, reward purchasing, QR generation, and future extensibility.

The long-term product may become a hosted service used by multiple teachers, schools, students, and parents. The initial product should therefore be designed as a multi-user web application rather than as a single-teacher local tool.

---

# 2. Authority of This Specification

This document defines required product behavior.

AI coding agents and human developers must not silently remove, simplify, reinterpret, or replace requirements because another implementation would be easier.

If a requirement appears contradictory or materially ambiguous, the issue must be surfaced before implementation.

Implementation details such as exact PostgreSQL table names, column names, framework structure, and internal service organization are not fixed by this document unless explicitly stated.

The architecture and database design should be proposed separately and reviewed before implementation.

---

# 3. Development Philosophy

QRBank must be developed incrementally. Do not attempt to generate the entire production application in one step.

For each milestone:

1. Read this specification and relevant supporting documentation.
2. Propose important design decisions.
3. Implement only the current milestone.
4. Add or update automated tests.
5. Run the tests.
6. Correct failures.
7. Review against the specification.
8. Commit a stable checkpoint.
9. Proceed only after the milestone is stable.

Existing functionality must not be silently removed during later development.

---

# 4. Product Categories

## 4.1 Required for the Initial Product

The initial custom version should include:

- teacher account registration and authentication
- email verification
- multiple classrooms per teacher
- classroom management
- student management
- QR scanning
- Quick Actions
- financial transaction ledger
- manual transactions
- transaction reversals
- behavior tracking
- classroom jobs
- job assignments
- weekly wages
- weekly allowance
- classroom rewards store
- reward purchases
- QR generation
- settings
- transaction history
- basic analytics
- CSV/data export
- administrative integrity checks

## 4.2 Architect for Now, Implement Later if Needed

The architecture should make these possible without a major redesign:

- multiple teachers in one classroom
- teacher assistants
- school administrators
- student accounts
- parent accounts
- student-facing balance views
- student self-service reward purchasing
- teacher approval workflows
- stronger teacher verification
- paid subscription plans
- school-level subscriptions
- feature entitlements
- richer inventory management
- advanced analytics
- public/mobile APIs

## 4.3 Future Experiments

These do not need to affect the first implementation unless a later requirement says otherwise:

- advertisements
- gamification
- leaderboards
- school/district dashboards
- native mobile apps
- messaging
- advanced QR/card designers
- third-party integrations

---

# 5. User Accounts

Teachers must be able to register their own accounts.

Initial registration should support email address, password, email verification, password reset, account status, and account creation timestamp.

Passwords must never be stored in plaintext. Authentication must use established secure password-hashing and session-management practices.

A verified email address is required before the account is considered fully active.

---

# 6. Teacher Verification

Email verification and teacher verification are separate concepts.

Initial releases may allow teachers to use the application after normal email verification.

The architecture should support a separate teacher-verification status such as not requested, pending, verified, or rejected.

Future verification methods may include educational email domains, school or organization confirmation, manual documentation review, or educator credential verification.

The initial product should not depend on a complicated educator-license verification system, and a school email address alone must not permanently define whether someone is a teacher.

---

# 7. Multi-Tenant Security

QRBank may host many unrelated teachers. Data isolation is mandatory.

A teacher must never gain access to another user's classrooms, students, balances, transactions, behavior records, jobs, rewards, settings, or QR tokens merely by modifying an identifier in a URL or API request.

Every server-side operation must validate authorization based on the authenticated user's relationship to the requested classroom or resource. Client-side hiding is not sufficient security.

---

# 8. Classrooms

A teacher can create and manage multiple classrooms.

A classroom is a first-class entity with concepts such as:

- immutable internal identifier
- teacher-facing name
- school year
- active/archive status
- settings
- creation date

Examples include Period 1 Math, Period 4 Science, and Homeroom.

The architecture should eventually permit more than one authorized adult to manage the same classroom. The initial interface may expose only a classroom owner if desired.

---

# 9. Classroom Archiving and School-Year Rollover

Classrooms should normally be archived rather than permanently deleted when historical records exist.

Teachers should be able to end a school year while preserving students, balances as historical data, transaction history, behavior records, jobs, reward purchases, and analytics.

The system should eventually support creating a new classroom/year from a previous classroom without corrupting historical records.

---

# 10. Students and Enrollments

Students are independent entities.

The system should distinguish a student from that student being enrolled in a particular classroom. This should support multiple classrooms, school years, transfers, and future student accounts.

Student/classroom information should support stable internal student identity, teacher-facing student identifier, classroom enrollment, balance, active/inactive state, notes, and creation date.

Teacher-facing IDs may look like 01, 02, and 03, but internal database relationships must not rely solely on these human-readable IDs.

---

# 11. Student Privacy

The application should minimize unnecessary personally identifiable information.

A teacher should be able to operate the core classroom banking system using identifiers such as Student 01 without requiring a full legal name.

QR codes must not contain unnecessary personally identifiable information.

---

# 12. Main Teacher Dashboard

The teacher dashboard should provide quick access to classrooms, students, balances, scanner mode, transactions, behavior data, jobs, rewards, analytics, and settings.

Within a classroom, active students and their current balances should be easy to view.

---

# 13. Student Details

A student detail page should provide access to:

- current balance
- transaction history
- behavior history
- behavior analytics
- active jobs
- job assignment controls
- manual transactions
- balance adjustment
- reward purchasing
- reversal controls
- relevant account/activity information

---

# 14. QR Scanning Is a Core Interface

QR scanning is a primary interaction method, not an optional accessory.

The system must support USB barcode/QR scanners that behave as keyboards and normally append Enter or carriage return.

The teacher should be able to enter a dedicated Scan Mode quickly from the classroom interface. Scanning should require little or no mouse interaction during normal operation.

---

# 15. QR Scan Workflow

The existing workflow is:

    Student scan
    -> Quick Action scan
    -> Transaction

The first scan selects a student. The second scan identifies an action and executes it against the selected student.

Legacy payload examples include STUDENT:01 and QUICK:QA1. The new implementation does not need to preserve these literal strings internally and may use secure opaque scan tokens, but the rapid two-scan user experience must be preserved.

---

# 16. QR Token Requirements

QR payloads should not contain student names, teacher emails, database credentials, or sensitive personal information.

The preferred architecture should use unique opaque tokens or an equally safe mechanism.

A teacher should be able to view, download, print, disable, and regenerate a QR. Regenerating a QR should invalidate the previous token without changing the student or transaction history.

---

# 17. Scanner Focus Behavior

On the dedicated scanning interface, USB scanner input should be ready without requiring the teacher to click an input before every scan.

The application should restore scanning readiness after successful scans, failed scans, and completed transactions.

It must not aggressively steal keyboard focus while the teacher is intentionally typing into notes, settings, forms, reward fields, job fields, behavior details, or other text inputs.

Scanner capture must therefore have a clearly controlled active/inactive state.

---

# 18. Scanner Status

The scanning interface must clearly communicate processing state, including at minimum READY TO SCAN and PROCESSING.

Additional statuses may include STUDENT SELECTED, TRANSACTION COMPLETED, INSUFFICIENT BALANCE, INVALID QR, STUDENT NOT FOUND, and ACTION NOT FOUND.

The selected student, current balance, scanner status, and recent transaction should be prominent.

---

# 19. Scanner Processing Safety

A second financial action must not accidentally overlap an unresolved transaction.

When a financial scan begins processing, the client should enter a processing state, overlapping client requests should be prevented or safely queued, and server-side protections must still exist.

A visual busy indicator alone is not sufficient.

---

# 20. Duplicate Scan Handling

The system may support a configurable duplicate-scan timing window such as DuplicateScanSeconds. A value of 0 can mean timing-based duplicate suppression is disabled.

This must not prevent legitimate repeated transactions. Timing-based duplicate suppression is distinct from server-side financial request idempotency.

---

# 21. Audio Feedback

Audio confirmation is important because the teacher may scan without watching the screen.

Required sounds should include student selected, financial transaction successful, and error.

Sound should be configurable. Success audio must play only after the server confirms that the transaction committed successfully. The scanner hardware beep is not application confirmation.

---

# 22. Mobile Scanning

The architecture should support mobile camera QR scanning.

Mobile scanning must invoke the same backend transaction services used by USB scanner actions. There must not be separate financial logic for desktop and phone scanning.

---

# 23. Quick Actions

Quick Actions represent configurable scannable operations.

Required defaults conceptually include:

- QA1 -> General +5
- QA2 -> General +10
- QA3 -> General -5
- QA4 -> General -10
- QA5 -> Positive Behavior +5
- QA6 -> Positive Behavior +10
- QA7 -> Behavior Deduction -5
- QA8 -> Behavior Deduction -10
- QA9 -> General +1
- QA10 -> General -1

Quick Action behavior must be database/configuration driven rather than hard-coded only in JavaScript.

---

# 24. Actions

Actions should support a stable identifier, name, category, integer amount, active/inactive state, and description.

Historical transactions must preserve what an action meant when the transaction occurred. Changing an action later must not rewrite historical meaning.

---

# 25. Financial Integrity — Mandatory

Financial correctness is a highest-priority requirement.

Balances and point amounts must use integer arithmetic. Do not use floating-point values for classroom currency.

All operations that change a balance must use one authoritative backend financial transaction service or equivalent centralized domain logic.

This includes QR Quick Actions, manual deposits/deductions, reward purchases, balance adjustments, weekly allowance, job wages, bulk transactions, reversals, and future student purchases.

---

# 26. Atomic Transactions

A balance change and corresponding financial ledger entry must succeed or fail together.

The backend should conceptually:

    begin database transaction
    lock/read authoritative balance
    validate request
    calculate new balance
    write ledger entry
    update balance
    commit

If any step fails, the whole operation rolls back, the balance remains unchanged, no completed ledger entry remains, the client receives an error, and success audio does not play.

PostgreSQL row-level locking or another demonstrably safe concurrency strategy should be used.

---

# 27. Browser Is Not Financial Authority

The frontend must never be trusted as the authority for current balance, transaction validity, transaction amount, negative-balance eligibility, or action availability.

The server must obtain and validate authoritative values independently.

---

# 28. Concurrent Transaction Safety

The system must remain correct when multiple requests occur nearly simultaneously, including rapid scanner events, browser tabs, phone and desktop use, scheduled payments, and repeated button clicks.

If a student has 5 points and two simultaneous -5 requests occur with negative balances disabled, only one should succeed, final balance should be 0, and the ledger must remain correct.

Concurrency behavior must be covered by automated tests.

---

# 29. Financial Request Idempotency

Financial requests should support server-side idempotency where appropriate.

A retry of the same logical request must not create an additional transaction.

Idempotency should be enforced server-side and preferably with database uniqueness constraints.

---

# 30. Negative Balances

Negative balances are disabled by default.

A deduction must normally satisfy:

    current_balance + amount >= 0

If not, reject the operation, do not change the balance, do not create a completed transaction, and provide clear error feedback.

Classrooms may configure this later.

---

# 31. Financial Ledger

Every balance-changing event must create a durable financial ledger record.

Financial history must not be silently erased or rewritten.

The ledger should retain enough information to understand the event later, including transaction identity, student/enrollment, classroom, timestamp, amount, category, action/Quick Action information, behavior reason when applicable, reward information when applicable, job information when applicable, note, balance before/after, status, reversal relationship, source, authenticated actor, and request/idempotency identifier.

Exact schema will be reviewed separately.

---

# 32. Historical Snapshots

Historical transactions must retain their original meaning.

If a teacher later changes an action name, behavior reason, job wage, reward price, or reward name, old transactions must still show the values that existed when the transaction occurred.

The architecture may combine foreign keys with historical snapshot fields.

---

# 33. Transaction Sources

Transactions should identify how they were initiated, such as Scanner, Phone, Manual, Automatic, RewardStore, Admin, or Bulk.

---

# 34. Reversals / Undo

A financial transaction must not be deleted to undo it.

Undo creates an opposite financial transaction and leaves the original visible. The reversal references the transaction it reverses.

A reversal transaction must itself be reversible. The complete chain must remain auditable.

---

# 35. Manual Transactions

Teachers can manually add or subtract an arbitrary integer amount with category and reason/note.

Manual transactions use the same central financial engine and follow atomicity, ledger, concurrency, negative-balance, and reversal rules.

---

# 36. Set Balance / Balance Adjustment

Teachers can set a student's balance to a target value.

This must not silently overwrite the balance.

The system should calculate:

    adjustment = desired_balance - current_balance

and create an adjustment ledger transaction recording previous balance, target balance, difference, reason, and actor.

---

# 37. Bulk Financial Operations

The system may support class-wide additions, deductions, or starting-balance initialization.

Every affected student must receive an individual auditable financial transaction. Bulk operations must not directly overwrite balances without ledger entries.

---

# 38. Behavior Tracking

QRBank supports positive and negative behavior tracking.

Behavior actions may simultaneously affect balances.

A fast behavior QR transaction must be allowed to complete before a detailed behavior reason is entered. The teacher may attach detail afterward so the scanning workflow remains fast.

---

# 39. Behavior Reasons

Teachers should be able to configure behavior reasons with identifier, name, positive/negative type, description, and active/inactive status.

Positive transactions should offer positive reasons, negative behavior transactions negative reasons, and historical transactions must retain the original reason name.

---

# 40. Behavior Analytics

Student behavior analytics should support positive-event counts, negative-event counts, behavior history, weekly trends, date filtering, and charts.

The architecture should allow richer analytics later.

---

# 41. Classroom Jobs

Teachers can create classroom jobs with identifier, name, weekly pay, active/inactive status, and notes.

Teachers can create jobs, change pay, activate/deactivate jobs, assign jobs, and end assignments.

Jobs with historical activity should generally be deactivated rather than deleted.

---

# 42. Job Assignments

Students may have multiple active jobs.

Job assignments should exist independently from job definitions and track student/enrollment, job, active status, start date, and end date.

The same student should not accidentally receive duplicate active assignments for the same job unless explicitly allowed later.

---

# 43. Weekly Job Wages

Active jobs generate automatic weekly wage transactions.

Current default schedule:

    Monday 6:10 AM
    America/New_York

Each job assignment creates its own transaction. A student with three jobs receives three separate wage transactions.

Payments must use the central financial service, remain safe under concurrency, avoid duplicate payment, create ledger records, and preserve job name/wage at payment time.

---

# 44. Weekly Allowance

Active students may receive a scheduled weekly allowance.

Current default:

    Amount: 20
    Schedule: Monday 6:00 AM
    Timezone: America/New_York

Allowance settings should be configurable and each payment creates its own financial transaction.

---

# 45. Scheduled Task Idempotency

Scheduled jobs must never accidentally pay twice for the same period.

The architecture should use durable idempotency/uniqueness rather than only an application-level run flag.

Partial failure and crash recovery must be considered. A retried run should safely complete missing work without repeating completed payments.

---

# 46. Classroom Rewards Store

Each classroom should have a teacher-managed rewards catalog.

Examples include Small Toy — 25 points, Choose Your Seat — 40 points, Homework Pass — 75 points, and Extra Computer Time — 30 points.

Rewards should be first-class entities rather than generic financial actions.

---

# 47. Reward Definitions

A reward may include stable identifier, classroom, name, description, point price, active/inactive status, reward type, optional image, optional inventory quantity, optional purchase limits, and optional availability dates.

Reward types may include Physical Item, Privilege, and Other.

---

# 48. Purchasing Rewards

The teacher should be able to purchase a reward for a student from the student's page or a dedicated classroom Store page.

Both interfaces must use the same backend purchasing service.

A purchase must atomically verify balance and availability, deduct points, create ledger entry, create necessary purchase/fulfillment record, and update inventory when applicable.

---

# 49. Reward Purchase History

Reward purchases should appear clearly in transaction history.

Historical purchase records must preserve the original reward, reward name, and price paid. Changing the reward later must not alter history.

---

# 50. Reward Refunds

A mistaken reward purchase must be corrected using the standard reversal system.

Do not delete the purchase. A refund/reversal credits the appropriate amount and retains history. Inventory restoration must be handled consistently.

---

# 51. Reward Fulfillment / Redemption

Some rewards are used immediately; others are purchased now and redeemed later.

The architecture should separate financial purchase from fulfillment/redemption. Possible states may include Unused, Redeemed, and Cancelled.

Exact workflow can be refined later.

---

# 52. QR Code Center

Each classroom should have a QR management area.

Teachers should be able to generate QR codes for individual students, selected students, all active students, individual Quick Actions, selected Quick Actions, and all active Quick Actions.

Future scan-enabled entities may also be supported.

---

# 53. Automatic QR Availability

When a new scan-enabled entity is created, its QR should be immediately available.

Teachers should not need to register codes with an external service.

---

# 54. Individual QR Downloads

Teachers should be able to view and save individual QR codes.

Supported export formats should include at least PNG and SVG. SVG is important for high-quality printing and use in design software such as Canva.

QR images should be generated locally by QRBank using an installed library, not by unnecessary third-party QR-generation APIs.

---

# 55. Bulk QR Generation

Teachers should be able to generate codes in bulk for all/selected students and all/selected Quick Actions.

A ZIP of individual image files may also be supported.

---

# 56. Printable QR PDFs

QRBank should generate printable QR sheets/cards.

At minimum support US Letter and A4.

Output modes should include QR Only, Student Cards, and Quick Action Cards.

Teachers should be able to generate one card, selected cards, all student cards, all Quick Action cards, or combined classroom packs.

Raw QR downloads must remain available for external design tools.

---

# 57. QR Print Reliability

QR generation must prioritize scanner reliability using sufficient quiet zone, adequate print size, high contrast, appropriate error correction, and adequate resolution.

Decorative styling must not compromise scanning. A QR test/preview feature is desirable.

---

# 58. QR Regeneration

Teachers should be able to regenerate the QR token for a scan-enabled entity.

The old QR becomes invalid, the new QR becomes active, and identity, balances, and transaction history remain unchanged.

---

# 59. Classroom Settings

A classroom should have configurable settings including classroom name, school year, starting balance, weekly allowance amount/enabled/schedule, job wages enabled, sound enabled, duplicate-scan interval, negative-balance policy, student-selection timeout, transaction confirmations, balance-reset confirmation, and timezone.

Configuration should not be scattered throughout source code.

---

# 60. Time Handling

Server timestamps should preferably be stored in UTC and displayed using the classroom/user timezone.

Default relevant timezone is America/New_York.

Scheduled tasks must correctly handle daylight-saving changes and must not permanently assume a fixed UTC offset.

---

# 61. Transaction History

Teachers must be able to browse financial history with useful filters such as student, classroom, date range, category, action, behavior type, reward, job, source, and status.

Transaction details should display reversal relationships.

---

# 62. Data Export

Teachers should be able to export classroom data including student balances, transaction ledger, behavior data, jobs, assignments, and reward purchases.

CSV should be supported and data should be structured cleanly for pandas or similar tools.

---

# 63. Student Import

CSV import should eventually allow teachers to add many students efficiently.

Imports should validate data and report errors clearly.

---

# 64. Administrative Audit Log

Financial ledger data and application audit data are separate concepts.

Important non-financial changes should be auditable where practical, such as reward price changes, job wage changes, QR regeneration, classroom setting changes, student deactivation, and permission changes.

Audit entries should record responsible authenticated user and timestamp.

---

# 65. Integrity Audit

QRBank should provide an administrative integrity-check mechanism capable of detecting stored balance vs ledger-derived balance mismatches, duplicate automatic payments, invalid reversal relationships, orphaned financial records, duplicate active job assignments, and other important consistency violations.

Detected problems should be reported, not silently rewritten away.

---

# 66. Active / Inactive Records

Where history references an entity, QRBank should normally prefer Active/Inactive or Archived rather than permanent deletion.

This applies to students/enrollments, classrooms, jobs, actions, Quick Actions, behavior reasons, and rewards.

Inactive items should not normally be selectable for new activity but must remain visible historically.

---

# 67. No Silent Destructive Deletes

Financial ledger entries must not be deleted through normal application use.

Records with historical dependencies should not be casually removed. Destructive actions should be prohibited, archived/deactivated, or require strong administrative confirmation.

---

# 68. Authentication Security

Production authentication must use secure established practices including secure password hashing, secure sessions, protected endpoints, HttpOnly cookies where appropriate, Secure cookies under HTTPS, appropriate SameSite settings, brute-force protection, CSRF protection where required, no plaintext passwords, and no secrets in source control.

---

# 69. Roles and Permissions

The initial product may expose only a teacher/owner role.

Authorization architecture should accommodate future roles such as Classroom Owner, Teacher, Assistant, School Administrator, Student, and Parent.

Permissions should be based on relationships and entitlements rather than fragile client-side checks.

---

# 70. Future Student Accounts

Students do not require accounts in the first release.

The architecture should permit a future authenticated user account to link to an existing student identity without replacing the financial ledger or student history.

---

# 71. Future Parent Accounts

Parent/guardian accounts are optional future functionality.

The data model should not make future parent-student relationships unnecessarily difficult, but no parent portal is required initially.

---

# 72. Monetization Readiness

QRBank may eventually offer paid features.

The architecture should allow future concepts such as Free, Pro, and School without embedding plan-specific logic throughout unrelated code.

Feature access should eventually be controlled through a centralized entitlement/capability system. Payment processing is not required initially.

---

# 73. Advertisements

Advertising is only a possible future experiment and must not influence the initial architecture.

Because student/child data may eventually be involved, advertising and tracking should not be introduced casually.

---

# 74. Responsive Design

Desktop use is especially important because USB scanning is a primary workflow.

The application should also be usable on tablets and phones, including common classroom-management tasks and eventual camera scanning.

---

# 75. Accessibility

The application should use reasonable accessibility practices including keyboard navigation, semantic form labels, usable contrast, visible focus states, and screen-reader-friendly controls where practical.

---

# 76. Performance

Scanning and financial transactions should feel fast.

The frontend should not reload entire datasets after every action.

A financial response should normally return enough authoritative information to update the interface directly, including success/failure, transaction identifier, new balance, student information, and relevant message.

Appropriate database indexing should be part of the architecture proposal.

---

# 77. Database Technology

The production database should use PostgreSQL.

The exact schema is not defined by this document.

The architecture-design phase must propose a normalized PostgreSQL model satisfying current and future requirements.

Important principles include stable internal identifiers, proper foreign keys, appropriate unique constraints, integer balances, auditability, transactional correctness, multi-tenant isolation, historical data preservation, and future extensibility.

---

# 78. Database Migrations

Production schema evolution must use versioned migrations.

Alembic is preferred unless the architecture proposal provides a convincing alternative.

Schema changes must never be undocumented manual edits.

---

# 79. Backup and Recovery

Production must include regular PostgreSQL backups, a documented restore procedure, backup before significant migrations/upgrades, and persistent storage independent of application deployment.

A failed software update must not destroy classroom financial records.

---

# 80. Deployment Direction

Initial production deployment may use Linux VPS, Ubuntu or Debian, Python, FastAPI, PostgreSQL, Nginx, HTTPS, and systemd or an equivalent service manager.

This is a current preferred direction, not necessarily an immutable limitation.

Configuration and secrets must come from secure environment configuration rather than hard-coded values.

---

# 81. Scheduling

Weekly allowances and wages must run server-side and must not depend on a teacher having the website open.

The scheduling architecture should avoid accidental execution from multiple application workers.

A simple reliable solution appropriate to a single VPS is preferred over unnecessary distributed complexity.

---

# 82. Application Logging

Technical application logs should record useful failures and diagnostic identifiers.

Logs must not expose passwords, session secrets, authentication tokens, or unnecessary student information.

Application logs are not a replacement for the financial ledger.

---

# 83. Automated Testing

Automated tests are mandatory for core financial and scanner behavior.

Required scenarios include:

- valid and invalid student scans
- malformed QR scan
- valid and invalid Quick Actions
- Quick Action without selected student
- +1, +5, +10, -1, -5, -10
- insufficient balance
- exact-zero balance
- behavior reward/deduction/reason attachment
- manual transaction
- set-balance adjustment
- transaction reversal
- reversal of reversal
- reward purchase
- insufficient-funds reward purchase
- reward reversal/refund
- duplicate HTTP request
- simultaneous deductions/deposits
- weekly allowance and duplicate same-period allowance
- weekly wages and duplicate same-period wages
- multiple jobs for one student
- failed database transaction rollback
- unauthorized endpoint access
- cross-tenant access attempt

Concurrency and financial-integrity tests are mandatory.

---

# 84. AI Development Rules

Any AI coding agent working on QRBank must:

1. Read this specification before architectural or code changes.
2. Read relevant supporting documentation.
3. Not remove functionality merely to simplify implementation.
4. Not change database schema without a migration.
5. Not use floating-point values for balances.
6. Not modify balances outside the central financial domain/service.
7. Not delete ledger entries to implement undo.
8. Not trust browser-submitted balances.
9. Not report success before database commit succeeds.
10. Not implement financial correctness only in frontend JavaScript.
11. Not commit credentials or secrets.
12. Not use production classroom data for development testing.
13. Run relevant tests before declaring a milestone complete.
14. Report material requirement conflicts.
15. Ask/review rather than guess when financial behavior is uncertain.

---

# 85. Architecture Review Requirement

Before implementation begins, the AI/developer must propose the system architecture covering backend structure, frontend approach, authentication, authorization, multi-tenancy, financial transaction service, QR scanning subsystem, QR token strategy, rewards, scheduled jobs, PostgreSQL architecture, migrations, deployment, security, and testing strategy.

The architecture proposal must identify important tradeoffs.

Do not begin implementing the complete application during this stage.

---

# 86. Database Design Review Requirement

After architecture review, the AI/developer must propose the PostgreSQL schema.

The schema proposal must explain major entities, relationships, primary keys, foreign keys, unique constraints, ledger design, student/classroom relationship, account/classroom permissions, reward purchasing, job assignments, automation idempotency, QR token management, historical snapshot strategy, indexes, concurrency considerations, future student/parent account compatibility, and monetization/entitlement compatibility.

Do not create production migrations until the database proposal has been reviewed.

---

# 87. First-Release Priority

When implementation tradeoffs arise, prioritize:

1. Financial correctness
2. Data isolation/security
3. QR scanning reliability
4. Auditability
5. Teacher usability
6. Student/classroom management
7. Reward purchasing
8. Behavior tracking
9. Jobs and automation
10. QR generation
11. Analytics
12. Optional future features

Reliability must not be sacrificed merely to add more features quickly.

---

# 88. Definition of Success

The first production release is successful when a teacher can:

1. Register and verify an account.
2. Create more than one classroom.
3. Add/import students.
4. Configure classroom settings.
5. Create/manage actions and Quick Actions.
6. Generate and print student/action QR codes.
7. Enter Scan Mode.
8. Scan a student.
9. Scan a Quick Action.
10. Receive reliable visual/audio confirmation.
11. View the correct resulting balance and ledger entry.
12. Perform manual transactions.
13. Reverse transactions safely.
14. Record and analyze behavior.
15. Create classroom jobs.
16. Assign multiple jobs to students.
17. Automatically pay weekly job wages.
18. Automatically distribute weekly allowances.
19. Create rewards.
20. Purchase rewards for students.
21. Refund/reverse mistaken reward purchases.
22. Review transaction and reward history.
23. Export classroom data.
24. Run integrity checks.
25. Archive a classroom while preserving historical information.

All financial actions must remain correct under retries, failures, and concurrent requests.
