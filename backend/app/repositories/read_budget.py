"""Execution-local deadlines for bounded read-only repository work."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


class ReadBudgetExceeded(TimeoutError):
    """The caller's read budget expired; contains no database diagnostics."""


@dataclass(frozen=True)
class ReadBudget:
    deadline: float
    cancel_event: object = None

    def remaining_seconds(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or (
            self.cancel_event is not None and self.cancel_event.is_set()
        ):
            raise ReadBudgetExceeded("read budget exhausted")
        return remaining

    def check(self) -> None:
        self.remaining_seconds()


_CURRENT = ContextVar("repository_read_budget", default=None)


def current_read_budget():
    return _CURRENT.get()


@contextmanager
def read_budget(deadline: float, cancel_event=None):
    previous = current_read_budget()
    budget = ReadBudget(
        min(deadline, previous.deadline) if previous else deadline,
        cancel_event if cancel_event is not None else (
            previous.cancel_event if previous else None
        ),
    )
    token = _CURRENT.set(budget)
    try:
        budget.check()
        yield budget
    finally:
        _CURRENT.reset(token)
