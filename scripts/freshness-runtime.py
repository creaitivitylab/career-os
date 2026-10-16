"""Operational driver streamed to, and executed ONLY inside, the worker.

It imports the running image's application, not host application sources. The
host framing bootstrap passes baseline JSON on stdin after this script's bytes.
No ingestion command is retried here. Source transaction guards own correctness;
result validation cannot undo an already committed source transaction.
"""
import contextlib
import io
import json
from pathlib import Path
import sys


class Output(io.StringIO):
    """Keep unmodified application stdout both in host logs and for validation."""
    def write(self, value):
        sys.__stdout__.write(value)
        sys.__stdout__.flush()
        return super().write(value)


@contextlib.contextmanager
def input_json(value):
    previous_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(value))
        yield
    finally:
        sys.stdin = previous_stdin


def execute(mode, directory, group, args):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    baseline_input = sys.stdin.read()
    capture = Output()
    if mode == 'report':
        from app.freshness_report import main
        if any(a.startswith('--output') for a in args):
            raise ValueError('Output directory is managed by the shell bridge')
        with contextlib.redirect_stdout(capture):
            status = main(['--output-dir', str(directory), *args])
        json.loads((directory / 'summary.json').read_text())
        if status != 0 or not (directory / 'report.md').is_file():
            raise ValueError('Invalid report result')
    elif mode == 'runner':
        # Import ALL result/baseline dependencies before any possible writes.
        from app.direct_freshness import entrypoint, next_baseline, EXIT_CODES
        if any(a.startswith('--baseline') or
               (a.startswith('--') and a != '--apply' and '--apply'.startswith(a)) for a in args):
            raise ValueError('Use explicit --apply; baseline input is managed by the bridge')
        apply = '--apply' in args
        previous = json.loads(baseline_input) if apply else None
        if apply and previous is not None:
            if (not isinstance(previous, dict) or previous.get('schema_version') != 'freshness-run-v1'
                    or previous.get('runner_type') != group or not isinstance(previous.get('sources'), list)):
                raise ValueError('Invalid baseline; refused before ingestion')
            next_baseline(previous, {'runner_type': group, 'sources': []})
            args = [*args, '--baseline-stdin']
        with contextlib.redirect_stdout(capture), input_json(previous):
            status = entrypoint([group, *args])
        raw = capture.getvalue()
        (directory / ('result.json' if apply else 'plan.json')).write_text(raw)
        result = json.loads(raw)
        if apply:
            if (result.get('schema_version') != 'freshness-run-v1' or result.get('runner_type') != group
                    or result.get('exit_code') != status or status != EXIT_CODES[result['status']]):
                raise ValueError('Invalid operational result')
            if result['status'] != 'SKIPPED_LOCKED':
                (directory / 'baseline.json').write_text(json.dumps(next_baseline(previous, result), indent=2) + '\n')
            if result.get('alert_worthy', result['status'] in {'PARTIAL', 'FAILED'}):
                print('ALERT: freshness execution requires operator review.', file=sys.stderr)
            elif result.get('coverage_degraded'):
                print('COVERAGE GAP: reviewed scopes remain unverified.', file=sys.stderr)
        elif status != 0 or result.get('plan_only') is not True or result.get('group') != group:
            raise ValueError('Invalid plan result')
    else:
        raise ValueError('Unknown operational mode')
    (directory / 'validated.json').write_text(json.dumps({'exit_code': status, 'mode': mode}) + '\n')
    (directory / f'validated-{status}').touch()
    return status


def main():
    try:
        return execute(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
    except Exception as exc:
        # Never include arbitrary exception messages/credentials. No automatic
        # retry: a source may have committed before operational validation failed.
        print(f'FAILED: worker operational workflow ({type(exc).__name__}); inspect logs and ingestion_runs before any retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
