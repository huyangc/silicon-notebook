"""Which libraries a finished global Ask is attributed to.

One rule, used by the completion hook (``GlobalAskService._note_completed``)
and by the overlay sampler's global arm (``ask_sample_merge``): a global run
counts for the libraries it actually retrieved from or cited; a run that made
no federated call at all (a document overview, a collection listing) counts
for its participants minus the ones it skipped. A library the run never
touched must neither be told "this member finished an Ask in you" nor see the
run's trace in that member's overlay sample. Pure leaf module.
"""
from __future__ import annotations

from typing import Iterable


def touched_notebook_ids(
    resolved: Iterable[str],
    searched: Iterable[str],
    cited: Iterable[str],
    skipped: Iterable[str],
) -> list[str]:
    """The attributed libraries, in participant (``resolved``) order."""
    ordered = [str(value) for value in dict.fromkeys(resolved) if str(value)]
    touched = {str(value) for value in searched} | {str(value) for value in cited}
    if not touched:
        left_out = {str(value) for value in skipped}
        touched = {value for value in ordered if value not in left_out}
    return [value for value in ordered if value in touched]


def attribution_sql(*, dialect: str, marker: str) -> tuple[str, int]:
    """The SAME rule as ``touched_notebook_ids``, as a WHERE fragment over one
    ``global_ask_jobs`` row's ``payload_json`` so it applies BEFORE ``LIMIT``
    (a post-``LIMIT`` filter would starve a library whose newest runs merely
    resolved it: the sample would fill up with rows that get dropped while
    older qualifying runs never reach the window). Returns the fragment and
    how many times the notebook id must be bound (the fragment repeats the
    placeholder). ``dialect`` is ``"sqlite"`` or ``"postgres"``; the caller
    keeps applying ``touched_notebook_ids`` afterwards, and a parity test pins
    the two spellings against the Python rule.
    """
    if dialect == "sqlite":
        def has(key: str) -> str:
            return (
                f"(json_type(json_extract(payload_json,'$.{key}')) = 'array' AND EXISTS("
                f"SELECT 1 FROM json_each(json_extract(payload_json,'$.{key}')) p WHERE p.value = {marker}))"
            )
        def empty(key: str) -> str:
            return (
                f"(json_type(json_extract(payload_json,'$.{key}')) IS NOT 'array' OR "
                f"json_array_length(json_extract(payload_json,'$.{key}')) = 0)"
            )
        skipped = (
            "NOT EXISTS(SELECT 1 FROM json_each(json_extract(payload_json,'$.skipped_notebooks')) s "
            f"WHERE json_extract(s.value,'$.notebook_id') = {marker})"
        )
        not_an_array = "json_type(json_extract(payload_json,'$.skipped_notebooks')) IS NOT 'array'"
        # Participation is a MANDATORY outer conjunct (the Python rule always
        # intersects attribution with ``resolved``): it is the one branch a
        # planner can answer from an index, and inside an OR it could not
        # restrict the candidate set -- a member with a long global history
        # and few runs in this library would have every payload parsed.
        fragment = (
            f"({has('resolved_notebook_ids')} AND "
            f"({has('searched_notebook_ids')} OR {has('cited_notebook_ids')} OR "
            f"({empty('searched_notebook_ids')} AND {empty('cited_notebook_ids')} AND "
            f"({not_an_array} OR {skipped}))))"
        )
        return fragment, 4
    if dialect == "postgres":
        def has(key: str) -> str:
            return f"(payload_json::jsonb->'{key}') @> jsonb_build_array({marker}::text)"
        def empty(key: str) -> str:
            return (
                f"(jsonb_typeof(payload_json::jsonb->'{key}') IS DISTINCT FROM 'array' OR "
                f"jsonb_array_length(payload_json::jsonb->'{key}') = 0)"
            )
        skipped = (
            "NOT EXISTS(SELECT 1 FROM jsonb_array_elements("
            "CASE WHEN jsonb_typeof(payload_json::jsonb->'skipped_notebooks') = 'array' "
            "THEN payload_json::jsonb->'skipped_notebooks' ELSE '[]'::jsonb END) s "
            f"WHERE s->>'notebook_id' = {marker})"
        )
        # Same shape as SQLite: the indexable participation containment
        # (``idx_global_ask_jobs_participants``) is the mandatory outer
        # conjunct, so the planner restricts the candidate set with it before
        # the searched/cited branches touch any payload.
        fragment = (
            f"({has('resolved_notebook_ids')} AND "
            f"({has('searched_notebook_ids')} OR {has('cited_notebook_ids')} OR "
            f"({empty('searched_notebook_ids')} AND {empty('cited_notebook_ids')} AND "
            f"{skipped})))"
        )
        return fragment, 4
    raise ValueError(dialect)
