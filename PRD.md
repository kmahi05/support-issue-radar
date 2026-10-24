# Support Issue Radar - Product Requirements Document

## 1. Executive Summary & Value Proposition

### Elevator pitch
A system that reads every incoming support ticket and groups tickets that describe the same underlying problem into one *issue*. It ranks issues by real business impact (how many customers, how much revenue), spots sudden spikes, and links a spike to the release that probably caused it. Support, product and engineering see one shared, ranked list of what is actually hurting customers.

### Target persona and friction
- **Primary persona:** A support lead at a B2B SaaS company with 10–60 agents, receiving 500–5,000 tickets per week by email, chat and web form.
- **Secondary personas:** A product manager who decides what to fix next, and an engineering manager who wants to hear about breakages early.
- **Friction:**
  1. The same bug arrives 40 times in 40 different wordings ("export button does nothing", "CSV download stuck", "can't get my report out"). Every agent investigates it from scratch.
  2. Nobody can answer "what are our top 10 problems this week, and how many paying customers do they hit?" Tags are applied inconsistently, and keyword reports miss paraphrases.
  3. A bad release breaks something on Tuesday morning. Engineering hears about it on Thursday, after 200 tickets and an angry escalation from the biggest customer.
  4. When a fix ships, nobody knows which customers to tell.
- **Outcome:** Every ticket is linked to an issue within ~10 seconds of arriving. The dashboard ranks issues by an explainable impact score. A Slack alert fires when an issue spikes ("Issue #142 *CSV export hangs*: 23 tickets in 24 h vs a baseline of 1.2/day; started 2 h after web v4.12.0; 91% of tickets on 4.12.0"). Agents see the known workaround on matching tickets. When the issue is resolved, the affected-customer list is one click away.

### Technical selling points for interviews
1. **Online clustering with a gray zone.** Each new ticket is placed by nearest-neighbor voting in ChromaDB with fixed similarity thresholds. Only uncertain cases (the gray zone) go to the LLM, which picks between at most 3 candidate issues or "new issue". That keeps cost and latency low and makes behavior predictable.
2. **Embed the meaning, not the noise.** The LLM first rewrites each ticket into a short normalized problem statement (symptom, product area, error code, platform). The embedding is of that statement, not the raw email with greetings, signatures and quoted replies, so paraphrases land close together.
3. **Race-free cluster creation.** Two tickets about a brand-new bug that arrive in the same second must not create two issues. Assignment runs inside a per-organization Postgres advisory lock (`pg_advisory_xact_lock`). This is a concrete check-then-write race, and the solution needs no queue.
4. **Deterministic analytics on top of AI grouping.** Impact score, spike detection (count vs baseline, Poisson-style) and release correlation are plain SQL and Python formulas, so they are explainable and testable.
5. **Human corrections as first-class data.** Merge, split and move are audited operations. They update Postgres and Chroma metadata, and they feed the evaluation set used to tune thresholds.
6. **Regression detection.** A new ticket that matches an issue already marked *resolved* raises a "possible regression" flag instead of being silently filed.

---

## 2. End-to-End System Architecture

### Frontend — Next.js 15 (App Router, TypeScript, Tailwind v4), port 3003
- **All data goes through FastAPI.** Next never talks to Postgres. `lib/api.ts` (`import "server-only"`) adds the Bearer access token, and on a 401 refreshes once, updates the cookies and retries.
- **Auth pages:** `/login` and `/register` use Server Actions that call the API. The access and refresh tokens live in httpOnly cookies set by Next (`sameSite: "lax"`, `secure` in production). `middleware.ts` redirects to `/login` without a session cookie and refreshes the access token when its cookie has expired.
- **Server Components** (each reads one FastAPI endpoint from §4):
  - Issues dashboard: issues ranked by impact, with filters for status, product area, kind and platform.
  - Issue detail page: summary, daily ticket chart, app-version breakdown, release correlation, workaround, linked tickets, affected customers.
  - Alerts feed.
  - Weekly report page.
  - Merge suggestions inbox.
  - Settings: integrations, releases, team, customers, failed tickets.
- **Server Actions** (all call FastAPI): log in, register, log out, create an org, change issue status, edit title/workaround/external link, merge issues, split tickets into a new issue, move a ticket, accept or dismiss merge suggestions, add a release, rotate the ingest secret, invite a member, upload a ticket CSV, retry failed tickets.
- **Client Components:**
  - `TicketTimeline`: a Recharts bar chart of tickets per day, with release markers.
  - `VersionBreakdown`: a bar chart of `app_version` shares.
  - `MergeDialog`: search for the target issue and preview the combined counts.
  - `SplitDialog`: tick tickets to move into a new issue.
- **Live updates:** the dashboard polls `GET /issues` every 30 s. Issue rankings change slowly, so polling is enough (see §6).

### Backend API — FastAPI (Python 3.12, Pydantic v2), port 8003
- `async def` endpoints, SQLAlchemy 2.0 async with `asyncpg`, raw SQL via `text()`.
- **Auth:** own accounts with email + password (argon2id) and standard JWTs. Access token: HS256, 15 min, `iss=support-radar`, `aud=support-radar-api`. Refresh token: opaque, stored as a sha256 hash, 14 days, rotated on every use with reuse detection. No OAuth or magic links in v1.
- **Three database roles** (§3.1): `sir_app` for requests made on behalf of a user (RLS applies; each request is one transaction that first runs `select set_config('app.user_id', :uid, true)`), `sir_worker` (BYPASSRLS) for the ingest webhook, the pipeline, the scheduler and the auth routes (every worker statement filters by an explicit `org_id`), and `sir_owner` for migrations.
- **Ingestion endpoint** `POST /api/v1/ingest/{org_id}/tickets`:
  - Authenticated by HMAC-SHA256 of the raw body with the org's ingest secret (header `X-Radar-Signature: sha256=<hex>`), compared with `hmac.compare_digest`. After a rotation, the previous secret is also accepted until `prev_ingest_secret_expires_at` (24 h).
  - Batches of up to 100 tickets.
  - **Clean at ingest:** before the upsert, each body is cleaned and PII-redacted (strip quoted replies and signatures, redact emails, phone numbers and Luhn-valid card numbers, truncate to 4,000 characters; §3.4). Only `body_redacted` and `body_hash` (sha256 of the raw body) are stored; the raw body is never written anywhere.
  - Idempotent upsert on `(org_id, external_id)`. Any helpdesk (Zendesk triggers, Intercom webhooks, a custom form) can post here through a small adapter or an automation tool.
