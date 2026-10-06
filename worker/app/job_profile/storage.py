"""Short PostgreSQL transactions for explicitly enrolled profile work."""
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import JobProfile
from .pipeline import input_fingerprint


MAX_BATCH = 25
MAX_ATTEMPTS = 3


def bounded_ids(ids):
    result = list(dict.fromkeys(str(UUID(str(value))) for value in ids))
    if not 1 <= len(result) <= MAX_BATCH:
        raise ValueError('Provide between 1 and 25 unique canonical job IDs')
    return result


def load_job(conn, job_id):
    job = conn.execute('select id::text as id, title, status from public.jobs where id = %s',
                       (job_id,)).fetchone()
    if not job:
        raise LookupError('Canonical job not found')
    job['sources'] = conn.execute('''select source_name, source_job_id, raw_payload,
        source_url, is_active, last_seen_at from public.job_sources where job_id = %s
        order by source_name, source_job_id''', (job_id,)).fetchall()
    return job


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    revision: int
    attempts: int


class ProfileStore:
    def __init__(self, database_url):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def preview(self, job_id):
        with self.connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            return load_job(conn, str(UUID(str(job_id))))

    def enqueue(self, ids, *, retry=False):
        outcomes = []
        for job_id in bounded_ids(ids):
            with self.connect() as conn:
                conn.execute('''insert into public.job_profile_current (job_id)
                    values (%s) on conflict (job_id) do nothing''', (job_id,))
                current = conn.execute('''select c.*, v.input_hash as published_hash
                    from public.job_profile_current c left join public.job_profile_versions v
                    on v.id = c.current_version_id where c.job_id = %s for update of c''', (job_id,)).fetchone()
                fp = input_fingerprint(load_job(conn, job_id))
                if current['processing_state'] == 'processing':
                    outcomes.append({'job_id': job_id, 'state': 'leased'})
                    continue
                if current['published_hash'] == fp['input_hash']:
                    state = 'completed'
                elif current['target_input_hash'] == fp['input_hash'] and current['processing_state'] == 'failed' and not retry:
                    outcomes.append({'job_id': job_id, 'state': 'failed', 'retry_required': True})
                    continue
                else:
                    state = 'stale' if current['current_version_id'] else 'pending'
                conn.execute('''update public.job_profile_current set processing_state = %s,
                    target_input_hash = %s, target_hashes = %s, attempts = 0, next_retry_at = null,
                    last_error = null, updated_at = now() where job_id = %s''',
                    (state, fp['input_hash'], Jsonb(fp), job_id))
                outcomes.append({'job_id': job_id, 'state': state})
        return outcomes

    def claim(self, *, lease_seconds=120):
        if not 5 <= lease_seconds <= 900:
            raise ValueError('Lease duration must be between 5 and 900 seconds')
        token = str(uuid4())
        with self.connect() as conn:
            # A repeatedly crashed worker must not create an infinite retry loop.
            conn.execute('''with exhausted as (
                select job_id from public.job_profile_current
                where processing_state = 'processing' and lease_expires_at <= clock_timestamp()
                  and attempts >= %s for update skip locked limit %s
            ) update public.job_profile_current c set processing_state = 'failed',
                lease_token = null, lease_expires_at = null, last_error = 'lease_exhausted',
                next_retry_at = null, updated_at = now()
                from exhausted where c.job_id = exhausted.job_id''', (MAX_ATTEMPTS, MAX_BATCH))
            row = conn.execute('''with chosen as (
                select job_id from public.job_profile_current
                where attempts < %s and (
                    processing_state in ('pending','stale') or
                    (processing_state = 'failed' and next_retry_at <= now()) or
                    (processing_state = 'processing' and lease_expires_at <= now()))
                order by updated_at, job_id for update skip locked limit 1
            ) update public.job_profile_current c set processing_state = 'processing',
                lease_token = %s, lease_expires_at = clock_timestamp() + %s * interval '1 second',
                attempts = attempts + 1, next_retry_at = null, updated_at = now()
                from chosen where c.job_id = chosen.job_id
                returning c.job_id::text, c.dirty_revision, c.attempts''',
                (MAX_ATTEMPTS, token, lease_seconds)).fetchone()
            return Lease(row['job_id'], token, row['dirty_revision'], row['attempts']) if row else None

    def inputs(self, lease):
        with self.connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            job = load_job(conn, lease.job_id)
            previous = conn.execute('''select v.profile, v.input_snapshot from public.job_profile_current c
                join public.job_profile_versions v on v.id = c.current_version_id
                where c.job_id = %s''', (lease.job_id,)).fetchone()
            return job, previous

    def publish(self, lease, fingerprint, profile=None, snapshot=None):
        with self.connect() as conn:
            # Same parent-before-child order as canonical merge/delete. A merge
            # cannot delete this job while its profile pointer is being published.
            if not conn.execute('select id from public.jobs where id = %s for key share',
                                (lease.job_id,)).fetchone():
                return False
            current = conn.execute('''select * from public.job_profile_current
                where job_id = %s for update''', (lease.job_id,)).fetchone()
            if not current or str(current['lease_token']) != lease.token or current['dirty_revision'] != lease.revision:
                return False
            live = conn.execute('select lease_expires_at > clock_timestamp() as valid from public.job_profile_current where job_id = %s',
                                (lease.job_id,)).fetchone()['valid']
            if not live:
                return False
            # Cheap hashes again, never text extraction inside the publish txn.
            latest = input_fingerprint(load_job(conn, lease.job_id))
            if latest != fingerprint:
                conn.execute('''update public.job_profile_current set processing_state = 'stale',
                    dirty_revision = dirty_revision + 1, lease_token = null, lease_expires_at = null,
                    attempts = 0, updated_at = now() where job_id = %s''', (lease.job_id,))
                return False
            if profile is not None:
                profile = JobProfile.model_validate(profile)
                if profile.metadata.job_id != lease.job_id or profile.metadata.input_hash != fingerprint['input_hash']:
                    raise ValueError('Profile input identity mismatch')
                body = profile.model_dump(mode='json')
                versions = fingerprint['versions']
                row = conn.execute('''insert into public.job_profile_versions (
                    id, job_id, schema_version, projector_version, parser_version, taxonomy_version,
                    layer_versions, semantic_metadata_hash, cleaned_description_hash,
                    source_selection_hash, input_hash, profile, evidence, input_snapshot)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (job_id, input_hash) do nothing returning id''',
                    (str(uuid4()), lease.job_id, versions['schema'], versions['projector'], versions['parser'], versions['taxonomy'],
                     Jsonb(versions), fingerprint['semantic_metadata_hash'], fingerprint['cleaned_description_hash'],
                     fingerprint['source_inputs_hash'], fingerprint['input_hash'], Jsonb(body), Jsonb(body['evidence']),
                     Jsonb(snapshot or {}))).fetchone()
                version_id = row['id'] if row else conn.execute('''select id from public.job_profile_versions
                    where job_id = %s and input_hash = %s''', (lease.job_id, fingerprint['input_hash'])).fetchone()['id']
            else:
                row = conn.execute('''select id from public.job_profile_versions where id = %s and input_hash = %s''',
                    (current['current_version_id'], fingerprint['input_hash'])).fetchone()
                if not row:
                    raise ValueError('No matching current version for no-op')
                version_id = row['id']
            published = conn.execute('''update public.job_profile_current set current_version_id = %s,
                processing_state = 'completed', target_input_hash = %s, target_hashes = %s,
                lease_token = null, lease_expires_at = null, last_success_at = now(),
                last_error = null, attempts = 0, next_retry_at = null, updated_at = now()
                where job_id = %s and lease_token = %s and dirty_revision = %s
                  and lease_expires_at > clock_timestamp() returning job_id''',
                (version_id, fingerprint['input_hash'], Jsonb(fingerprint), lease.job_id, lease.token, lease.revision)).fetchone()
            if not published:
                conn.rollback()  # also discard a version inserted after lease expiry
                return False
            return True

    def fail(self, lease, error):
        # Never persist raw exceptions: database URLs/payloads may appear in them.
        code = type(error).__name__[:100]
        delay = min(300, 15 * 2 ** (lease.attempts - 1))
        with self.connect() as conn:
            conn.execute('''update public.job_profile_current set processing_state = 'failed',
                lease_token = null, lease_expires_at = null, last_error = %s,
                next_retry_at = case when attempts < %s then now() + %s * interval '1 second' else null end,
                updated_at = now() where job_id = %s and lease_token = %s
                and dirty_revision = %s and lease_expires_at > clock_timestamp()''',
                (code, MAX_ATTEMPTS, delay, lease.job_id, lease.token, lease.revision))
