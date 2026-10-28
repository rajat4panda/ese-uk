# ESE International Student Recruitment & Compliance System

Student-route module of the eMigrant® platform for ESE Insight Limited (European School of Economics).
Built by OMFYS Technologies UK Ltd to **LLD v1.1** under the delivery plan in **PDP v1.0**, with Finteam Ltd as
compliance authority.

**Current release: Sprint 3 — post-CAS compliance.** The system now covers the whole journey: an applicant
from registration to an assigned CAS, and a sponsored student from the visa decision through enrolment,
attendance, changes of circumstances, reporting to UKVI within ten working days, the Basic Compliance
Assessment rates and an evidence pack. See [`docs/SPRINT3_REPORT.md`](docs/SPRINT3_REPORT.md) for this
sprint, [`docs/SPRINT2_REPORT.md`](docs/SPRINT2_REPORT.md) for admissions to CAS, and
[`docs/FOUNDATION_REPORT.md`](docs/FOUNDATION_REPORT.md) for Sprints 0 and 1.
[`docs/RUN_GUIDE.md`](docs/RUN_GUIDE.md) is how to run it.

## Stack (LLD 2)

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Pydantic v2 |
| Data | PostgreSQL 16 via SQLAlchemy 2 and Alembic (forward-only migrations) |
| Cache | Redis 7: token deny-list, session inactivity, rate limits. Holds no durable data |
| Files | S3-compatible storage with SSE, in a UK/EEA region (local disk in development) |
| Malware scan | ClamAV (clamd). A simulated scanner is used in development and detects the EICAR test file |
| Worker | APScheduler in a separate process |
| Web client | TypeScript + React single-page app, served by the API from the same origin |

## Repository layout

```
app/
  main.py            application factory, middleware, error model
  config.py          environment settings (refuses to start on a default SECRET_KEY)
  database.py        engine and session
  models/            SQLAlchemy models, one per aggregate
  schemas/           Pydantic contracts; Appendix A data dictionary in schemas/application.py
  api/               routers: auth, applications (+documents), workflow, admin, reference
  services/          domain logic, no HTTP imports: lifecycle, guards, audit, assessments, offers, cas,
                     finance, accounts, documents, storage...
  views.py           role-shaped responses (LLD 5.9)
  workers/           scheduler process: the LLD 13 compliance jobs, each writing a job_runs row
  cli.py             create-admin, seed-reference, seed-allocation-and-fees, seed-demo
migrations/versions/ 0001 baseline schema + audit grants, 0002 auth and settings,
                     0003 assessments, offers, fees, CAS and student records,
                     0004 attendance, reportable events, bank holidays and the operational tables
tests/               unit/, integration/, guards/, e2e/ (Playwright)
web/                 React client
scripts/bootstrap_db.sql   database roles: owner, app login, app_role
```

## Running locally

Prerequisites: Python 3.12, Node 22, PostgreSQL 16 and Redis 7 (or use `docker compose up`).

```bash
# 1. Database roles and database (once, as a PostgreSQL superuser)
psql -U postgres -v owner_pw=ese_owner_local -v app_pw=ese_app_local -v dbname=ese_dev -f scripts/bootstrap_db.sql

# 2. Configuration
cp .env.example .env        # then set SECRET_KEY and the two database URLs

# 3. Backend
pip install -r requirements-dev.txt
alembic upgrade head        # runs as MIGRATION_DATABASE_URL (schema owner)
python -m app.cli seed-reference     # placeholder programmes, intakes, fee schedules and CAS allocation (D-05)
python -m app.cli seed-demo          # demo staff: <role>@demo.ese.example / Demo-Password-2026!
                                     # roles: admissions, admissions2, compliance, compliance2,
                                     # finance, senior, admin — two reviewers each where the
                                     # segregation-of-duties controls need a colleague

# 4. Web client
cd web && npm ci && npm run build && cd ..

# 5. Run
uvicorn app.main:app --port 8000     # http://localhost:8000  ·  API docs: /api/docs
python -m app.workers.scheduler      # separate process
```

For client development with hot reload, run `npm run dev` in `web/`. It serves on port 5173 and proxies
`/api` to port 8000.

To create the first real administrator, run `python -m app.cli create-admin --email ... --name "..."`.
It prints a one-time activation link.

## Tests

```bash
# once: bootstrap ese_test the same way as ese_dev (-v dbname=ese_test)
pytest -q --cov=app                 # unit, integration and guard suites against real Postgres + Redis
pytest tests/guards -q              # release blockers (T-15 audit immutability, lifecycle)
```