- **Processing pipeline per ticket** (a `BackgroundTasks` job that processes the org's due pending tickets in order):
  1. **Understand:** LLM structured output on `body_redacted` → `TicketSignal` (kind, normalized summary, product area, symptom, error code, severity, platform, app version if mentioned).
  2. **Embed** the summary.
  3. **Assign** inside the per-org advisory lock: nearest-neighbor vote → auto / LLM gray zone / new issue (a new issue gets a provisional title).
  4. **Update** the issue's counters, and run the regression check (same transaction).
  5. **After commit:** upsert the Chroma vectors, and run `title_chain` for new issues and at the 5- and 20-ticket marks.
- **Scheduled loop** (in-process asyncio task started in the FastAPI lifespan): every 15 min spike detection, new-high-impact detection and impact score refresh; hourly Chroma repair; nightly merge suggestions and retention. Only one API process runs it, guarded by `pg_try_advisory_lock(7265001)` on a dedicated worker connection.
- **Outgoing Slack alerts** via an incoming-webhook URL stored encrypted (`integrations.slack_webhook_encrypted`).
- **Secrets at rest:** `app/crypto.py` encrypts the ingest HMAC secret and the Slack webhook URL with Fernet (`APP_ENCRYPTION_KEY`) into `bytea` columns. The ingest secret must be readable to verify signatures, so it is encrypted rather than hashed. No endpoint returns a stored secret; a new ingest secret is shown once on create/rotate.
- **File uploads:** CSV imports are uploaded to FastAPI and written to local disk under `STORAGE_DIR/{org_id}/imports/` (`app/storage.py`, streamed in 1 MB chunks with a size cap). This is a single-server shortcut; move to S3-compatible storage before running 2+ API instances.

### Relational DB — PostgreSQL 16
- Roles in an org: `admin`, `lead`, `agent`, `viewer` (PM / engineering, read-only).
- Tables: users, refresh tokens, invites, organizations, org members, customers (with ARR, annual recurring revenue), tickets, issues, issue events (audit), releases, alerts, merge suggestions, integrations, jobs.
- RLS on every table. `app.current_user_id()` reads the `app.user_id` setting of the current transaction.
- Orgs are created only by the `security definer` function `public.create_org()`, which also adds the caller as `admin` and stores the encrypted ingest secret.
- Daily aggregates use plain indexed SQL (`date_trunc('day', source_created_at)`) and need no extra analytics store at this volume.

### RAG & Orchestration — LangChain + Gemini
- **Embeddings:** `GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL)`, default `models/gemini-embedding-001`.
- **Models** (`langchain-google-genai`, `temperature=0`, key `GOOGLE_API_KEY`):
  - `GEMINI_MODEL` (default `gemini-2.5-flash`) for per-ticket understanding (high volume, low cost).
  - `GEMINI_MODEL_STRONG` (default `gemini-2.5-pro`) for gray-zone decisions, issue titles and suggested replies (low volume, needs judgment).
- **Chains:**
  - `signal_chain`: `body_redacted` (inside delimiters) + the org's `product_areas` → `with_structured_output(TicketSignal)`.
  - `gray_zone_chain`: ticket summary + ≤ 3 candidate issues (title, summary, 3 example ticket summaries each) → `with_structured_output(GrayZoneDecision)`.
  - `title_chain`: summaries of the first 1–5 tickets of an issue → a title (≤ 80 chars) + summary (≤ 400 chars). It never runs inside the advisory lock: a new issue is created with a provisional title (the first ticket's summary cut to 80 chars) and `title_chain` runs after the assignment commits. It re-runs when the issue reaches 5 and 20 tickets, so the title improves as evidence grows.
  - `reply_chain`: ticket + the human-written workaround → a draft reply. Only runs if the issue has a workaround; it never invents a fix.

### Vector Store — ChromaDB
- `PersistentClient(path=CHROMA_PATH)` (default `./data/chroma`; `/data/chroma` on a persistent volume in deploy).
- Each collection's metadata records the embedding model; the API refuses to start if it differs from `GEMINI_EMBEDDING_MODEL`.
- Chroma has no RLS, so every `where` is built by one helper, `tenant_where(org_id, **conditions)`, which always ANDs the org filter.
- Collection `tickets`: one vector per ticket summary, used for neighbor voting and search.
- Collection `issues`: one vector per issue, used for gray-zone candidates, issue search and merge suggestions.

### Data flow

```
Helpdesk (Zendesk / Intercom / form)
   │ POST /api/v1/ingest/{org}/tickets  [HMAC signed, ≤100 per batch]
   ▼
┌──────────────────────── FastAPI (sir_worker) ─────────────────────┐
│ verify HMAC (current or unexpired previous secret)                 │
│ clean + redact PII → upsert body_redacted (org_id, external_id)    │
│ unchanged body_hash? → skip.   else status=pending → BackgroundTask│
└───────────────────────────────┬───────────────────────────────────┘
                                ▼
 ┌─────────────────────────── process(ticket) ──────────────────────────────┐
 │ 1 LLM (GEMINI_MODEL) → TicketSignal {kind, summary, area, symptom, …}    │
 │    kind ∈ {billing, other} → assignment=skipped, done                    │
 │ 2 embed(summary)                                                         │
 │ 3 BEGIN; pg_advisory_xact_lock(org)                                      │
 │      Chroma tickets.query(n=10, tenant_where(org, assigned, last 90 d))  │
 │      vote by issue ─┬─ best ≥ 0.82 (+support) ──▶ auto assign            │
 │                     ├─ 0.70–0.82 ─▶ Chroma issues top-3 ─▶ LLM (STRONG)  │
 │                     │                   └─▶ pick issue | NEW             │
 │                     └─ < 0.70 ───────────────────────────▶ NEW issue     │
 │      NEW issue: provisional title = summary[:80]                         │
 │      update tickets.issue_id, issues counters; regression check          │
 │    COMMIT                                                                │
 │ 4 Chroma upsert ticket vector (metadata issue_id); new issue → upsert    │
 │ 5 title_chain (GEMINI_MODEL_STRONG) for new issues / 5 / 20 tickets      │
 └──────────────────────────────────────────────────────────────────────────┘
                                ▼
 every 15 min (one process, pg_try_advisory_lock):
   SQL: counts 24 h vs 14-day baseline → spike? → alerts row (unique per day) → Slack webhook
   SQL: impact_score refresh;  hourly: Chroma repair;  nightly: merge_suggestions, retention

Browser (Next.js) ── Server Components → lib/api.ts → FastAPI (sir_app, RLS) ── all reads
                  ── Server Actions   → lib/api.ts → FastAPI (sir_app, RLS) ── all writes
```

---

## 3. Database Schema & Vector Specifications

### 3.1 PostgreSQL DDL

PostgreSQL 16. `db/migrations/0001_init.sql` is this DDL verbatim (applied by `scripts/migrate.py` as `sir_owner`). `scripts/db_setup.py` creates the three login roles first: `sir_owner` (owns all tables), `sir_app` (not owner, no BYPASSRLS, used for user requests) and `sir_worker` (BYPASSRLS, used by the ingest webhook, the pipeline, the scheduler and the auth routes).

```sql
-- 0001_init: full schema (PRD §3.1). Applied by scripts/migrate.py as sir_owner.
-- Roles sir_app (RLS applies) and sir_worker (BYPASSRLS) are created by scripts/db_setup.py.

-- ============ Session identity ============
create schema if not exists app;
create function app.current_user_id() returns uuid language sql stable as $$
  select nullif(current_setting('app.user_id', true), '')::uuid
$$;
grant usage on schema app to sir_app, sir_worker;

-- ============ Auth (own JWTs, PRD §4 Auth) ============
create table public.users (
  id            uuid primary key default gen_random_uuid(),
  email         text not null,
  password_hash text not null,
  display_name  text not null,
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);
create unique index users_email_lower on public.users (lower(email));

create table public.refresh_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  token_hash  char(64) not null unique,          -- sha256 hex of the opaque token
  expires_at  timestamptz not null,
  revoked_at  timestamptz,
  replaced_by uuid references public.refresh_tokens(id),
  created_at  timestamptz not null default now()
);
create index refresh_tokens_user_idx on public.refresh_tokens(user_id);

-- ============ Tenancy ============
create table public.organizations (
  id             uuid primary key default gen_random_uuid(),
  name           text not null check (char_length(btrim(name)) between 1 and 120),
  timezone       text not null default 'UTC',
  product_areas  text[] not null default '{}',    -- "the org's area list" used by signal_chain (§3.5)
  retention_days int not null default 730 check (retention_days >= 30),
  created_at     timestamptz not null default now()
);

create table public.org_members (
  org_id     uuid not null references public.organizations(id) on delete cascade,
  user_id    uuid not null references public.users(id) on delete cascade,
  role       text not null default 'agent' check (role in ('admin','lead','agent','viewer')),
  created_at timestamptz not null default now(),
  primary key (org_id, user_id)
);
create index org_members_user_idx on public.org_members(user_id);

create table public.invites (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organizations(id) on delete cascade,
  email       text not null,
  role        text not null check (role in ('admin','lead','agent','viewer')),
  token_hash  char(64) not null unique,
  invited_by  uuid references public.users(id) on delete set null,
  expires_at  timestamptz not null,
  accepted_at timestamptz,
  created_at  timestamptz not null default now()
);
create index invites_org_idx on public.invites(org_id);

create function public.org_role(p_org uuid)
returns text language sql stable security definer set search_path = '' as $$
  select role from public.org_members where org_id = p_org and user_id = (select app.current_user_id());
$$;

-- Secrets are Fernet-encrypted by app/crypto.py (APP_ENCRYPTION_KEY); never returned by an endpoint.
create table public.integrations (
  org_id                        uuid primary key references public.organizations(id) on delete cascade,
  ingest_secret_encrypted       bytea not null,
  prev_ingest_secret_encrypted  bytea,               -- still accepted until prev_ingest_secret_expires_at
  prev_ingest_secret_expires_at timestamptz,
  slack_webhook_encrypted       bytea,               -- optional
  high_impact_arr               numeric(14,2) not null default 50000,  -- ARR threshold for new-issue alerts
  updated_at                    timestamptz not null default now(),
  check ((prev_ingest_secret_encrypted is null) = (prev_ingest_secret_expires_at is null))
);

-- ============ Customers ============
create table public.customers (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organizations(id) on delete cascade,
  external_id text not null,                     -- helpdesk organization / account id
  name        text not null,
  plan        text,
  arr         numeric(14,2) not null default 0 check (arr >= 0),
  created_at  timestamptz not null default now(),
  unique (org_id, external_id),
  unique (org_id, id)                            -- target of same-org composite FKs
);

-- ============ Releases ============
create table public.releases (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organizations(id) on delete cascade,
  platform    text not null check (platform in ('web','ios','android','desktop','api')),
  version     text not null,
  released_at timestamptz not null,
  notes       text,
  unique (org_id, platform, version)
);
create index releases_org_time_idx on public.releases(org_id, released_at desc);

-- ============ Issues ============
create table public.issues (
  id                  uuid primary key default gen_random_uuid(),
  org_id              uuid not null references public.organizations(id) on delete cascade,
  number              int  not null,             -- human id "#142", assigned under the org lock
  kind                text not null check (kind in ('bug','question','feature_request')),
  title               text not null check (char_length(title) <= 80),
  summary             text not null check (char_length(summary) <= 400),
  product_area        text,
  status              text not null default 'new'
                      check (status in ('new','investigating','fix_in_progress','resolved','wont_fix','merged')),
  workaround          text check (char_length(workaround) <= 2000),
  external_url        text,                      -- Jira / Linear / GitHub issue
  merged_into         uuid,
  possible_regression boolean not null default false,
  first_seen_at       timestamptz not null,
  last_seen_at        timestamptz not null,
  resolved_at         timestamptz,
  ticket_count        int not null default 0,
  customers_7d        int not null default 0,
  arr_affected_7d     numeric(14,2) not null default 0,
  tickets_7d          int not null default 0,
  tickets_prev_7d     int not null default 0,
  impact_score        smallint not null default 0 check (impact_score between 0 and 100),
  title_ticket_count  int not null default 0,    -- ticket_count when the title was last generated
  created_at          timestamptz not null default now(),
  unique (org_id, number),
  unique (org_id, id),
  foreign key (org_id, merged_into) references public.issues(org_id, id),
  check (status <> 'merged' or merged_into is not null),
  check (merged_into is null or merged_into <> id)
);
create index issues_rank_idx on public.issues(org_id, status, impact_score desc);
create index issues_last_seen_idx on public.issues(org_id, last_seen_at desc);

-- ============ Tickets ============
create table public.tickets (
  id                uuid primary key default gen_random_uuid(),
  org_id            uuid not null references public.organizations(id) on delete cascade,
  external_id       text not null,
  customer_id       uuid,
  channel           text not null check (channel in ('email','chat','form','api')),
  subject           text,                        -- PII-redacted at ingest
  body_redacted     text not null,               -- cleaned + PII-redacted at ingest; the raw body is never stored
  body_hash         char(64) not null,           -- sha256 of the raw body, to skip unchanged updates
  source_created_at timestamptz not null,
  platform          text check (platform in ('web','ios','android','desktop','api')),
  app_version       text,
  -- AI understanding
  kind              text check (kind in ('bug','question','feature_request','billing','other')),
  summary           text check (char_length(summary) <= 300),
  product_area      text,
  symptom           text,
  error_code        text,
  severity          text check (severity in ('low','medium','high','critical')),
  -- clustering
  issue_id          uuid,
  assignment        text check (assignment in ('auto','llm','manual','new_issue','skipped')),
  similarity        real check (similarity between -1 and 1),
  -- processing state
  status            text not null default 'pending' check (status in ('pending','processing','processed','failed')),
  attempts          smallint not null default 0, -- failed attempts so far (§6 backoff)
  next_attempt_at   timestamptz,                 -- null = due now
  error             text,
  processed_at      timestamptz,
  created_at        timestamptz not null default now(),
  unique (org_id, external_id),
  foreign key (org_id, customer_id) references public.customers(org_id, id) on delete set null (customer_id),
  foreign key (org_id, issue_id) references public.issues(org_id, id) on delete set null (issue_id)
);
create index tickets_issue_time_idx on public.tickets(issue_id, source_created_at desc);
create index tickets_org_time_idx on public.tickets(org_id, source_created_at desc);
create index tickets_pending_idx on public.tickets(org_id, source_created_at) where status = 'pending';
create index tickets_processing_idx on public.tickets(org_id) where status = 'processing';
create index tickets_failed_idx on public.tickets(org_id, created_at desc) where status = 'failed';

-- ============ Audit, alerts, suggestions, jobs ============
create table public.issue_events (
  id         bigint generated always as identity primary key,
  org_id     uuid not null references public.organizations(id) on delete cascade,
  issue_id   uuid not null,
  actor_id   uuid references public.users(id) on delete set null,  -- null = system
  action     text not null check (action in ('created','merged','split','ticket_moved','status_changed',
                                             'edited','regression_flagged','title_regenerated')),
  details    jsonb not null default '{}',
  created_at timestamptz not null default now(),
  foreign key (org_id, issue_id) references public.issues(org_id, id) on delete cascade
);
create index issue_events_issue_idx on public.issue_events(issue_id, created_at desc);

create table public.alerts (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organizations(id) on delete cascade,
  issue_id    uuid not null,
  kind        text not null check (kind in ('spike','new_high_impact','regression')),
  alert_date  date not null,                     -- in org timezone; one alert per issue/kind/day
  observed    int not null,
  baseline    real not null,
  message     text not null,
  sent_at     timestamptz,
  created_at  timestamptz not null default now(),
  unique (issue_id, kind, alert_date),
  foreign key (org_id, issue_id) references public.issues(org_id, id) on delete cascade
);
create index alerts_org_time_idx on public.alerts(org_id, created_at desc);

create table public.merge_suggestions (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null references public.organizations(id) on delete cascade,
  issue_a    uuid not null,
  issue_b    uuid not null,
  similarity real not null,
  status     text not null default 'open' check (status in ('open','accepted','dismissed')),
  decided_by uuid references public.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (issue_a, issue_b),
  check (issue_a < issue_b),                     -- one row per unordered pair
  foreign key (org_id, issue_a) references public.issues(org_id, id) on delete cascade,
  foreign key (org_id, issue_b) references public.issues(org_id, id) on delete cascade
);
create index merge_suggestions_open_idx on public.merge_suggestions(org_id, created_at desc) where status = 'open';

create table public.jobs (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organizations(id) on delete cascade,
  kind        text not null check (kind in ('process_batch','csv_import','customer_import','reindex','repair')),
  status      text not null default 'queued' check (status in ('queued','running','succeeded','failed')),
  total       int not null default 0,
  done        int not null default 0,
  error       text,
  created_at  timestamptz not null default now(),   -- = start time; the repair marker (§5 Phase 4)
  finished_at timestamptz
);
create index jobs_org_kind_idx on public.jobs(org_id, kind, created_at desc);

-- ============ Security definer functions (owned by sir_owner) ============
-- The only way to create an org: inserts the org, the caller's admin membership and the integrations row.
create function public.create_org(p_name text, p_timezone text, p_ingest_secret_encrypted bytea)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := app.current_user_id();
  v_org uuid;
begin
  if v_uid is null then
    raise exception 'create_org requires app.user_id' using errcode = '42501';
  end if;
  insert into public.organizations (name, timezone) values (btrim(p_name), p_timezone) returning id into v_org;
  insert into public.org_members (org_id, user_id, role) values (v_org, v_uid, 'admin');
  insert into public.integrations (org_id, ingest_secret_encrypted) values (v_org, p_ingest_secret_encrypted);
  return v_org;
end $$;

-- User-triggered merge / split / move (§3.1 "Which code writes"): the route changes tickets/issues through
-- RLS in the user session, then writes the audit row and the counters through these two functions in the
-- same transaction. They re-check the caller's role, so no user write policy exists on issue_events.
create function public.log_issue_event(p_issue_id uuid, p_action text, p_details jsonb default '{}')
returns void language plpgsql security definer set search_path = '' as $$
declare
  v_org  uuid;
  v_role text;
begin
  select org_id into v_org from public.issues where id = p_issue_id;
  v_role := public.org_role(v_org);
  if v_org is null or v_role is null or v_role = 'viewer'
     or p_action not in ('merged','split','ticket_moved','status_changed','edited')
     or (v_role = 'agent' and p_action <> 'ticket_moved') then
    raise exception 'log_issue_event not allowed' using errcode = '42501';
  end if;
  insert into public.issue_events (org_id, issue_id, actor_id, action, details)
  values (v_org, p_issue_id, app.current_user_id(), p_action, coalesce(p_details, '{}'));
end $$;

create function public.recompute_issue_counters(p_issue_ids uuid[])
returns void language plpgsql security definer set search_path = '' as $$
begin
  if exists (select 1 from public.issues i
             where i.id = any(p_issue_ids) and coalesce(public.org_role(i.org_id), 'viewer') = 'viewer') then
    raise exception 'recompute_issue_counters not allowed' using errcode = '42501';
  end if;
  update public.issues i
     set ticket_count  = s.n,
         first_seen_at = coalesce(s.first_at, i.first_seen_at),
         last_seen_at  = coalesce(s.last_at, i.last_seen_at)
    from (select i2.id, count(t.id) as n, min(t.source_created_at) as first_at, max(t.source_created_at) as last_at
            from public.issues i2 left join public.tickets t on t.issue_id = i2.id
           where i2.id = any(p_issue_ids)
           group by i2.id) s
   where i.id = s.id;
end $$;

revoke execute on function public.org_role(uuid) from public;
revoke execute on function public.create_org(text, text, bytea) from public;
revoke execute on function public.log_issue_event(uuid, text, jsonb) from public;
revoke execute on function public.recompute_issue_counters(uuid[]) from public;
grant execute on function public.org_role(uuid) to sir_app;
grant execute on function public.create_org(text, text, bytea) to sir_app;
grant execute on function public.log_issue_event(uuid, text, jsonb) to sir_app;
grant execute on function public.recompute_issue_counters(uuid[]) to sir_app;

-- ============ RLS ============
do $$ declare t text; begin
  foreach t in array array['users','refresh_tokens','organizations','org_members','invites','integrations',
    'customers','releases','issues','tickets','issue_events','alerts','merge_suggestions','jobs']
  loop execute format('alter table public.%I enable row level security', t); end loop;
end $$;

-- users: a user reads only their own row; auth routes use the worker role. refresh_tokens: worker only.
create policy users_self on public.users for select to sir_app using (id = (select app.current_user_id()));

create policy org_read on public.organizations for select to sir_app using ((select public.org_role(id)) is not null);
create policy org_admin_update on public.organizations for update to sir_app
  using ((select public.org_role(id)) = 'admin') with check ((select public.org_role(id)) = 'admin');
create policy members_read on public.org_members for select to sir_app using ((select public.org_role(org_id)) is not null);
create policy members_admin on public.org_members for all to sir_app
  using ((select public.org_role(org_id)) = 'admin') with check ((select public.org_role(org_id)) = 'admin');
create policy invites_admin on public.invites for all to sir_app
  using ((select public.org_role(org_id)) = 'admin') with check ((select public.org_role(org_id)) = 'admin');
create policy integrations_admin on public.integrations for all to sir_app
  using ((select public.org_role(org_id)) = 'admin') with check ((select public.org_role(org_id)) = 'admin');

-- every member reads everything in their org
do $$ declare t text; begin
  foreach t in array array['customers','releases','issues','tickets','issue_events','alerts','merge_suggestions','jobs']
  loop execute format($f$create policy %1$s_read on public.%1$I for select to sir_app
       using ((select public.org_role(org_id)) is not null)$f$, t);
  end loop;
end $$;

-- leads/admins manage issues, releases, customers, suggestions
do $$ declare t text; begin
  foreach t in array array['customers','releases','issues','merge_suggestions']
  loop execute format($f$create policy %1$s_manage on public.%1$I for all to sir_app
       using ((select public.org_role(org_id)) in ('admin','lead'))
       with check ((select public.org_role(org_id)) in ('admin','lead'))$f$, t);
  end loop;
end $$;

-- agents (and up) may move a ticket to another issue (column-limited by the grant below)
create policy tickets_move on public.tickets for update to sir_app
  using ((select public.org_role(org_id)) in ('admin','lead','agent'))
  with check ((select public.org_role(org_id)) in ('admin','lead','agent'));

-- issue_events, alerts, jobs, tickets inserts: written only by sir_worker (no user write policies)

-- ============ Grants ============
grant select (id, email, display_name, is_active, created_at) on public.users to sir_app;
grant select, update on public.organizations to sir_app;
grant select, insert, update, delete on public.org_members, public.invites, public.integrations,
  public.customers, public.releases, public.issues, public.merge_suggestions to sir_app;
grant select on public.tickets, public.issue_events, public.alerts, public.jobs to sir_app;
grant update (issue_id, assignment) on public.tickets to sir_app;

grant select, insert, update, delete on all tables in schema public to sir_worker;
grant usage, select on all sequences in schema public to sir_worker;
```

**Which code writes:**
- **Worker (`sir_worker`):** ingestion, processing, assignment, alerts, jobs, the scheduler and the auth routes (login/register run before a user exists). There is no user, so RLS cannot protect these statements: every statement in `services/ingest.py`, `services/assign.py`, `services/pipeline.py` and the scheduler takes `org_id` as an explicit parameter and filters by it.
- **User (`sir_app`, RLS):** every request made on behalf of a user runs in one `user_session(user_id)` transaction. The same-org composite foreign keys (`(org_id, issue_id) → issues(org_id, id)`) also stop a ticket from pointing at another org's issue.
- **Merge, split, move (gaps between "user changes data" and "events/counters are worker-only"):** these routes do the authorization check and the data change (`tickets.issue_id/assignment`, the source issue's `status/merged_into`, the new issue row for a split) in the RLS user session, and in the **same transaction** call the `security definer` functions `public.log_issue_event()` (writes the `issue_events` row with `actor_id = app.current_user_id()`) and `public.recompute_issue_counters()` (recomputes `ticket_count`, `first_seen_at`, `last_seen_at` from `tickets`). Both re-check the caller's role: leads/admins may log any user action; agents may only log `ticket_moved` (recomputing counters is allowed for agents because the counters are derived from `tickets` and cannot be set to arbitrary values); viewers get an error. So users never get a write policy on `issue_events`, and an agent's ticket move still updates the issue counters even though agents cannot update `issues`. The 7-day metrics and `impact_score` of affected issues are refreshed by the next 15-minute scheduler pass.
- **Members and invites:** the route checks `org_role() = 'admin'` in the user session, then looks up the invitee by email and writes the membership or invite with the worker role filtered by that `org_id` (`sir_app` can only read its own `users` row).

### 3.2 Assignment algorithm (deterministic thresholds)

Chroma returns cosine **distance**; similarity = `1 − distance`.

```
neighbors = tickets.query(query_embeddings=[v], n_results=10,
              where=tenant_where(org, has_issue=True, created_ts={"$gte": now-90d}))
group neighbors with similarity ≥ 0.70 by issue_id (following merged_into to the live issue)
for each issue: votes = count, score = sum(similarity), best = max(similarity)
top = issue with highest score

if top.best ≥ 0.90                         → AUTO   (assignment='auto')
elif top.best ≥ 0.82 and top.votes ≥ 2      → AUTO
elif top.best ≥ 0.70 and not overloaded     → GRAY ZONE:
        candidates = top-3 issues from neighbors ∪ issues.query(v, n=3, status != merged)
        LLM GrayZoneDecision → existing issue id (must be in candidates) or NEW
        (assignment='llm')
elif top.best ≥ 0.70 and overloaded         → NEW issue (gray-zone overload fallback, see below)
else                                         → NEW issue (assignment='new_issue')

NEW issue: number = coalesce(max(number),0)+1 for org (safe: we hold the org advisory lock)
           provisional title = ticket summary[:80], summary = ticket summary,
           first_seen_at = last_seen_at = ticket time, issue_events('created')
           (title_chain runs after COMMIT, outside the lock)

if chosen issue.status = 'resolved' and ticket.source_created_at > resolved_at
   → possible_regression = true, issue_events('regression_flagged'),
     alerts(kind='regression', alert_date = today in org tz,
            observed = issue tickets with source_created_at > resolved_at (including this one),
            baseline = issue tickets in the 14 days before resolved_at / 14)
     on conflict (issue_id, kind, alert_date) do nothing
```

**Gray-zone overload:** `overloaded` is true when the org has more than 20 tickets with `status = 'processing'` and a stored signal (`summary is not null`), i.e. tickets understood and waiting for the assignment step. The pipeline clears the signal fields when it claims a ticket and writes them after understanding commits, so this count is measurable with one indexed query (`tickets_processing_idx`) taken right before the gray-zone call. While overloaded, gray-zone tickets become new issues instead of waiting on the strong model inside the lock, and the nightly merge-suggestion pass catches the duplicates.

The thresholds are constants in `app/clustering.py`, tuned on the evaluation set (§5, Phase 4). A wrong merge hides a separate bug, while a wrong split costs one click to merge. So the thresholds lean high, and the system prefers to split when unsure.

### 3.3 Impact score, spike and release correlation (deterministic)

**Impact score (0–100), refreshed every 15 min for issues seen in the last 14 days:**

```
customers   = distinct customers with tickets in the last 7 days
arr         = sum of those customers' ARR
growth      = clamp((tickets_7d − tickets_prev_7d) / max(tickets_prev_7d, 1), 0, 2) / 2
severe      = share of the issue's 7-day tickets with severity in (high, critical)

impact = min(100, round( 12 · log2(1 + customers)
                       +  6 · log10(1 + arr)
                       + 15 · growth
                       + 15 · severe ))
```
Example: 20 customers, $400k ARR, doubling week-on-week, 30% severe → 12·4.39 + 6·5.60 + 15·0.5 + 15·0.3 ≈ 52.7 + 33.6 + 7.5 + 4.5 = 98.

**Spike detection:**
```
observed = tickets for the issue in the last 24 h
baseline = tickets in the 14 days before that / 14
score    = (observed − baseline) / sqrt(baseline + 1)
alert if observed ≥ 5 and score ≥ 3
```
`sqrt(baseline + 1)` is the Poisson standard deviation with +1 smoothing, so an issue going from 0 to 5 tickets counts as a spike, but going from 40 to 45 does not.

**New high-impact issue:** created in the last 48 h, with `customers_7d ≥ 3` or `arr_affected_7d ≥ integrations.high_impact_arr`. `observed = customers_7d`, `baseline = 0`. A brand-new issue that also spikes gets **both** alerts (one `spike` and one `new_high_impact` row, at most one of each per issue per org-local day).

**Release correlation (shown on the issue page and in alerts):** two anchors, same rule.
```
anchor  = issue.first_seen_at                     (issue onset)
        | spike window start = now − 24 h         (for spike alerts on older issues)
release = latest release with released_at ≤ anchor + (24 h if spike anchor else 0)
          and anchor − released_at ≤ 72 h
          and (platform matches the most common platform of the tickets in scope, if known)
scope   = all issue tickets (onset) | the issue's tickets in the spike window (spike)
share   = scope tickets with app_version = release.version / scope tickets with any app_version
report "Started {h} h after {platform} {version}; {share}% of tickets on that version" if share ≥ 0.6
        (h = hours from released_at to first_seen_at, or to the first ticket in the spike window)
```
A spike alert uses the onset correlation when the issue is new (first seen within the window or the 72 h before it) and the spike-window correlation otherwise, so a release that breaks an old feature again is still named. The issue page returns the onset correlation if it qualifies, else the most recent spike-window correlation, with `"basis": "first_seen" | "spike"`.

### 3.4 ChromaDB collections

```python
# app/vectorstore.py: created if missing; an existing collection must carry the same model name
for name in ("tickets", "issues"):
    client.create_collection(
        name,
        configuration={"hnsw": {"space": "cosine"}},
        metadata={"embedding_model": settings.GEMINI_EMBEDDING_MODEL, "schema_version": 1},
    )
```

**Chunking strategy:** tickets are not chunked; each ticket is **one vector** of its normalized summary. Cleaning runs **at ingest**, before the upsert, so only the cleaned text is ever stored:
1. For email threads, keep the first customer message and the latest customer message. Cut quoted text at lines matching `^>` or `^On .+ wrote:$`, and cut signatures at `^--\s*$` or common sign-offs (`^(thanks|regards|best|cheers)[,!]?\s*$`, case-insensitive).
2. Redact PII: emails → `[EMAIL]`, phone numbers (`\+?\d[\d\s().-]{7,}\d`) → `[PHONE]`, 13–19 digit numbers that pass a Luhn check → `[CARD]`.
3. Truncate to 4,000 characters (head 3,000 + tail 1,000, since error details often sit at the end).

**Collection `tickets`**
- **Document:** `"{summary} | area: {product_area} | symptom: {symptom} | error: {error_code or 'none'} | platform: {platform or 'unknown'}"`.
- **ID:** `tickets.id`.
- **Metadata:**

| key | type | notes |
|---|---|---|
| `org_id` | str | always filtered, set by the server |
| `ticket_id` | str | |
| `issue_id` | str | `""` until assigned; updated on merge/move with `collection.update` |
| `has_issue` | bool | Chroma `where` cannot filter on empty strings well, so this is a separate flag |
| `kind` | str | |
| `product_area` | str | |
| `platform` | str | `""` if unknown |
| `app_version` | str | `""` if unknown |
| `created_ts` | int | unix seconds, for the 90-day window |

**Collection `issues`**
- **Document:** `"{title}. {summary} Typical symptoms: {3 most common ticket symptoms}."`, re-embedded when the title is regenerated or the issue is edited.
- **ID:** `issues.id`. **Metadata:** `org_id`, `kind`, `status`, `product_area`, `number` (int).
- **Uses:** gray-zone candidates, issue search, the nightly merge suggestions (for each issue active in 30 days, query `n_results=5`, pairs with similarity ≥ 0.85 that aren't already suggested → `merge_suggestions`).

### 3.5 Structured output models

```python
class TicketSignal(BaseModel):
    kind: Literal["bug","question","feature_request","billing","other"]
    summary: str = Field(max_length=300, description="One neutral sentence: what is broken or asked, no customer names")
    product_area: str = Field(max_length=40)          # chosen from organizations.product_areas when non-empty
    symptom: str = Field(max_length=120)
    error_code: str | None = Field(default=None, max_length=80)
    severity: Literal["low","medium","high","critical"]
    platform: Literal["web","ios","android","desktop","api"] | None = None
    app_version: str | None = Field(default=None, pattern=r"^\d+(\.\d+){1,3}$")

class GrayZoneDecision(BaseModel):
    choice: UUID | Literal["NEW"]
    reason: str = Field(max_length=200)
```
If `choice` is a UUID that is not one of the given candidates, it is treated as `NEW` and logged.

---

## 4. API Specification

Base `/api/v1` (except `GET /health`). Every endpoint needs `Authorization: Bearer <access token>`, except `/health`, the `/auth/*` routes and `/ingest/*` (HMAC). Errors always have the shape `{"error":{"code":"snake_case","message":"Human text"}}` (validation errors: 422 `validation_error`). Org-scoped routes return 404 for orgs or rows the caller cannot see, and 403 when the caller's role is too low.

### Auth & session
| Method | Route | Request | Response |
|---|---|---|---|
| GET | `/health` | — | `{"status":"ok","db":"ok","chroma":"ok"}` (503 with `"error"` values when a check fails) |
| POST | `/auth/register` | `{"email","password" (10–128),"display_name"}` | `201 {"user_id","email"}` · `409 email_taken` |
| POST | `/auth/login` | `{"email","password"}` | `{"access_token","token_type":"bearer","expires_in":900,"refresh_token"}` · generic `401 invalid_credentials` · 10/min per IP |
| POST | `/auth/refresh` | `{"refresh_token"}` | new token pair; the old refresh token is revoked. Reusing a revoked token revokes all of the user's refresh tokens → `401` |
| POST | `/auth/logout` | `{"refresh_token"}` | `204` |
| POST | `/auth/accept-invite` | `{"token","password","display_name"}` | token pair; creates the user if needed (an existing user must give their password) and the membership |
| GET | `/me` | — | `{"user_id","email","display_name","orgs":[{"org_id","name","role"}]}` |
| POST | `/orgs` | `{"name":"Acme","timezone":"Europe/London"}` | `201 {"org_id","name","timezone","role":"admin","ingest_secret":"shown once"}`. Calls `public.create_org()` in the user session (inserts the org, the caller's `admin` membership and the encrypted ingest secret). |
| GET | `/orgs` | — | `{"items":[{"org_id","name","timezone","role","created_at"}]}` (my orgs) |
| PATCH | `/orgs/{id}` | `{"name?","timezone?","product_areas?":["exports","billing"],"retention_days?":730}` | `{"org_id",…}` (admin) |
| GET | `/orgs/{id}/members` | — | `{"items":[{"user_id","email","display_name","role","joined_at"}]}` (any member) |
| POST | `/orgs/{id}/members` | `{"email","role":"agent"}` | admin only. Existing user → `201 {"user_id","role"}`. Unknown email → `201 {"invite_token","role","expires_at"}` (7 days, shown once; no email is sent in v1, the admin shares the link `/accept-invite?token=…`) |
| PATCH | `/orgs/{id}/members/{user_id}` | `{"role":"lead"}` | `{"user_id","role"}` (admin; the last admin cannot be demoted) |
| DELETE | `/orgs/{id}/members/{user_id}` | — | `204` (admin; the last admin cannot be removed) |
| GET | `/integrations/{org_id}` | — | `{"ingest_url","has_slack_webhook","high_impact_arr","prev_ingest_secret_expires_at"}` (admin; never returns a secret) |
| POST | `/orgs/{id}/ingest-secret/rotate` | `{}` | `{"ingest_secret":"shown once","prev_valid_until"}` (admin). The current secret moves to `prev_ingest_secret_encrypted` with `prev_ingest_secret_expires_at = now() + 24 h`; a second rotation inside the window replaces the previous one. |

### Ingestion & indexing
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/ingest/{org_id}/tickets` (full path `/api/v1/ingest/{org_id}/tickets`) | header `X-Radar-Signature: sha256=<hex>`; body `{"tickets":[{"external_id":"ZD-88123","customer_external_id":"acct_42","channel":"email","subject":"Export stuck","body":"…","created_at":"2026-10-20T09:14:00Z","platform":"web","app_version":"4.12.0"}]}` (≤ 100) | `202 {"accepted":37,"unchanged":3}` · `401` bad signature (no DB write) · `413` > 100 tickets · 600/min per org |
| POST | `/imports/tickets-csv` | `multipart/form-data`: `org_id`, `file` (`.csv`, ≤ `MAX_CSV_UPLOAD_BYTES`, default 50 MB; columns as in the JSON above) | lead+. `202 {"job_id","rows":50213,"estimated_llm_cost_usd":"65.28"}`. The file is streamed to `STORAGE_DIR/{org_id}/imports/{job_id}.csv`; rows are cleaned at import and processed oldest-first so clustering builds up in time order. The estimate is `rows / 1000 × LLM_COST_PER_1K_TICKETS_USD` (§6). |
| POST | `/customers/bulk` | `{"org_id","customers":[{"external_id":"acct_42","name":"Globex","plan":"enterprise","arr":"120000.00"}]}` (≤ 1,000) | `{"upserted":980}` |
| POST | `/releases` | `{"org_id","platform":"web","version":"4.12.0","released_at":"2026-10-20T07:00:00Z","notes":"str?"}` | `201 {"release_id"}` (also callable from CI with a lead's token) |
| GET | `/jobs/{id}` | — | `{"job_id","kind","status","total":50213,"done":12040,"error":null}` |
| GET | `/releases?org_id&cursor` | — | `{"items":[{"release_id","platform","version","released_at","notes"}],"next_cursor"}` (newest first) |
| GET | `/tickets?org_id&status=failed&cursor` | — | `{"items":[{"ticket_id","external_id","subject","attempts","error","created_at"}],"next_cursor"}` (settings → failed tickets; also `status=pending/processing`) |
| POST | `/admin/retry-failed` | `{"org_id"}` | lead+. `{"retried":12}`: `failed` tickets go back to `pending` with `attempts=0`, `next_attempt_at=null`, then a processing run is scheduled |
| POST | `/admin/reindex` | `{"org_id"}` | admin. `202 {"job_id"}`: a `jobs(kind='reindex')` row; deletes the org's vectors from both collections and rebuilds them from Postgres |
| POST | `/tickets/{id}/reprocess` | `{}` | `202` (re-runs understand + assign; a manual assignment is kept unless `{"force":true}`) |

### Vector query & retrieval
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/search/issues` | `{"org_id","query":"export hangs","status":["new","investigating"],"k":10}` | `{"results":[{"issue_id","number":142,"title","status","impact_score","similarity":0.87}]}` |
| POST | `/search/tickets` | `{"org_id","query":"SSO login loop","k":20}` | `{"results":[{"ticket_id","external_id","summary","issue_id","similarity"}]}` |
| GET | `/tickets/{id}/suggestion` | — | `{"issue":{"issue_id","number","title","ticket_count","status"},"workaround":"str|null","draft_reply":"str|null"}` (used by an agent-side widget or bookmarklet) |
| GET | `/issues/{id}/similar` | — | `{"results":[{"issue_id","number","title","similarity"}]}` (merge candidates) |

### Relational CRUD
| Method | Route | Request | Response |
|---|---|---|---|
| GET | `/issues?org_id&status&kind&area&sort=impact|recent&cursor` | — | `{"items":[{"issue_id","number","title","kind","status","impact_score","tickets_7d","customers_7d","arr_affected_7d","possible_regression","last_seen_at"}],"next_cursor"}` |
| GET | `/issues/{id}` | — | `{"issue":Issue,"daily":[{"date":"2026-10-20","tickets":23}],"versions":[{"app_version":"4.12.0","share":0.91}],"release_correlation":{"basis":"first_seen","platform":"web","version":"4.12.0","hours_after":2,"share":0.91}|null,"unknown_account_tickets_7d":12,"recent_tickets":[Ticket],"events":[IssueEvent]}` (`unknown_account_tickets_7d` = the issue's last-7-day tickets with `customer_id is null`, shown as "12 tickets from unknown accounts") |
| PATCH | `/issues/{id}` | `{"status":"fix_in_progress","title?","workaround?","external_url?"}` | `{"issue_id","status"}` (`resolved` sets `resolved_at` and clears `possible_regression`) |
| POST | `/issues/{id}/merge` | `{"into_issue_id":"uuid"}` | `{"issue_id":"into id","moved_tickets":38}`. lead+. One RLS transaction: move tickets, set the source to `merged` + `merged_into`, then `log_issue_event()` + `recompute_issue_counters()` (§3.1 "Which code writes"). Then Chroma metadata update. |
| POST | `/issues/{id}/split` | `{"ticket_ids":["uuid"],"title":"str"}` | lead+. `201 {"new_issue_id","number":311,"moved_tickets":6}` (number allocated under the org advisory lock; same event/counter functions) |
| PATCH | `/tickets/{id}` | `{"issue_id":"uuid"}` | agent+. `{"ticket_id","issue_id","assignment":"manual"}` (target must be a live issue of the same org; `log_issue_event('ticket_moved')` + `recompute_issue_counters([old, new])`) |
| GET | `/issues/{id}/affected-customers.csv` | — | CSV `customer_name,external_id,plan,arr,ticket_count,first_ticket_at,last_ticket_at` |
| GET | `/alerts?org_id&cursor` | — | `{"items":[{"alert_id","issue_id","kind","observed","baseline","message","created_at"}]}` |
| GET | `/merge-suggestions?org_id&status=open&cursor` | — | `{"items":[{"id","similarity","created_at","issue_a":{"issue_id","number","title","ticket_count"},"issue_b":{…}}],"next_cursor"}` |
| PATCH | `/merge-suggestions/{id}` | `{"status":"accepted"}` | `{"id","status"}` (`accepted` runs the merge, keeping the issue with more tickets) |
| GET | `/reports/weekly?org_id&week=2026-10-19` | — | `{"new_issues":14,"resolved":9,"top_issues":[…10],"spikes":[…],"tickets_total":2310,"auto_assigned_share":0.81}` |
| PUT | `/integrations/{org_id}/slack` | `{"webhook_url":"https://hooks.slack.com/services/…"}` | `{"ok":true}` (admin; only `https://hooks.slack.com/` URLs; stored Fernet-encrypted in `slack_webhook_encrypted`; a test message is sent) |

---

## 5. Implementation Roadmap

### Phase 1 — Environment & Schemas (days 1–3)
1. Repo layout: `web/` (Next.js 15, Tailwind v4, Recharts), `api/` (FastAPI, `uv`, Python 3.12, `langchain-google-genai`, `chromadb`), `db/migrations/`, `scripts/` (`db_setup.py`, `migrate.py`, demo tools).
2. `scripts/db_setup.py` creates the roles and databases; `scripts/migrate.py` applies the §3.1 DDL.
3. pytest RLS tests against the real test database `support_radar_test`: org A members see 0 rows of org B; an agent can update `tickets.issue_id` but not `tickets.summary`; a viewer cannot update issues; `sir_app` cannot insert `alerts`, `issue_events` or `jobs`; the worker role sees every org.
4. **Demo dataset:** a script `scripts/make_demo.py` that writes 3,000 synthetic tickets over the 60 days ending at `--now` (default: the current time; every timestamp, release date and spike is relative to it, so the demo always has a "last 24 h"). It plants 15 recurring issues (each written in 10+ different phrasings), one new bug that spikes 2 h after a planted release `web 4.12.0` at `now − 26 h`, 20% noise tickets (billing, one-offs), and 300 customers with ARR. The planted labels become the evaluation set.

**Exit check:** `db_setup.py`, `migrate.py` and `pytest` pass, and the demo data loads.

### Phase 2 — FastAPI & ChromaDB Core (days 4–11)
1. Auth (own JWTs, §4 Auth), `db.py` (`user_session` / `worker_session`), `crypto.py` (Fernet secrets).
2. `ingest.py`: HMAC verify on the raw body (read `await request.body()` before parsing) against the current or unexpired previous secret, clean + redact each body (`cleaning.py`), then upsert with `on conflict (org_id, external_id) do update … where tickets.body_hash <> excluded.body_hash`, and only changed rows go back to `pending`.
3. `cleaning.py`: pure functions for quote stripping, signature stripping, PII redaction and truncation. Table tests include a real-looking Gmail thread, an Outlook thread, a card number that passes Luhn and one that doesn't.
4. `understand.py`: `signal_chain` with 8 concurrent calls (`asyncio.Semaphore(8)`); a validation failure retries once, then the ticket is `failed` with the error.
5. `assign.py`: the §3.2 algorithm (provisional title for new issues; `title_chain` after commit) inside `async with session.begin(): await session.execute(text("select pg_advisory_xact_lock(hashtextextended(:org, 0))"), {"org": str(org_id)})`. Unit tests use a fake neighbor list for each branch (auto, auto with votes, gray zone pick, gray zone NEW, invalid LLM id → NEW, new issue, regression).
6. `analytics.py`: impact, spike and release correlation as SQL + small pure functions, tested with fixed numbers (including the worked example in §3.3).
7. `scheduler.py`: a startup task `while True: if try_lock: run(); sleep(900)`.

**Exit check:** on the demo dataset, the 15 planted issues come out as ≤ 20 issues, and no ticket is in two issues. One scheduler pass creates exactly two alerts in the org: one `spike` alert on the planted spike issue whose message names `web 4.12.0` with share ≥ 60%, and one `new_high_impact` alert on the same issue (it is new and hits ≥ 3 customers). No other issue gets a `spike` alert; a second pass on the same day adds none.

### Phase 3 — Frontend & Auth (days 12–17)
1. Email + password login and registration (Server Actions, httpOnly cookies), invite acceptance page, org switcher, role-aware buttons (viewers see no edit controls).
2. Issues dashboard: a ranked table with impact, 7-day tickets, customers, ARR, status pills and a regression badge, polling every 30 s.
3. Issue page: daily chart with release markers, version breakdown, release correlation line, workaround editor, ticket list, merge and split dialogs, affected-customers CSV export.
4. Alerts page, merge suggestions inbox, weekly report.
5. Settings: copy the ingest URL, rotate the secret, add the Slack webhook, add releases by hand, upload customer and ticket CSVs, invite members, list failed tickets with "Retry all".

**Exit check:** a lead can find the planted spike issue, add a workaround, merge two duplicate issues and export the affected customers, without touching the API directly.

### Phase 4 — Integration & Edge Cases (days 18–23)
- **Burst traffic** (a 500-ticket outage burst): processing is serialized per org only for the short assignment step (≈ 50 ms with Chroma + SQL). The slow LLM understanding step runs before the lock, in parallel. Gray-zone LLM calls happen inside the lock, so their count is limited by the thresholds. When more than 20 of the org's tickets are understood and waiting for assignment (§3.2 "Gray-zone overload"), gray-zone tickets become new issues ("NEW if below 0.82") and the nightly merge-suggestion pass catches duplicates.
- **Ticket updated by the customer:** a new body hash re-runs understanding. Re-assignment only happens if the ticket's assignment is not `manual`.
- **Merging chains** (A merged into B, then B into C): neighbor voting follows `merged_into` until it reaches a live issue (max depth 5, guarded by the `check` constraint and a loop counter).
- **Unknown customer:** the ticket is stored with `customer_id = null` and counts toward ticket numbers, but not customer or ARR numbers. The issue page shows "12 tickets from unknown accounts" (`unknown_account_tickets_7d`).
- **Non-English tickets:** the LLM writes the summary in English, so tickets in different languages about the same bug cluster together. `body_redacted` keeps the original language (cleaned and redacted); agents read the full original message in their helpdesk, which stays the system of record for ticket text.
- **Chroma and Postgres disagree** (crash between commit and Chroma upsert): a repair task runs every hour. Each run is a `jobs(kind='repair')` row. The last-sync marker is the `created_at` (start time) of the org's latest `succeeded` job with `kind in ('repair','reindex')`; the run re-upserts every processed ticket (and its issue) with `processed_at ≥ marker − 5 min` (overlap so nothing committed during the previous run is missed), then marks its own job `succeeded`. It also resets tickets stuck in `processing` for more than 10 min back to `pending`. Postgres is the source of truth, and `POST /admin/reindex` rebuilds both collections for an org from it.
- **Evaluation:** on the demo labels, measure pairwise precision (tickets in the same issue that truly belong together) and recall. Targets: precision ≥ 0.9, recall ≥ 0.75. Track `auto_assigned_share` (target ≥ 75%, so most tickets never need the gray-zone LLM).

---

## 6. Interview Talking Points & Architecture Trade-offs

### Key trade-offs
| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Clustering method | Online nearest-neighbor voting with thresholds + LLM gray zone | Batch clustering (HDBSCAN, k-means) nightly | Tickets need an issue *now*, so agents see the workaround while answering. Batch clusters also change IDs between runs, which breaks links, Jira references and history. Online assignment gives stable IDs; the nightly merge-suggestion pass recovers the main benefit of batch clustering. |
| What gets embedded | LLM-normalized summary | Raw ticket text | Raw text is dominated by greetings, signatures, quoted threads and customer-specific detail. Normalizing first makes paraphrases and different languages land close together. Cost: one `GEMINI_MODEL` call per ticket (~900 tokens in, ~120 out). |
| Race handling | Per-org advisory lock around assignment only | Message queue with one consumer per org | Same safety with no extra service. Only the ~50 ms decision step is serialized; the slow LLM step stays parallel. |
| Vector store | Embedded ChromaDB | pgvector | Clean separation: Postgres holds the truth (assignments, counts), Chroma is a rebuildable index. Metadata filters (`org_id`, `has_issue`, `created_ts`) fit Chroma's `where`. Tenancy is enforced in one helper that builds every `where`, because Chroma has no RLS. |
| Dashboard freshness | 30 s polling | Server push (WebSockets / SSE) | Issue rankings move over hours, not seconds. Polling a single indexed query is cheap and survives reconnects. Push would make sense for a live "incoming tickets" feed, which is not a v1 need. |
| Scheduler | In-process loop + `pg_try_advisory_lock` | Celery beat / cron server | One less service; the lock guarantees one runner even with several API replicas. |
| Auth | Own email/password accounts + short-lived HS256 JWTs + rotated refresh tokens | Hosted auth / OAuth | No external dependency for a B2B tool with invited users; reuse detection on refresh tokens limits token theft. OAuth/SSO can be added later behind the same `get_current_user`. |
| Tenancy enforcement | Postgres RLS on a non-owner app role + one Chroma `tenant_where` helper | Filtering in application code only | A forgotten `where org_id = …` in a user route returns 0 rows instead of leaking. Background work uses a separate BYPASSRLS role whose queries always take an explicit `org_id`. |
| Alert rule | Count vs baseline with Poisson-style scaling | ML anomaly detection | Explainable in one sentence in the Slack message, with no training data needed. Good enough for daily-scale counts. |

### Hallucination mitigation & guardrails
1. **The LLM never invents IDs:** a gray-zone answer must be one of the given candidate IDs, or it becomes `NEW`.
2. **Thresholds decide most cases:** the LLM only sees the uncertain middle band, and its decision is logged with a reason for review.
3. **Suggested replies only from human workarounds:** with no workaround written, no reply is drafted. The reply prompt gets only the workaround and the ticket.
4. **Numbers come from SQL:** the impact score, spike counts and release correlation never pass through the LLM.
5. **PII stays out of prompts and vectors:** redaction runs before the LLM and before embedding; customer names come from the `customers` table, never from the text.
6. **Prompt injection in tickets:** ticket text is untrusted input inside delimiters, and the chains have no tools. The worst case is a wrong grouping, which a human can fix in one click.

### Latency optimization
- Webhook response is immediate (`202`); processing is asynchronous. Target: p95 from webhook to assigned issue < 10 s.
- The cheap model handles volume; the stronger model is used only for the gray zone (target < 25% of tickets) and titles.
- Neighbor queries are limited to 90 days and `has_issue = True`, which keeps the candidate set small and relevant.
- Issue counters are updated incrementally on assignment; the full impact refresh runs every 15 min, so dashboard reads are a single indexed `order by impact_score desc`.

### Failure modes, rate limiting, scaling
- **LLM outage or 429s:** a failed ticket goes back to `pending` with `attempts += 1` and `next_attempt_at = now() + BACKOFF[attempts − 1]`, where `BACKOFF = [30 s, 2 min, 10 min, 30 min, 60 min]`, so it is retried up to 5 times. The claim query only picks tickets with `next_attempt_at is null or next_attempt_at ≤ now()`. A 6th failure (the 5 retries are used up) sets `status = 'failed'` with the error. Failed tickets are listed in settings (`GET /tickets?status=failed`) with a "Retry all" button (`POST /admin/retry-failed`). Ingestion keeps accepting tickets, so no data is lost.
- **Webhook abuse:** the HMAC check rejects unsigned calls before any database write; `slowapi` limits ingestion to 600 requests/min per org and the search endpoints to 60/min per user.
- **Backfill cost control:** CSV import shows the estimated LLM cost before starting and runs at most 8 concurrent calls, so a 50k-ticket history doesn't spike costs or hit provider limits. The estimate is `rows / 1000 × LLM_COST_PER_1K_TICKETS_USD` (a setting, default `1.30`). The default comes from list prices at the time of writing (check current Gemini pricing): `gemini-2.5-flash` signal call ≈ 900 input tokens × $0.30/M + 120 output × $2.50/M ≈ $0.00057; embedding ≈ $0.00001; gray-zone and title calls on `gemini-2.5-pro` for ~25% of tickets ≈ 0.25 × (1,500 × $1.25/M + 100 × $10/M) ≈ $0.00072. Total ≈ $0.0013 per ticket, so 50,213 rows ≈ $65.28.
- **Scaling path:**
  1. Run processing in a separate worker process that claims pending tickets with `select … for update skip locked`; the API code stays the same.
  2. Move Chroma to server mode (`HttpClient`) once more than one process writes to it.
  3. Give large orgs their own Chroma collection.
  4. At ~10M tickets, partition `tickets` by month on `source_created_at`.

  At the target load (≤ 5,000 tickets/week per org, ~1 ticket/min on average with bursts), the single-process design has wide headroom.
- **Data retention:** tickets older than `organizations.retention_days` (default 730) are deleted from Postgres and Chroma by a nightly job; issues keep their aggregate counts.
