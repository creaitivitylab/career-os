"""Host/container bridge integration tests; stdlib only, NO real Docker/DB/API.

Run: python3 -S -m unittest discover -s tests -p 'test_freshness_runtime.py' -v
The fake worker runs the real streamed driver with synthetic application modules.
Host python/python3 commands are poisoned; application imports exist only inside
the fake worker. No command in this test can invoke real Docker or ingestion.
"""
import ast
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ('_run-freshness.sh', '_freshness-worker.sh', 'freshness-runtime.py',
           'run-direct-light.sh', 'run-direct-heavy.sh', 'run-direct-ats.sh', 'freshness-report.sh')
LIGHT = ['smartrecruiters_direct', 'greenhouse_direct', 'workable_direct', 'ashby_direct', 'lever_direct']
HEAVY = ['workday_direct', 'successfactors_direct']
EXIT_CODES = {'SUCCESS': 0, 'SUCCESS_WITH_KNOWN_GAPS': 0, 'PARTIAL': 2, 'FAILED': 1, 'SKIPPED_LOCKED': 75}

FAKE_DOCKER = r'''
import json, os, pathlib, shutil, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
if args[:2] == ['compose', 'ps']:
    if os.environ.get('FAKE_PS_FAIL'): sys.exit(1)
    if not os.environ.get('FAKE_MISSING'): print('fake-running-worker')
    sys.exit(0)
if args[0] == 'exec':
    assert args[1:5] == ['-i', '-w', '/app', 'fake-running-worker']
    assert 'PYTHONPATH=/app' in args and 'PYTHONDONTWRITEBYTECODE=1' in args
    data = sys.stdin.buffer.read()
    if os.environ.get('FAKE_EXEC_FAIL'):
        print('fake docker exec failure', file=sys.stderr); sys.exit(42)
    index = args.index('-c')
    command = args[index + 1:]
    command[2] = str(pathlib.Path(os.environ['FAKE_REMOTE']) / pathlib.Path(command[2]).name)
    env = dict(os.environ, PYTHONPATH=os.environ['FAKE_APP'])
    result = subprocess.run([sys.executable, '-S', '-c', *command], input=data, env=env)
    sys.exit(int(os.environ.get('FAKE_DOCKER_EXIT', result.returncode)))
if args[0] == 'cp':
    if os.environ.get('FAKE_COPY_FAIL'): sys.exit(1)
    if os.environ.get('FAKE_COPY_PAUSE'):
        import time
        pathlib.Path(os.environ['FAKE_STARTED']).touch(); time.sleep(1)
    source = pathlib.Path(os.environ['FAKE_REMOTE']) / pathlib.Path(args[1].split(':', 1)[1].removesuffix('/.')).name
    if not source.is_dir(): sys.exit(1)
    shutil.copytree(source, args[2], dirs_exist_ok=True)
    sys.exit(0)
sys.exit('unexpected Docker operation')
'''

FAKE_APPLICATION = r'''
import json, os, pathlib, sys, time
import worker_only_dependency
LIGHT = ['smartrecruiters_direct', 'greenhouse_direct', 'workable_direct', 'ashby_direct', 'lever_direct']
HEAVY = ['workday_direct', 'successfactors_direct']
EXIT_CODES = {'SUCCESS': 0, 'SUCCESS_WITH_KNOWN_GAPS': 0, 'PARTIAL': 2, 'FAILED': 1, 'SKIPPED_LOCKED': 75}
def entrypoint(argv):
    previous = json.load(sys.stdin) if '--baseline-stdin' in argv else None
    with open(os.environ['FAKE_CALLS'], 'a') as f:
        f.write(json.dumps({'args': argv, 'previous': previous}) + '\n')
    if os.environ.get('FAKE_PAUSE'):
        pathlib.Path(os.environ['FAKE_STARTED']).touch(); time.sleep(1)
    print('worker application stderr', file=sys.stderr)
    if os.environ.get('FAKE_APP_EXCEPTION'): raise RuntimeError('do not expose secret')
    if '--apply' not in argv:
        sources = LIGHT if argv[0] == 'light' else HEAVY if argv[0] == 'heavy' else LIGHT + HEAVY
        print(json.dumps({'plan_only': True, 'group': argv[0], 'sources': sources, 'rmk_policy': 'validated scopes only', 'dedupe': False}))
        return 0
    state = os.environ.get('FAKE_STATUS', 'SUCCESS')
    result = {'schema_version': 'freshness-run-v1', 'runner_type': argv[0], 'status': state,
              'exit_code': EXIT_CODES[state], 'duration_seconds': 1.25,
              'coverage_degraded': state == 'SUCCESS_WITH_KNOWN_GAPS',
              'retry_metrics': {'transient_failures': 1, 'transient_retries': 1, 'retry_successes': 1, 'retry_failures': 0},
              'sources': [{'source_name': 'lever_direct', 'request': {'country': 'cz'}, 'fetched': 123,
                           'ingestion_status': 'success', 'scopes_completed': 1, 'scopes_attempted': 1,
                           'reviewed_scope': {'scope': 'jobgether', 'state': 'NEEDS_REVIEW'}}]}
    if os.environ.get('FAKE_MALFORMED'): result['schema_version'] = 'bad'
    if os.environ.get('FAKE_STATUS_MISMATCH'): result['exit_code'] = 88
    print(json.dumps(result))
    return EXIT_CODES[state]
'''

