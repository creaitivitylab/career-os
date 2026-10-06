-- Historical Phase 1 draft; superseded by worker/migrations/20261006_job_profiles.sql.
-- REVIEW PROPOSAL ONLY. Not a migration and never executed by Phase 1.
-- Client generates version_id. Existing jobs.id is UUID.
-- No public/client grants: RLS/service access policy requires separate review.

create table public.job_profile_versions (
    version_id uuid primary key,
    job_id uuid not null, -- history survives canonical merges/deletes
    schema_version text not null,
    layer_versions jsonb not null check (jsonb_typeof(layer_versions) = 'object'),
    semantic_metadata_hash text not null check (semantic_metadata_hash ~ '^[0-9a-f]{64}$'),
    cleaned_description_hash text not null check (cleaned_description_hash ~ '^[0-9a-f]{64}$'),
    source_inputs_hash text not null check (source_inputs_hash ~ '^[0-9a-f]{64}$'),
    input_hash text not null check (input_hash ~ '^[0-9a-f]{64}$'),
    profile jsonb not null check (jsonb_typeof(profile) = 'object'),
    input_snapshot jsonb not null check (jsonb_typeof(input_snapshot) = 'object'),
    generated_at timestamptz not null,
    created_at timestamptz not null default now(),
    unique (job_id, version_id),
    unique (job_id, input_hash)
);

create table public.job_profile_current (
    job_id uuid primary key references public.jobs(id) on delete cascade,
    current_version_id uuid,
    processing_state text not null default 'pending'
        check (processing_state in ('pending', 'processing', 'completed', 'failed', 'stale')),
    target_input_hash text check (target_input_hash ~ '^[0-9a-f]{64}$'),
    lease_expires_at timestamptz,
    attempts integer not null default 0 check (attempts >= 0),
    last_error text, -- sanitized; no tokens/raw credentials
    requested_at timestamptz not null default now(),
    processed_at timestamptz,
    updated_at timestamptz not null default now(),
    foreign key (job_id, current_version_id)
        references public.job_profile_versions(job_id, version_id),
    check (processing_state <> 'completed' or current_version_id is not null),
    check (processing_state <> 'processing' or lease_expires_at is not null)
);

alter table public.job_profile_versions enable row level security;
alter table public.job_profile_current enable row level security;
-- Policies/grants, lease claims and invalidation are not implemented here.
-- Version rows are immutable by application contract; production permissions
-- must enforce insert/read-only history before deployment.
