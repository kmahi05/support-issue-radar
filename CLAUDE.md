# CLAUDE.md — Support Issue Radar

## Project summary
Support Issue Radar ingests support tickets from any helpdesk (HMAC-signed webhook or CSV upload), cleans and PII-redacts them at ingest, has an LLM rewrite each one into a normalized `TicketSignal`, embeds that summary, and assigns the ticket to an *issue* by nearest-neighbor voting in ChromaDB with fixed thresholds (the LLM only decides the gray zone). On top of that grouping, plain SQL/Python computes an impact score, spike alerts, release correlation and nightly merge suggestions. Leads, PMs and engineers work from one ranked issue list in a Next.js app.
**`PRD.md` is the source of truth.** Cite sections by number (e.g. "§3.2") and do not copy them here. If code and PRD disagree, follow the PRD, or ask before you change behavior. `TASKS.md` is the build plan.

## Stack
- **web/**: Next.js 15 App Router, TypeScript, Tailwind v4, Recharts (added by the chart task). Port 3003. All data through FastAPI (§2 Frontend).
- **api/**: FastAPI, Python 3.12 (uv, `.python-version`), Pydantic v2, SQLAlchemy 2.0 async + asyncpg (raw SQL via `text()`), argon2-cffi, PyJWT, cryptography (Fernet), slowapi, chromadb, `langchain-google-genai`. Port 8003.
- **db/**: PostgreSQL 16 on `localhost:5432` (any install method). Plain SQL migrations in `db/migrations/`, applied by `scripts/migrate.py`.
- Models (Gemini only): `GEMINI_MODEL` = `gemini-2.5-flash` (signals; the PRD's "cheap model"), `GEMINI_MODEL_STRONG` = `gemini-2.5-pro` (gray zone, titles, replies), `GEMINI_EMBEDDING_MODEL` = `models/gemini-embedding-001`.

## Repo layout
```
db/migrations/0001_init.sql   # full §3.1 schema, RLS, grants, security definer functions
scripts/
  db_setup.py                 # idempotent: roles sir_owner/sir_app/sir_worker + support_radar(+_test)
  migrate.py                  # applies db/migrations in order, tracked in schema_migrations
  make_demo.py, load_demo.py, evaluate.py, burst.py   # later tasks; demo_data/ is git-ignored
api/
  pyproject.toml, .python-version
  app/
    main.py          # app factory, lifespan (engines, Chroma, embedding-model check), CORS, limiter, /health
    config.py        # pydantic-settings Settings; all env vars read here only
    db.py            # init_engines(), user_session(user_id), worker_session()
    errors.py        # ApiError + handlers -> {"error": {"code", "message"}}
    ratelimit.py     # slowapi Limiter
    crypto.py        # Fernet encrypt/decrypt for *_encrypted bytea columns
    storage.py       # local uploads under STORAGE_DIR/{org_id}/... (CSV imports)
    llm.py           # chat_model(strong), embeddings(), set_overrides() for tests
    vectorstore.py   # Chroma client, tickets/issues collections, tenant_where()
    auth/            # passwords.py, tokens.py, deps.py (get_current_user), router.py (/auth/*, /me)
    orgs/router.py   # POST/GET /orgs, GET/POST /orgs/{id}/members
    <resource>/router.py   # later routers follow the same package-per-resource pattern
                           # (ingest, imports, customers, releases, jobs, tickets, search, issues,
                           #  alerts, merge_suggestions, reports, integrations, admin)
    services/        # later: cleaning, ingest, understand, embed, assign, pipeline, titles,
                     # issue_ops, analytics, alerts, slack, merge_suggest, repair, retention,
                     # imports, reports, scheduler (pure logic + SQL, no HTTP)
    clustering.py    # later: threshold constants + pure vote()/decide() (§3.2)
  tests/             # pytest; conftest.py recreates support_radar_test and injects fakes
web/
  app/login, app/register      # Server Action forms
  app/(app)/layout.tsx, page.tsx   # authenticated layout (email + logout), org list + create form
  app/actions.ts               # Server Actions (verbNounAction)
  components/                  # AuthForm, CreateOrgForm; later IssuesTable, TicketTimeline, ...
  lib/api.ts                   # server-only apiFetch (Bearer, one refresh + retry)
  lib/session.ts               # cookie names, setTokenCookies, refreshTokens
  middleware.ts                # redirect to /login without session; refresh expired access cookie
```

## Commands (work in Git Bash and PowerShell)
Setup (once, in order):
```
cp .env.example api/.env         # fill APP_ENCRYPTION_KEY (generator command in the file)
cp web/.env.example web/.env.local
cd api
uv sync
uv run python ../scripts/db_setup.py
uv run python ../scripts/migrate.py
cd ../web
npm install
```
Run:
- API: `cd api; uv run uvicorn app.main:app --reload --port 8003`
- Web: `cd web; npm run dev` (http://localhost:3003)
- Postgres check: `pg_isready -h localhost -p 5432` (portable install here: `C:\Users\meetp\pgsql\bin\pg_isready.exe`)

Tests, lint, format:
- API all: `cd api; uv run pytest -q` (drops and recreates `support_radar_test`; DB tests skip with "PostgreSQL not reachable at <url>" if Postgres is down)
- Single test: `uv run pytest tests/test_auth.py::test_refresh_rotation_and_reuse_detection -q`
- API lint/format: `uv run ruff check . ../scripts` and `uv run ruff format --check . ../scripts`
- Web: `npm run lint`, `npm run typecheck`, `npm run build`

Database:
- New migration: add `db/migrations/NNNN_snake_name.sql` (next number), then `uv run python ../scripts/migrate.py`. Never edit an applied migration.
- Target the test DB: `uv run python ../scripts/migrate.py --database-url postgresql://sir_owner:sir_owner@localhost:5432/support_radar_test`

Demo dataset (later tasks, run from `api/`):
- Generate: `uv run python ../scripts/make_demo.py --seed 42 --out ../scripts/demo_data` (dated relative to `--now`, default now)
- Load: `uv run python ../scripts/load_demo.py --direct` or `--via-api`
- Evaluate: `uv run python ../scripts/evaluate.py`

## Environment variables (names only; `.env.example` lists them all with dev defaults)
API (`api/.env`):
- `APP_DATABASE_URL` (sir_app, RLS), `WORKER_DATABASE_URL` (sir_worker, BYPASSRLS)
- `PG_SUPERUSER_URL` (db_setup + tests only), `MIGRATE_DATABASE_URL` (sir_owner)
- `JWT_SECRET` (≥ 32 bytes, the API refuses to start otherwise), `ACCESS_TOKEN_TTL_SECONDS`, `REFRESH_TOKEN_TTL_SECONDS`, `INVITE_TTL_SECONDS`
- `APP_ENCRYPTION_KEY` (Fernet key for `*_encrypted` columns)
- `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GEMINI_MODEL_STRONG`, `GEMINI_EMBEDDING_MODEL`, `LLM_COST_PER_1K_TICKETS_USD`
- `CHROMA_PATH`, `STORAGE_DIR`, `MAX_CSV_UPLOAD_BYTES`
- `WEB_ORIGIN` (the only CORS origin), `RATE_LIMIT_ENABLED`, `LOG_LEVEL`
Web (`web/.env.local`): `API_URL` (server-only FastAPI base URL).
Org secrets (ingest HMAC secret, Slack webhook URL) are never env vars: they are stored Fernet-encrypted in `integrations`.

## Architecture invariants (never break these)
1. Three DB roles: `sir_owner` (migrations), `sir_app` (user requests, RLS applies, not owner, no BYPASSRLS), `sir_worker` (BYPASSRLS: webhooks, pipeline, scheduler, auth routes). Every user-facing read/write runs in `db.user_session(user_id)`. Every `sir_worker` statement filters by an explicit `org_id` parameter.
2. Orgs are created only by `public.create_org()` (security definer). Never insert into `organizations` from app code.
3. Verify the HMAC on the raw body (`await request.body()`) with `hmac.compare_digest` before parsing JSON or touching the DB. Bad signature → 401, no writes. Accept the previous secret only until `prev_ingest_secret_expires_at`.
4. Clean and redact PII at ingest, before the upsert. Store only `body_redacted` and `body_hash`; never store or log the raw body. The LLM and the embedder only see `body_redacted` or the summary.
5. Run assignment (§3.2) inside `pg_advisory_xact_lock(hashtextextended(org_id, 0))` in one transaction, including new-issue `number` allocation, the provisional title and the regression check. `title_chain` and Chroma upserts run after commit.
6. Keep LLM calls for understanding outside the lock and parallel (`Semaphore(8)`). Only gray-zone calls run inside the lock, and not when the org is overloaded (§3.2).
7. Thresholds decide. Use the constants in `app/clustering.py` (0.90 / 0.82 + 2 votes / 0.70). Do not hardcode them anywhere else.
8. The gray-zone LLM may only pick a candidate ID it was given. Any other UUID → `NEW`, logged with the reason.
9. Impact score, spike counts, baselines and release correlation come from SQL/Python only. Never ask an LLM for a number.
10. Draft replies only when the issue has a human-written workaround. The reply prompt gets only that workaround and the ticket.
11. Ticket text is untrusted: wrap it in delimiters in every prompt. Chains have no tools.
12. RLS is enabled on every table. Every new table ships with policies, minimal `sir_app` grants and a pytest RLS test in the same change.
13. Every Chroma query, update or delete builds its `where` with `vectorstore.tenant_where(org_id, ...)`. No raw `where` dicts anywhere else.
14. Postgres is the source of truth. Chroma is an index that `POST /admin/reindex` can rebuild. Write Postgres first, commit, then upsert Chroma.
15. Only one scheduler runs: hold `pg_try_advisory_lock(7265001)` on a dedicated worker connection, never a pooled one.
16. Never overwrite `assignment = 'manual'` automatically. Reprocessing keeps it unless `force=true`.
17. User merge/split/move run in the RLS user session; their `issue_events` rows and counters go through `public.log_issue_event()` and `public.recompute_issue_counters()` in the same transaction (§3.1 "Which code writes"). Never add a user write policy on `issue_events`, `alerts` or `jobs`.
18. Follow `merged_into` to the live issue (max depth 5) everywhere an issue ID is resolved.
19. Customer names and ARR come from the `customers` table, never from ticket text. Unknown customers count toward tickets, not customers or ARR.
20. Gemini only (`app/llm.py` factories). No OpenAI or Anthropic packages. Tests never call Gemini.
21. Secrets (`*_encrypted` columns) are encrypted with `app/crypto.py` and never returned by an endpoint; a new ingest secret is shown once.
22. Local file storage is a shortcut (`# shortcut: local disk storage, single API server; ...` in `app/storage.py`): uploads stream in 1 MB chunks with a size cap and an extension/MIME whitelist; downloads go through FastAPI after a membership check.
23. Prefer split when unsure. Do not lower thresholds without an evaluation run (§5 Phase 4 targets).

## Coding conventions
- API: every endpoint is `async def`; every DB call uses SQLAlchemy 2.0 async with `text()` SQL. No ORM models.
- Routes are thin: parse → authorize → call one `services/` function → return. Business logic lives in `services/` and `clustering.py`.
- Authorization: `orgs.router.require_role(conn, org_id, allowed)` in the user session. Non-members get 404, too-low roles 403.
- Deterministic logic (cleaning, Luhn, vote/decide, impact, spike, release correlation, merge-chain resolution, backoff delay) is pure functions with no I/O.
- Pydantic v2: `model_config = ConfigDict(extra="forbid")` on request models; money is `Decimal` serialized as a string (`"120000.00"`).
- Error response shape for every non-2xx: `{"error": {"code": "snake_case_code", "message": "Human text"}}`. Raise `ApiError(status, code, message)`.
- Timestamps are `timestamptz` UTC; convert to the org timezone only for `alert_date` and daily buckets.
- Naming: snake_case Python and SQL; tables plural; services export verbs (`assign_ticket`, `merge_issues`); React components PascalCase; Server Actions `verbNounAction`.
- Config only via `app/config.py` `get_settings()`; no `os.environ` elsewhere in `app/`.
- Web: Server Components read through `lib/api.ts`; all writes are Server Actions that call FastAPI. Client Components only for forms with state, charts, dialogs and polling. Next never talks to Postgres.
- Hide controls the role cannot use, but the API and RLS still enforce every rule.
- Relative imports inside `app/`. Each router module exposes `router`.

## Testing rules
- Tests run against a real Postgres test DB `support_radar_test`, recreated once per session by `tests/conftest.py` (via `PG_SUPERUSER_URL` + `scripts/db_setup.py` + `scripts/migrate.py`). The `client` fixture is an `httpx.AsyncClient` on `ASGITransport` with the app lifespan running.
- RLS tests are pytest tests (no pgTAP): connect as `sir_app`, `set_config('app.user_id', ...)` in a transaction, assert the visible rows; also assert `sir_worker` sees every org.
- Fakes: `llm.set_overrides()` with `FakeListChatModel` / `DeterministicFakeEmbedding`; Slack faked; Chroma in a temp dir. Tests never call real providers.
- Use unique emails/org names per test (no cleanup needed). Pure tests must not depend on the `database` fixture.
- Must-have test lists per area are in TASKS.md (`Done when`).
- Before you tick a task: `uv run ruff check . ../scripts`, `uv run ruff format --check . ../scripts`, `uv run pytest -q`, and for web work `npm run lint`, `npm run typecheck`, `npm run build`.

## Do not
- Do not parse the ingest JSON (or let FastAPI bind a body model) before verifying the HMAC.
- Do not put the advisory lock around the understanding call, `title_chain` or Chroma upserts after commit.
- Do not use `pg_advisory_lock` (session) for assignment. Use the `_xact_` variant.
- Do not filter Chroma on `issue_id == ""`. Use `has_issue`.
- Do not trust IDs from the client without loading them through the RLS session (cross-org IDs must 404).
- Do not store plaintext secrets in tables, env files for orgs, or logs.
- Do not compute counters only in Chroma, and do not read counts from Chroma.
- Do not add server push for the dashboard. Poll every 30 s (§6).
- Do not add Celery, Redis or a queue. Use the in-process scheduler and BackgroundTasks (§6).
- Do not run real LLM calls in CI. A full demo run costs money, so run it only on purpose.
- Do not use bash-only syntax (`export X=`, `&&` chains that break in PowerShell 5.1) in documented commands. Use `;` or one command per line.

## Workflow
- Work through `TASKS.md` from top to bottom. Tasks are ordered by dependency. Do not start a task whose `Depends on` boxes are unticked.
- A task is done only when its `Done when` check passes. Then tick its box (`- [x]`) in the same change.
- If a task reveals missing work, add a new task in dependency order with the next free ID in that phase. Do not renumber existing tasks.
- When a convention, command or invariant changes, update this file in the same change.
- When the PRD is ambiguous, pick the smallest choice that keeps every invariant, and write it down in the task's notes.
