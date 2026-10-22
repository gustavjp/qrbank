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
- classroom-scoped student codes with active/inactive status
- central opening-balance financial posting service with PostgreSQL row locking and idempotency
- immutable opening-balance ledger history with stored balance cache updates in the same transaction
- health and readiness endpoints
- PostgreSQL-backed automated tests

QR scan execution, default Quick Action seeding, normal/manual transaction flows, rewards UI, scheduled payments, password reset, and a production email provider are intentionally not implemented yet.

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
