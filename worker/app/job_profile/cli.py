"""Local operational command. No public generation endpoint or startup worker."""
import argparse
import json
import os
import time
from uuid import UUID

from .pipeline import build_profile
from .service import process_batch
from .storage import ProfileStore, bounded_ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--job-id', required=True, type=UUID)
    enqueue = commands.add_parser('enqueue')
    enqueue.add_argument('--job-id', action='append', required=True, type=UUID)
    enqueue.add_argument('--retry', action='store_true')
    enqueue.add_argument('--apply', action='store_true')
    process = commands.add_parser('process')
    process.add_argument('--limit', type=int, default=1, choices=range(1, 26))
    process.add_argument('--polls', type=int, default=1, choices=range(1, 11))
    process.add_argument('--interval', type=int, default=5, choices=range(1, 61))
    process.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    store = ProfileStore(os.environ.get('JOB_PROFILE_DATABASE_URL') or os.environ['DATABASE_URL'])
    try:
        if args.command == 'preview':
            print(build_profile(store.preview(args.job_id)).model_dump_json(indent=2))
        elif args.command == 'enqueue':
            ids = bounded_ids(args.job_id)
            if args.apply:
                print(json.dumps(store.enqueue(ids, retry=args.retry)))
            else:
                print(json.dumps({'dry_run': True, 'profiles': [build_profile(store.preview(job_id)).model_dump(mode='json') for job_id in ids]}))
        elif not args.apply:
            parser.error('process requires --apply; use preview for read-only generation')
        else:
            for tick in range(args.polls):
                print(json.dumps(process_batch(store, limit=args.limit)), flush=True)
                if tick + 1 < args.polls:
                    time.sleep(args.interval)
    except Exception as exc:
        # Sanitized command failure; no connection string or native payload dump.
        parser.exit(1, f'{type(exc).__name__}: job-profile command failed\n')


if __name__ == '__main__':
    main()
