"""One fresh read-only inventory retry; never repeat database processing/COMMIT."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import errno
import logging
import math
import socket
import time

import httpx

from app.operation_budget import check_deadline, remaining_seconds

LIGHT_SOURCES = {'smartrecruiters_direct', 'greenhouse_direct', 'workable_direct',
                 'ashby_direct', 'lever_direct'}
RETRY_STATUSES = {429, 502, 503, 504}
METRICS = ('transient_failures', 'transient_retries', 'retry_successes', 'retry_failures')
_policy = ContextVar('light_inventory_retry_policy', default=None)
_collectors = ContextVar('light_inventory_acquisitions', default=None)
log = logging.getLogger('career_os.freshness')


class UpstreamHTTPError(RuntimeError):
    def __init__(self, status_code, context, retry_after=None):
        self.status_code = status_code
        self.retry_after = retry_after
        # Do not copy response bodies/credentials into operational metadata.
        super().__init__(f'{context} error {status_code}')


def validate_policy(config):
    retries, delay = config['max_transient_retries'], config['retry_backoff_seconds']
    if type(retries) is not int or retries not in (0, 1):
        raise ValueError('Transient retry limit must be 0 or 1')
    if (isinstance(delay, bool) or not isinstance(delay, (int, float))
            or not math.isfinite(delay) or not 5 <= delay <= 15):
        raise ValueError('Transient retry backoff must be between 5 and 15 seconds')


@contextmanager
def transient_retry_policy(config):
    if config is not None:
        validate_policy(config)
    token = _policy.set(None if config is None else {
        key: config[key] for key in ('max_transient_retries', 'retry_backoff_seconds')})
    acquisitions = []
    collector_token = _collectors.set(acquisitions)
    try:
        yield acquisitions
    finally:
        _collectors.reset(collector_token)
        _policy.reset(token)


def retryable(exc):
    if isinstance(exc, (UpstreamHTTPError, httpx.HTTPStatusError)):
        status = exc.status_code if isinstance(exc, UpstreamHTTPError) else exc.response.status_code
        return status in RETRY_STATUSES
    if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout)):
        return True
    # Only positively identified transient network causes, never message guessing,
    # generic ConnectError, TLS/certificate errors, NXDOMAIN or local pool timeouts.
    if not isinstance(exc, (httpx.NetworkError, OSError)):
        return False
    seen = set()
    cause = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ConnectionResetError):
            return True
        if isinstance(cause, OSError) and cause.errno in {
                errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, socket.EAI_AGAIN}:
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def retry_after_seconds(exc):
    value = (exc.retry_after if isinstance(exc, UpstreamHTTPError) else
             exc.response.headers.get('Retry-After') if isinstance(exc, httpx.HTTPStatusError) else None)
    if value is None:
        return 0
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            return math.inf
        return seconds
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return math.inf
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return math.inf  # Unusable server guidance is not permission to retry early.


class InventoryAcquisition:
    """Callbacks MUST be read-only and construct fresh traversal state each call.

    Lifecycle snapshots/observations and source writes happen only after run()
    returns. Non-guarded HTTP endpoints retain single-attempt behavior.
    """
    def __init__(self, source, *, clock=None, sleep=None):
        self.source = source
        self.policy = _policy.get() if source in LIGHT_SOURCES else None
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.records = []
        self.started = {}
        collector = _collectors.get()
        if collector is not None:
            collector.append(self)

    @property
    def enabled(self):
        return bool(self.policy and self.policy['max_transient_retries'])

    def run(self, scope, acquire):
        if tuple(scope) in self.started:
            raise ValueError('Scope acquisition already attempted')
        started = self.clock()
        self.started[tuple(scope)] = started
        record = {'scope': list(scope), 'attempts': [], 'retry_attempted': False,
                  'retry_delay_seconds': 0, 'retry_result': 'not_attempted',
                  'final_scope_result': 'FAILED', 'duration_seconds': 0,
                  'metrics': dict.fromkeys(METRICS, 0)}
        self.records.append(record)
        for index in range(2 if self.enabled else 1):
            attempt_start = self.clock()
            try:
                check_deadline()
                value = acquire()
            except Exception as exc:
                elapsed = self.clock() - attempt_start
                transient = retryable(exc)
                record['metrics']['transient_failures'] += int(transient)
                record['attempts'].append({'attempt': index + 1, 'result': 'FAILED',
                    'error_class': type(exc).__name__, 'retryable': transient,
                    'http_status': (exc.status_code if isinstance(exc, UpstreamHTTPError) else
                                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None),
                    'duration_seconds': round(elapsed, 3)})
                record['duration_seconds'] = round(self.clock() - started, 3)
                if index:
                    record['retry_result'] = 'FAILED'
                    record['metrics']['retry_failures'] = 1
                reason = ('retry_exhausted' if index else 'non_retryable' if not transient else
                          'retry_disabled' if not self.enabled else None)
                if reason is None:
                    delay = self.policy['retry_backoff_seconds']
                    remaining = remaining_seconds()
                    # Reserve at least one native 60s request timeout or the
                    # previous traversal duration; all adapter deadline checks remain.
                    reserve = max(60, elapsed)
                    if retry_after_seconds(exc) > delay:
                        reason = 'retry_after_exceeds_bounded_backoff'
                    elif remaining is None or remaining <= delay + reserve:
                        reason = 'insufficient_operation_budget'
                    else:
                        log.warning('transient inventory failure source=%s scope=%s error_class=%s retry_delay=%s',
                                    self.source, list(scope), type(exc).__name__, delay)
                        self.sleep(delay)
                        record['retry_delay_seconds'] = delay
                        remaining = remaining_seconds()
                        if remaining is None or remaining <= reserve:
                            reason = 'insufficient_budget_after_backoff'
                        else:
                            record['retry_attempted'] = True
                            record['metrics']['transient_retries'] = 1
                            continue
                record['retry_skip_reason'] = reason
                record['duration_seconds'] = round(self.clock() - started, 3)
                raise
            record['attempts'].append({'attempt': index + 1, 'result': 'ACQUIRED',
                'error_class': None, 'retryable': False,
                'duration_seconds': round(self.clock() - attempt_start, 3)})
            if index:
                record['retry_result'] = 'ACQUIRED'
                log.info('inventory retry acquired source=%s scope=%s', self.source, list(scope))
            record['final_scope_result'] = 'ACQUIRED'
            record['duration_seconds'] = round(self.clock() - started, 3)
            return value

    def finish(self, inventory, completion):
        result = inventory.finish(completion)
        record = next((r for r in reversed(self.records) if r['scope'] == list(inventory.parts)), None)
        if record is not None:
            record['final_scope_result'] = ('COMPLETE' if result['complete'] else
                                             'INCOMPLETE' if result['successful'] else 'FAILED')
            record['lifecycle'] = result
            record['duration_seconds'] = round(self.clock() - self.started[tuple(inventory.parts)], 3)
            if record['retry_attempted']:
                record['retry_result'] = record['final_scope_result']
                record['metrics']['retry_successes'] = int(result['complete'])
                record['metrics']['retry_failures'] = int(not result['complete'])
        return result

    def summary(self):
        return {'scope_attempts': self.records,
                'retry_metrics': {key: sum(r['metrics'][key] for r in self.records) for key in METRICS}}


def acquisition_summary(acquisitions, *, aborted=False):
    if aborted:
        for acquisition in acquisitions:
            for record in acquisition.records:
                if record['final_scope_result'] == 'ACQUIRED':
                    record['final_scope_result'] = 'ABORTED'
                    record['duration_seconds'] = round(
                        acquisition.clock() - acquisition.started[tuple(record['scope'])], 3)
                    if record['retry_attempted']:
                        record['retry_result'] = 'ABORTED'
                        record['metrics']['retry_failures'] = 1
    records = [record for acquisition in acquisitions for record in acquisition.records]
    return {'scope_attempts': records,
            'retry_metrics': {key: sum(r['metrics'][key] for r in records) for key in METRICS}}