FAKE_REPORT = r'''
import json, pathlib, sys
import worker_only_dependency
def main(args):
    root = pathlib.Path(args[args.index('--output-dir') + 1])
    report = {'canonical': {'total': 3}, 'derived_status_counts': {'NEEDS_VERIFICATION': 3}}
    (root / 'summary.json').write_text(json.dumps(report))
    (root / 'report.md').write_text('Read-only fake freshness report\n')
    print(json.dumps(report))
    print('worker report stderr', file=sys.stderr)
    return 0
'''


class FreshnessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='freshness bridge with spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        for name in SCRIPTS:
            shutil.copy2(REPO / 'scripts' / name, self.root / 'scripts' / name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text(f'#!{sys.executable}\n' + FAKE_DOCKER)
        docker.chmod(0o755)
        for name in ('python', 'python3'):
            poison = self.bin / name
            poison.write_text('#!/bin/sh\nprintf "FORBIDDEN HOST PYTHON\\n" >&2\nexit 97\n')
            poison.chmod(0o755)
        self.app = self.root / 'worker' / 'app'
        self.app.mkdir(parents=True)
        (self.app / '__init__.py').touch()
        (self.app.parent / 'worker_only_dependency.py').touch()
        # Exercise the real baseline policy, with no production module imports.
        source = (REPO / 'worker/app/direct_freshness.py').read_text()
        baseline = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'next_baseline')
        (self.app / 'direct_freshness.py').write_text(FAKE_APPLICATION + '\n' + ast.get_source_segment(source, baseline) + '\n')
        (self.app / 'freshness_report.py').write_text(FAKE_REPORT)
        self.remote = self.root / 'container'
        self.remote.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        PYTHONDONTWRITEBYTECODE='1', FAKE_APP=str(self.app.parent),
                        FAKE_REMOTE=str(self.remote), FAKE_DOCKER_LOG=str(self.root / 'docker.jsonl'),
                        FAKE_CALLS=str(self.root / 'calls.jsonl'), FAKE_STARTED=str(self.root / 'started'))

    def run_script(self, script='run-direct-light.sh', args=(), **env):
        return subprocess.run(['bash', str(self.root / 'scripts' / script), *args],
                              cwd='/', env=dict(self.env, **env), text=True, capture_output=True, timeout=10)

    def artifacts(self, report=False):
        parent = self.root / 'logs/freshness' / ('reports' if report else 'runs')
        return list(parent.glob('*/artifacts'))

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def baseline(self, group='light'):
        return self.root / f'logs/freshness/baseline-{group}.json'

    def test_light_plan_without_host_application_dependencies(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['sources'], LIGHT)
        self.assertNotIn('FORBIDDEN', r.stderr)
        self.assertFalse(self.baseline().exists())
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn('--apply', self.calls()[0]['args'])

    def test_heavy_plan_validated_scope_policy(self):
        r = self.run_script('run-direct-heavy.sh')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['sources'], HEAVY)
        self.assertEqual(json.loads(r.stdout)['rmk_policy'], 'validated scopes only')

    def test_legacy_runner_uses_same_bridge(self):
        r = self.run_script('run-direct-ats.sh')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['sources'], LIGHT + HEAVY)

    def test_report_via_worker_and_host_artifacts(self):
        r = self.run_script('freshness-report.sh')
        self.assertEqual(r.returncode, 0, r.stderr)
        artifact = self.artifacts(report=True)[0]
        self.assertEqual(json.loads((artifact / 'summary.json').read_text())['canonical']['total'], 3)
        self.assertIn('fake freshness report', (artifact / 'report.md').read_text())
        self.assertIn('worker report stderr', r.stderr)

    def assert_outcome(self, state):
        r = self.run_script(args=['--apply'], FAKE_STATUS=state)
        self.assertEqual(r.returncode, EXIT_CODES[state], r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'], state)
        raw = json.loads((self.artifacts()[0] / 'result.json').read_text())
        self.assertEqual(raw['status'], state)
        self.assertEqual(len(self.calls()), 1, 'No whole-source/group retries')
        return r

    def test_apply_success_exit_zero(self):
        self.assert_outcome('SUCCESS')

    def test_known_gaps_exit_zero_visible(self):
        r = self.assert_outcome('SUCCESS_WITH_KNOWN_GAPS')
        self.assertIn('COVERAGE GAP', r.stderr)
        self.assertEqual(json.loads(r.stdout)['sources'][0]['reviewed_scope']['state'], 'NEEDS_REVIEW')

    def test_partial_exit_two_not_tee_zero(self):
        self.assertIn('ALERT', self.assert_outcome('PARTIAL').stderr)

    def test_failed_exit_one_not_tee_zero(self):
        self.assert_outcome('FAILED')

    def test_worker_locked_exit_75_preserves_baseline(self):
        self.baseline().parent.mkdir(parents=True)
        old = {'schema_version': 'freshness-run-v1', 'runner_type': 'light', 'sources': []}
        self.baseline().write_text(json.dumps(old))
        self.assert_outcome('SKIPPED_LOCKED')
        self.assertEqual(json.loads(self.baseline().read_text()), old)

    def test_retry_metrics_preserved_in_stdout_and_artifact(self):
        r = self.assert_outcome('SUCCESS')
        result = json.loads(r.stdout)
        self.assertEqual(result['retry_metrics']['transient_retries'], 1)
        self.assertEqual(result['retry_metrics']['retry_successes'], 1)
        self.assertEqual(result, json.loads((self.artifacts()[0] / 'result.json').read_text()))

    def test_stdout_stderr_saved_separately(self):
        r = self.run_script(args=['--apply'])
        run = self.artifacts()[0].parent
        self.assertEqual((run / 'command.stdout').read_text(), r.stdout)
        self.assertEqual((run / 'command.log').read_text(), 'worker application stderr\n')
        self.assertIn('worker application stderr', r.stderr)

    def test_baseline_round_trip_stays_on_host(self):
        self.assertEqual(self.run_script(args=['--apply']).returncode, 0)
        first = json.loads(self.baseline().read_text())
        self.assertEqual(first['sources'][0]['fetched'], 123)
        self.assertEqual(self.run_script(args=['--apply']).returncode, 0)
        self.assertEqual(self.calls()[1]['previous'], first)
        self.assertEqual(json.loads(self.baseline().read_text()), first)

    def test_missing_worker_fails_no_fallback(self):
        r = self.run_script(FAKE_MISSING='1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('running Compose worker', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_docker_lookup_failure(self):
        r = self.run_script(FAKE_PS_FAIL='1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot locate', r.stderr)

    def test_docker_exec_failure(self):
        r = self.run_script(args=['--apply'], FAKE_EXEC_FAIL='1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('fake docker exec failure', r.stderr)
        self.assertFalse(self.baseline().exists())

    def test_application_exception_safe_failure(self):
        r = self.run_script(args=['--apply'], FAKE_APP_EXCEPTION='1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('RuntimeError', r.stderr)
        self.assertNotIn('do not expose secret', r.stderr)
        self.assertFalse(self.baseline().exists())
        self.assertEqual(len(self.calls()), 1)

    def test_missing_worker_dependency_fails_before_apply(self):
        (self.app.parent / 'worker_only_dependency.py').unlink()
        r = self.run_script(args=['--apply'])
        self.assertEqual(r.returncode, 1)
        self.assertIn('ModuleNotFoundError', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_validator_import_fails_before_apply(self):
        module = self.app / 'direct_freshness.py'
        module.write_text(module.read_text().replace('def next_baseline(', 'def removed_baseline('))
        r = self.run_script(args=['--apply'])
        self.assertEqual(r.returncode, 1)
        self.assertIn('ImportError', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_artifact_copy_failure_retains_raw_logs_no_baseline(self):
        r = self.run_script(args=['--apply'], FAKE_COPY_FAIL='1')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)['status'], 'SUCCESS')
        self.assertFalse(self.baseline().exists())
        self.assertIn('inspect state before any retry', r.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_docker_code_mismatch_cannot_promote_baseline(self):
        r = self.run_script(args=['--apply'], FAKE_DOCKER_EXIT='42')
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.baseline().exists())

    def test_invalid_result_rejected(self):
        r = self.run_script(args=['--apply'], FAKE_MALFORMED='1')
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.baseline().exists())

    def test_result_exit_code_mismatch_rejected(self):
        r = self.run_script(args=['--apply'], FAKE_STATUS_MISMATCH='1')
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.baseline().exists())

    def test_invalid_baseline_preflight_prevents_any_application_call(self):
        self.baseline().parent.mkdir(parents=True)
        self.baseline().write_text('{malformed')
        r = self.run_script(args=['--apply'])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.baseline().read_text(), '{malformed')

    def test_malformed_baseline_rows_rejected_before_apply(self):
        self.baseline().parent.mkdir(parents=True)
        self.baseline().write_text(json.dumps({'schema_version': 'freshness-run-v1',
                                              'runner_type': 'light', 'sources': [{}]}))
        self.assertEqual(self.run_script(args=['--apply']).returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_wrong_group_baseline_rejected_before_apply(self):
        self.baseline().parent.mkdir(parents=True)
        self.baseline().write_text(json.dumps({'schema_version': 'freshness-run-v1',
                                              'runner_type': 'heavy', 'sources': []}))
        self.assertEqual(self.run_script(args=['--apply']).returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_plan_does_not_depend_on_baseline_validity(self):
        self.baseline().parent.mkdir(parents=True)
        self.baseline().write_text('{malformed')
        self.assertEqual(self.run_script().returncode, 0)

    def test_no_apply_abbreviation_can_trigger_writes(self):
        for flag in ['--a', '--ap', '--app', '--appl']:
            with self.subTest(flag=flag):
                r = self.run_script(args=[flag])
                self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_host_lock_shared_by_light_heavy_legacy(self):
        lock = self.root / 'logs/freshness/runner.lock'
        lock.parent.mkdir(parents=True)
        with lock.open('w') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for script in ['run-direct-light.sh', 'run-direct-heavy.sh', 'run-direct-ats.sh']:
                with self.subTest(script=script):
                    r = self.run_script(script)
                    self.assertEqual(r.returncode, 75)
                    self.assertEqual(json.loads(r.stdout)['status'], 'SKIPPED_LOCKED')
            self.assertEqual(self.calls(), [])

    def test_lock_released_after_success(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script('run-direct-heavy.sh').returncode, 0)

    def test_lock_released_after_failure(self):
        self.assertEqual(self.run_script(FAKE_EXEC_FAIL='1').returncode, 1)
        self.assertEqual(self.run_script().returncode, 0)

    def test_lock_tool_failure_is_not_contention(self):
        flock = self.bin / 'flock'
        flock.write_text('#!/bin/sh\nexit 1\n')
        flock.chmod(0o755)
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn('lock could not be acquired', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_parallel_light_and_heavy_cannot_overlap(self):
        first = subprocess.Popen(['bash', str(self.root / 'scripts/run-direct-light.sh')],
                                 env=dict(self.env, FAKE_PAUSE='1'), text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'started').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / 'started').exists())
            r = self.run_script('run-direct-heavy.sh')
            self.assertEqual(r.returncode, 75)
            _, err = first.communicate(timeout=5)
            self.assertEqual(first.returncode, 0, err)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_host_lock_covers_artifact_recovery(self):
        first = subprocess.Popen(['bash', str(self.root / 'scripts/run-direct-light.sh')],
                                 env=dict(self.env, FAKE_COPY_PAUSE='1'), text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'started').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / 'started').exists())
            self.assertEqual(self.run_script('run-direct-heavy.sh').returncode, 75)
            _, err = first.communicate(timeout=5)
            self.assertEqual(first.returncode, 0, err)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_log_transport_failure_cannot_report_success(self):
        tee = self.bin / 'tee'
        tee.write_text('#!/bin/sh\ncat >/dev/null\nexit 86\n')
        tee.chmod(0o755)
        r = self.run_script(args=['--apply'])
        self.assertEqual(r.returncode, 1)
        self.assertIn('log transport failed', r.stderr)
        self.assertFalse(self.baseline().exists())
        self.assertEqual(len(self.calls()), 1)

    def test_container_identity_pinned_for_execution_and_copy(self):
        self.assertEqual(self.run_script().returncode, 0)
        calls = [json.loads(line) for line in (self.root / 'docker.jsonl').read_text().splitlines()]
        self.assertEqual(len([c for c in calls if c[0] == 'compose']), 1)
        self.assertEqual(next(c for c in calls if c[0] == 'exec')[4], 'fake-running-worker')
        self.assertTrue(next(c for c in calls if c[0] == 'cp')[1].startswith('fake-running-worker:'))

    def test_stale_lock_file_never_blocks(self):
        lock = self.root / 'logs/freshness/runner.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('old process no longer exists')
        self.assertEqual(self.run_script().returncode, 0)

    def test_host_imports_do_not_exist(self):
        # Both package absence and poisoned python executables are deliberate.
        r = subprocess.run([sys.executable, '-S', '-c',
                            'import importlib.util; assert importlib.util.find_spec("httpx") is None; '
                            'assert importlib.util.find_spec("worker_only_dependency") is None'],
                           cwd=self.root, env=dict(self.env, PYTHONPATH=''), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for script in ['run-direct-light.sh', 'run-direct-heavy.sh', 'freshness-report.sh']:
            self.assertEqual(self.run_script(script).returncode, 0)


if __name__ == '__main__':
    unittest.main()
