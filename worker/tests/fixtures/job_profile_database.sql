-- Isolated-test acquisition subset, based on 2026-10-06 production pg_catalog
-- audit. Includes every column used by the existing real merge implementation.
create role anon;
create role authenticated;
create role service_role bypassrls;
grant usage on schema public to anon, authenticated, service_role;
alter default privileges grant all on tables to anon, authenticated, service_role;
create table public.companies (id uuid primary key default gen_random_uuid(), name text not null);
create table public.jobs (
    id uuid primary key default gen_random_uuid(),
    company_id uuid references public.companies(id) on delete set null,
    title text not null, description text, location_text text, city text,
    country_code text default 'CZ', remote_type text default 'unknown'
        check (remote_type in ('onsite','hybrid','remote','unknown')),
    employment_type text, seniority text, salary_text text, salary_min numeric,
    salary_max numeric, salary_currency text, salary_period text,
    skills jsonb not null default '[]'::jsonb, canonical_url text,
    published_at timestamptz, expires_at timestamptz,
    first_seen_at timestamptz not null default now(), last_seen_at timestamptz not null default now(),
    last_verified_at timestamptz, updated_at timestamptz not null default now(),
    status text not null default 'active' check (status in ('active','inactive','expired'))
);
create table public.job_sources (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    source_name text not null, source_job_id text not null, source_url text, apply_url text,
    raw_payload jsonb, first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(), last_verified_at timestamptz,
    is_active boolean not null default true, created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(), unique(source_name, source_job_id)
);
create index job_sources_job_id_idx on public.job_sources(job_id);
create table public.duplicate_candidates (
    id uuid primary key default gen_random_uuid(),
    job_a_id uuid references public.jobs(id) on delete cascade,
    job_b_id uuid references public.jobs(id) on delete cascade,
    confidence numeric, reason jsonb
);
create table public.job_merge_history (
    id uuid primary key default gen_random_uuid(), kept_job_id uuid,
    removed_job_id uuid not null, duplicate_candidate_id uuid,
    confidence numeric, reason jsonb, snapshot jsonb, merged_at timestamptz not null default now()
);
alter table public.jobs enable row level security;
alter table public.job_sources enable row level security;
