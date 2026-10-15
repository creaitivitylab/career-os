"""Cooperative deadlines at transaction-safe ingestion boundaries.

Absent a runner context, existing endpoint behavior is unchanged. Never interrupt
COMMIT, kill a worker, or retry an ambiguous transaction automatically.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


class DeadlineExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    deadline: float
    clock: object = time.monotonic
    commit_started: bool = False
    commit_succeeded: bool = False
    timed_out: bool = False
    run_id: str | None = None


_budget = ContextVar("direct_ingestion_budget", default=None)


def note_run_id(run_id):
    budget = _budget.get()
    if budget is not None:
        budget.run_id = str(run_id)


def check_deadline():
    budget = _budget.get()
    if budget is not None and budget.clock() >= budget.deadline:
        budget.timed_out = True
        raise DeadlineExceeded("Cooperative ingestion deadline exceeded; rollback required")


def remaining_seconds():
    """Remaining shared source/group budget, or None outside guarded execution."""
    budget = _budget.get()
    return max(0, budget.deadline - budget.clock()) if budget is not None else None


def commit_inventory(conn):
    check_deadline()
    budget = _budget.get()
    if budget is not None:
        budget.commit_started = True
    conn.commit()
    if budget is not None:
        budget.commit_succeeded = True


@contextmanager
def ingestion_budget(deadline, clock=time.monotonic):
    budget = Budget(deadline, clock)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)
