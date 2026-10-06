-- Explicit operator execution only. No automatic application/startup migration.
begin;

create table public.job_profile_versions (
    id uuid primary key,
    job_id uuid not null, -- historical identity survives canonical deletion/merge
    schema_version text not null,
    projector_version text not null,
    parser_version text not null,
    taxonomy_version text not null,
    layer_versions jsonb not null check (jsonb_typeof(layer_versions) = 'object'),
    semantic_metadata_hash text not null check (semantic_metadata_hash ~ '^[0-9a-f]{64}$'),
    cleaned_description_hash text not null check (cleaned_description_hash ~ '^[0-9a-f]{64}$'),
    source_selection_hash text not null check (source_selection_hash ~ '^[0-9a-f]{64}$'),
    input_hash text not null check (input_hash ~ '^[0-9a-f]{64}$'),
    profile jsonb not null check (jsonb_typeof(profile) = 'object'),
    evidence jsonb not null check (jsonb_typeof(evidence) = 'array'),
    input_snapshot jsonb not null check (jsonb_typeof(input_snapshot) = 'object'),
    created_at timestamptz not null default now(),
    unique (job_id, id),
    unique (job_id, input_hash),
    check (profile #>> '{metadata,job_id}' is not null
        and profile #>> '{metadata,job_id}' = job_id::text),
    check (profile #>> '{metadata,input_hash}' is not null
        and profile #>> '{metadata,input_hash}' = input_hash),
    check (profile -> 'evidence' is not null and profile -> 'evidence' = evidence)
);

create table public.job_profile_current (
    job_id uuid primary key references public.jobs(id) on delete cascade,
    current_version_id uuid,
    processing_state text not null default 'pending'
        check (processing_state in ('pending','processing','completed','failed','stale')),
    target_input_hash text check (target_input_hash ~ '^[0-9a-f]{64}$'),
    target_hashes jsonb not null default '{}'::jsonb
        check (jsonb_typeof(target_hashes) = 'object'),
    dirty_revision bigint not null default 0 check (dirty_revision >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    attempts integer not null default 0 check (attempts >= 0),
    next_retry_at timestamptz,
    last_success_at timestamptz,
    last_error text, -- exception class/code only, never raw exception/payload
    updated_at timestamptz not null default now(),
    foreign key (job_id, current_version_id)
        references public.job_profile_versions(job_id, id),
    check (processing_state <> 'completed' or current_version_id is not null),
    check ((processing_state = 'processing') =
        (lease_token is not null and lease_expires_at is not null)),
    check (processing_state = 'processing' or
        (lease_token is null and lease_expires_at is null))
);

create index job_profile_work_idx on public.job_profile_current
    (processing_state, next_retry_at, lease_expires_at, updated_at);

create function public.job_profile_versions_immutable() returns trigger
language plpgsql set search_path = pg_catalog, public as $$
begin
    raise exception 'Job profile versions are immutable' using errcode = '55000';
end;
$$;
create trigger job_profile_versions_no_mutation
    before update or delete on public.job_profile_versions
    for each row execute function public.job_profile_versions_immutable();
create trigger job_profile_versions_no_truncate
    before truncate on public.job_profile_versions
    for each statement execute function public.job_profile_versions_immutable();

-- Enrolled jobs only: acquisition cannot enroll the whole database implicitly.
-- Raw changes mark dirty; semantic hashing later rejects token/default noise.
create function public.job_profile_source_changed() returns trigger
language plpgsql security definer set search_path = pg_catalog, public as $$
declare affected uuid[];
begin
    if TG_OP = 'UPDATE' then
        if (NEW.job_id, NEW.source_name, NEW.source_job_id, NEW.source_url,
            NEW.is_active, NEW.raw_payload) is not distinct from
           (OLD.job_id, OLD.source_name, OLD.source_job_id, OLD.source_url,
            OLD.is_active, OLD.raw_payload) then
            return NEW;
        end if;
        affected := array[OLD.job_id, NEW.job_id];
        -- A merge into an unenrolled keeper inherits enrollment, not the old
        -- current version. Rebuild from the keeper's combined native sources.
        if NEW.job_id is distinct from OLD.job_id and exists (
            select 1 from public.job_profile_current where job_id = OLD.job_id
        ) then
            insert into public.job_profile_current (job_id, processing_state)
                values (NEW.job_id, 'stale') on conflict (job_id) do nothing;
        end if;
    elsif TG_OP = 'DELETE' then
        affected := array[OLD.job_id];
    else
        affected := array[NEW.job_id];
    end if;
    update public.job_profile_current
        set processing_state = 'stale', dirty_revision = dirty_revision + 1,
            lease_token = null, lease_expires_at = null,
            attempts = 0, next_retry_at = null, updated_at = now()
        where job_id = any(affected);
    if TG_OP = 'DELETE' then return OLD; end if;
    return NEW;
end;
$$;
create trigger job_profile_source_invalidation
    after insert or update or delete on public.job_sources
    for each row execute function public.job_profile_source_changed();

alter table public.job_profile_versions enable row level security;
alter table public.job_profile_current enable row level security;
revoke all on public.job_profile_versions, public.job_profile_current from public;
revoke all on function public.job_profile_versions_immutable() from public;
revoke all on function public.job_profile_source_changed() from public;

-- Supabase default grants must not leak new projection/input/evidence tables.
-- Existing worker is the postgres owner (BYPASSRLS). Optional service_role can
-- also operate the subsystem, but receives no UPDATE/DELETE on version history.
do $$
declare role_name text;
begin
    foreach role_name in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = role_name) then
            execute format('revoke all on public.job_profile_versions, public.job_profile_current from %I', role_name);
            execute format('revoke all on function public.job_profile_versions_immutable(), public.job_profile_source_changed() from %I', role_name);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant select, insert on public.job_profile_versions to service_role;
        grant select, insert, update on public.job_profile_current to service_role;
    end if;
end;
$$;
commit;
