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