The suites connect as the application role, so the audit grant is tested exactly as it applies in production.
You can override the connection strings with `TEST_DATABASE_URL`, `TEST_MIGRATION_DATABASE_URL` and
`TEST_REDIS_URL`.

The browser journeys need a running server, the seeded reference data and the demo accounts:

```bash
uvicorn app.main:app --port 8000 > var/uvicorn.log 2>&1 &
E2E_BASE_URL=http://localhost:8000 E2E_SERVER_LOG=var/uvicorn.log pytest tests/e2e -p no:cacheprovider

# The Sprint 2 chain, submitted application through to an assigned CAS, and the Sprint 3 chain,
# CAS through to a reported event and a downloaded evidence pack. Both sign in as each role in turn
# and write the evidence screenshots to docs/screenshots/.
python3 tests/e2e/sprint2_chain.py
python3 tests/e2e/sprint3_post_cas.py

# WCAG 2.1 AA across every screen (NFR-11). Needs npm install in web/ for axe-core.
python3 tests/e2e/accessibility_sweep.py
```

Sign-in is rate limited to 10 attempts per minute per IP, and the walkthrough signs in seven times.
Start the server with `RATE_LIMIT_AUTH_PER_MINUTE=200` when running it, in development only.

## Walking the system after it starts

Sign in at `http://localhost:8000`. The demo accounts share the password above.

1. **Applicant.** Register at `/register` (the verification link is printed to the server console
   because `MAIL_BACKEND=console`), complete the nine-step form, upload documents and submit.
2. **Admissions** (`admissions@demo.ese.example`) opens the record from `/staff`, completes the academic
   assessment, then builds and issues the offer.
3. **Applicant** accepts the offer at `/my/offer`.
4. **Finance** (`finance@demo.ese.example`) records the deposit on the record's Fees tab. Admissions then
   marks the deposit condition met on the Offer tab.
5. **Compliance** (`compliance@demo.ese.example`) completes the pre-CAS review. At Medium or High risk,
   `compliance2@demo.ese.example` records the second-line review.
6. **Senior** (`senior@demo.ese.example`) opens the Pre-CAS summary and assigns the CAS. A senior who
   completed either review is refused, and told so before the form is opened (FR-55).
7. **Compliance** opens the new student record from `/staff/students`: records the visa decision and
   the enrolment, captures attendance, then records a deferral — which raises its reportable event
   and starts the ten working day clock in the same step.
8. **Compliance** reports it from `/staff/events` with the SMS reference; `/staff/compliance` shows
   the three BCA rates. **Senior** builds the evidence pack from the student record.

The Pre-CAS summary before step 4 is the clearest demonstration of the controls: the Assign CAS button
is shown disabled with each outstanding reason beside it.

## Security model (summary)

- **Database roles.** The application connects as `ese_app` (a member of `app_role`). That role holds
  INSERT and SELECT on `audit_entries` and no UPDATE, DELETE or TRUNCATE. It has no DELETE on any table.
  Migrations run as `ese_owner`, and production refuses to start if the two connection strings are the same.
- **Status changes.** `applications.status` can be written only by `services/lifecycle.transition()`. An
  attribute listener raises if any other code sets it, and a test scans the codebase for such writes.
- **Audit entries.** Every material action writes an audit entry inside the same transaction.
  The action vocabulary is closed (`services/audit.py`).
- **Authentication.** bcrypt (cost 12); JWT HS256 with a 12-hour expiry, a 30-minute inactivity timeout and
  revocation through a deny-list; lockout at 5 and 10 failures; rate limits of 10 requests per minute per IP
  on authentication endpoints and 300 per minute per token elsewhere.
- **Uploads.** The file type is taken from its magic bytes and the limit is 5MB, enforced even on chunked
  requests. Files are scanned before they are stored, and the upload fails closed if the scanner is
  unavailable. Storage keys are opaque and every download is audited.
- **HTTP headers.** Strict CSP (`default-src 'self'`), HSTS, nosniff, `X-Frame-Options: DENY`, and a request
  ID on every error.

## Deployment

Build the image from the `Dockerfile`. It contains the API and the built client, and the same image runs
the worker. Production settings are checked at start-up: `ENVIRONMENT=production` requires S3 storage,
ClamAV, SMTP and a separate migration role. Release procedure: LLD 19.1. The hosting target in a UK region
is pending ESE decision D-02, so infrastructure-as-code is not yet in this repository.
