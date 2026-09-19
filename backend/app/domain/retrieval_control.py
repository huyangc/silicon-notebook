"""Retrieval-layer control exceptions that fail-soft handlers must NOT swallow.

The retrieval path is full of ``except Exception`` handlers that degrade
gracefully on purpose: one library's timeout must not fail the whole arm, a
lexical probe blowing up must not cost the semantic one, telemetry must never
break retrieval.  That discipline is correct for DEGRADATIONS and wrong for
CONTROL FLOW, so every such handler names its control exceptions first and
re-raises them (``except AskCancelled: raise``).

``ParticipantOverrideError`` is the second member of that category, and it
lives here -- in the dependency-free domain layer, beside
``cancellation.AskCancelled`` -- rather than in
``app.services.retrieval_participants`` for one structural reason: a fail-soft
handler in a module that must NOT be able to read the participant override
(``reasoning_retrieval``, ``ask_service``, ``report_engine`` -- see the frozen
reader whitelist in ``backend/tests/test_participant_override_guard.py``) still
has to be able to name this exception in order to re-raise it.  Importing it
from here gives that module the name and nothing else: no accessor, no
override, no way to install one.

⛔ Deliberately an ``Exception``, not a ``BaseException``.  Escaping every
``except Exception`` in the process sounds safer and is not: ``global_ask._run``
catches ``Exception`` to mark its job failed, so a ``BaseException`` would
leave the job stuck in ``running`` forever -- trading a swallowed error for an
unfinished one.  The contract is therefore "named and re-raised by every
fail-soft handler on the path", enforced by the handlers themselves and pinned
by a guard test, not by the class hierarchy.

⛔ Deliberately NOT a subclass of ``AskCancelled``.  That would get it
re-raised for free by the ~95 existing ``except AskCancelled`` sites, but a
third of those do not re-raise: they treat cancellation as a durable terminal
transition and would report an identity mismatch to the user as "已取消".  A
silent wrong answer replaced by a silent wrong status is not the fix.
"""
from __future__ import annotations


class RetrievalControlError(Exception):
    """Base class for retrieval control flow that must reach the caller.

    A marker, so a handler can re-raise the whole category without importing
    each member, and so a future member inherits the existing re-raises instead
    of needing its own sweep.
    """


class ParticipantOverrideError(RetrievalControlError):
    """An override was installed or used outside its contract.

    Every message is content-free on purpose: these raise on identity
    mismatches, so the operands are a user id and a notebook id.  Naming them
    in an exception puts them in tracebacks, logs and error responses, which is
    the one place a scope-isolation failure must not start leaking the very
    identifiers it was protecting.  The failing call site is in the traceback
    already; the values are recoverable from the debugger, not from the log.
    """


__all__ = ["ParticipantOverrideError", "RetrievalControlError"]
