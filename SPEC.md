# QRBank Master Specification

**Status:** Product Requirements / Architecture Input  
**Version:** 0.1

---

# 1. Product Vision

QRBank is a web-based classroom banking, behavior-management, job-management, rewards, and QR transaction system for teachers.

It began as a Microsoft Power Apps / Microsoft Lists / Power Automate application and is being rebuilt as an independent web application.

The new application must preserve the useful behavior of the existing system while improving:

- reliability
- transaction safety
- QR scanning
- administration
- scalability
- multi-classroom support
- account management
- analytics
- reward purchasing
- QR generation
- future extensibility

The long-term product may become a hosted service used by multiple teachers, schools, students, and parents.

The initial product should therefore be designed as a multi-user web application rather than as a single-teacher local tool.

---

# 2. Authority of This Specification

This document defines required product behavior.

AI coding agents and human developers must not silently remove, simplify, reinterpret, or replace requirements because another implementation would be easier.

If a requirement appears contradictory or materially ambiguous, the issue must be surfaced before implementation.

Implementation details such as exact PostgreSQL table names, column names, framework structure, and internal service organization are **not fixed by this document unless explicitly stated**.

The architecture and database design should be proposed separately and reviewed before implementation.

---

# 3. Development Philosophy

QRBank must be developed incrementally.

Do not attempt to generate the entire production application in one step.

Development should proceed through reviewed milestones.

For each milestone:

1. Read this specification and relevant supporting documentation.
2. Propose any important design decisions.
3. Implement only the current milestone.
4. Add or update automated tests.
5. Run the tests.
6. Correct failures.
7. Review the implementation against the specification.
8. Commit a stable checkpoint.
9. Proceed only after the milestone is considered stable.

Existing functionality must not be silently removed during later development.

---

# 4. Product Categories

Requirements are divided into three categories.

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

The architecture should make these features possible without a major redesign:

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

Initial registration should support at minimum:

- email address
- password
- email verification
- password reset
- account status
- account creation timestamp

Passwords must never be stored in plaintext.

Authentication must use established secure password-hashing and session-management practices.

The system should support account recovery.

A verified email address is required before the account is considered fully active.

---

# 6. Teacher Verification

Email verification and teacher verification are separate concepts.

Initial releases may allow teachers to use the application after normal email verification.

The architecture should support a separate teacher-verification status that can later represent states such as:

- not requested
- pending
- verified
- rejected

Potential future verification methods may include:

- verified educational email domain
- school or organization confirmation
- manual documentation review
- educator credential verification

The initial product should not depend on a complicated educator-license verification system.

A school email address alone must not permanently define whether someone is a teacher.

---

# 7. Multi-Tenant Security

QRBank may host many unrelated teachers.

Data isolation is mandatory.

A teacher must never gain access to another user's:

- classrooms
- students
- balances
- transactions
- behavior records
- jobs
- rewards
- settings
- QR tokens

merely by modifying an identifier in a URL or API request.

Every server-side operation must validate authorization.

Authorization must be based on the authenticated user's relationship to the requested classroom or resource.

Client-side hiding is not sufficient security.

---

# 8. Classrooms

A teacher can create and manage multiple classrooms.

A classroom is a first-class entity.

Each classroom should have information such as:

- internal immutable identifier
- teacher-facing name
- school year
- active/archive status
- settings
- creation date

Examples:

- Period 1 Math
