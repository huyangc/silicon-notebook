"""Shared tie-break regression data for ``AgentObservationStore`` — **both
backends consume the SAME id list and the SAME expected survivor set**.

Mirrors ``activity_parity_cases.py``'s own rationale: a fixed data table that
both ``tests/test_agent_observation_store.py`` (SQLite) and ``tests/postgres/
test_agent_observation_store_conformance.py`` (PostgreSQL) import, rather
than each hand-rolling its own — two independently-typed copies of "which
rows survive a tie" is exactly the kind of thing that can silently drift
apart (one backend's fixture matching its own implementation's bug, the
other's not).

**Why the id list is a SCRAMBLED permutation, not a monotonic sequence**: if
the ids were assigned in sorted order (as both stores' own test harnesses'
default ``new_id`` counters normally do — ``obs-000001``, ``obs-000002``,
...), then "keep the last N rows INSERTED" and "keep the top N rows by ``id``
DESC" would produce the IDENTICAL surviving set, and a regression that
dropped the ``id`` tie-break in favor of plain insertion order would pass
this fixture by accident. Shuffling the ids relative to insertion order (a
fixed-seed deterministic shuffle, so the fixture itself never changes
between runs) makes the two hypotheses diverge, so the assertion is actually
exercising the ``id DESC`` comparison, not merely "the store returns
something".

Not a test module (no ``test_`` prefix), so pytest does not collect it;
both test files ``import`` it.
"""
from __future__ import annotations

import random

from app.repositories.ports import AGENT_OBSERVATION_RING_MAX

#: A handful more than the ring bound, matching the existing (non-tie-break)
#: eviction tests' own margin in both files.
AGENT_OBSERVATION_TIE_BREAK_TOTAL = AGENT_OBSERVATION_RING_MAX + 5

_PERMUTATION = list(range(AGENT_OBSERVATION_TIE_BREAK_TOTAL))
# Fixed seed: this must produce the exact same permutation on every run and
# on both backends' test processes -- it is a shared FIXTURE, not a random
# smoke test.
random.Random(20260820).shuffle(_PERMUTATION)

#: The ids ``append_observation`` is fed, in INSERTION order. Zero-padded so
#: plain string comparison agrees with numeric comparison of the suffix.
AGENT_OBSERVATION_TIE_BREAK_IDS: tuple[str, ...] = tuple(
    f"obs-tie-{n:04d}" for n in _PERMUTATION
)

#: The ``AGENT_OBSERVATION_RING_MAX`` ids that must survive eviction when
#: EVERY row shares the exact same ``created_at`` — i.e. the tie is broken
#: purely on ``id`` descending. This is independent of insertion order by
#: construction (see the module docstring).
AGENT_OBSERVATION_TIE_BREAK_SURVIVORS: frozenset[str] = frozenset(
    sorted(AGENT_OBSERVATION_TIE_BREAK_IDS, reverse=True)[:AGENT_OBSERVATION_RING_MAX]
)
