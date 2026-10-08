"""Offline batch scope: keep notebook-wide per-source hooks out of large runs.

``scripts/batch_ingest.py`` phases process thousands of sources in one process.
Several per-source hooks of the online pipeline cost O(whole notebook) each
time they fire, so inside a batch they multiply into minutes of work and tens
of GB of memory (a 13M-node scale index loaded once per source, a
whole-notebook understanding consolidation every few sources, a notebook
name/description synthesis per source).  While an :func:`offline_batch_scope`
is active these hooks are skipped:

* automatic notebook name/description refresh (``suppress_notebook_metadata_refresh``);
* automatic scale-index build (``ScaleArtifactRuntime.maybe_auto_index``) and
  fold queueing (``maybe_enqueue_fold``);
* the agent-understanding consolidation trigger (``note_corpus_change`` still
  bumps its durable counter so the online service consolidates later);
* with ``OfflineBatchPolicy.defer_kg_extraction`` also per-source KG extraction
  (``SourceIngestionService.should_extract_kg``), so ``ingest`` never runs an
  LLM even into a notebook that already has a KG.

The batch phases do the notebook-wide work once at their end instead: ``kg`` /
``all`` rebuild the unified KG and the scale index explicitly.

The scope lives in a ``ContextVar``.  ``kg.scheduler.submit_job`` replays the
submitter's context in its worker; plain thread pools do not, so such callers
capture :func:`current_offline_batch_policy` on the submitting thread and
re-enter the scope inside the worker (see ``batch_ingest._resolve_tracked_future``).
"""
from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

from app.services.notebook_metadata import suppress_notebook_metadata_refresh

_F = TypeVar("_F", bound=Callable[..., object])


@dataclass(frozen=True)
class OfflineBatchPolicy:
    defer_kg_extraction: bool = False


_policy: ContextVar[OfflineBatchPolicy | None] = ContextVar(
    "offline_batch_policy", default=None
)


def current_offline_batch_policy() -> OfflineBatchPolicy | None:
    return _policy.get()


def offline_batch_active() -> bool:
    return _policy.get() is not None


def kg_extraction_deferred() -> bool:
    policy = _policy.get()
    return policy is not None and policy.defer_kg_extraction


@contextmanager
def offline_batch_scope(policy: OfflineBatchPolicy) -> Iterator[None]:
    token = _policy.set(policy)
    try:
        with suppress_notebook_metadata_refresh():
            yield
    finally:
        _policy.reset(token)


def in_offline_batch_scope(policy: OfflineBatchPolicy) -> Callable[[_F], _F]:
    """Decorator form: run the whole function body inside ``offline_batch_scope``."""

    def decorate(function: _F) -> _F:
        @functools.wraps(function)
        def wrapper(*args: object, **kwargs: object) -> object:
            with offline_batch_scope(policy):
                return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
