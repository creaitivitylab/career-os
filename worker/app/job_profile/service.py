"""Bounded asynchronous work: claim/commit, compute, publish/commit."""
from .models import JobProfile
from .pipeline import build_profile, input_fingerprint, reprocessing_layers
from .storage import MAX_BATCH


def process_batch(store, *, limit=1, lease_seconds=120):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError('Batch limit must be between 1 and 25')
    results = []
    for _ in range(limit):
        lease = store.claim(lease_seconds=lease_seconds)
        if lease is None:
            break
        try:
            job, previous = store.inputs(lease)
            fp = input_fingerprint(job)
            old_profile = JobProfile.model_validate(previous['profile']) if previous else None
            if old_profile and old_profile.metadata.input_hash == fp['input_hash']:
                accepted = store.publish(lease, fp)
                results.append({'job_id': lease.job_id, 'state': 'completed' if accepted else 'superseded', 'layers': [], 'no_op': True})
                continue
            cache = {}
            profile = build_profile(job, text_cache=previous['input_snapshot'].get('text_layer') if previous else None,
                                    cache_out=cache)
            layers = reprocessing_layers(old_profile.metadata if old_profile else None, profile.metadata)
            # Selected facts/cleaned text only; never snapshot entire raw payloads.
            snapshot = {'cleaned_description': profile.content.cleaned_description.value,
                'native_inputs': [e.model_dump(mode='json') for e in profile.evidence if e.native_field_path and not e.text_span],
                'text_layer': cache}
            accepted = store.publish(lease, fp, profile, snapshot)
            results.append({'job_id': lease.job_id, 'state': 'completed' if accepted else 'superseded', 'layers': sorted(layers)})
        except Exception as exc:
            failure = {'job_id': lease.job_id, 'state': 'failed', 'error': type(exc).__name__}
            try:
                store.fail(lease, exc)
            except Exception as persistence_error:
                # The lease remains recoverable after expiry if the DB is down.
                failure['failure_recording_error'] = type(persistence_error).__name__
            results.append(failure)
    return results
