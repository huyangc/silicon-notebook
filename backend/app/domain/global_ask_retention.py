"""How long a global Ask turn outlives a deleted participant library.

Product decision (2026-10-19): when a library a global turn ran over is
deleted, that turn stays readable for its OWNER -- question and answer body
intact, citations into the deleted library degraded to "来源已删除" -- for the
same window the notebook-scoped Ask's content-minimal projection
(``retained_user_activity``) gets: ``USER_ACTIVITY_RETENTION_DAYS`` after the
deletion. Then it is cleaned up by the same mechanism that projection uses.

The mechanism, per turn (``global_ask_jobs.retained_until``, SQLite v95 /
PostgreSQL 0075):

* **stamp** -- the notebook delete transaction (the one that writes the
  ``retained_user_activity`` projection, right before ``DELETE FROM
  notebooks``) stamps every job whose ``resolved_notebook_ids`` contains the
  deleted library with the projection's own ``expires_at``; an earlier stamp
  (an earlier deletion) is kept, never pushed back. NULL means "no participant
  was ever deleted";
* **read gate** -- every read of a job's content excludes a job whose stamp
  has passed (``live_turn_sql``), against the database clock exactly like the
  ``retained_user_activity`` readers' ``expires_at`` gate;
* **purge** -- startup recovery and the next notebook deletion physically
  delete the expired jobs and every conversation they leave empty
  (``GlobalAskStore.purge_expired_turns``).

Only the turns that ran over the deleted library are retained and expire; the
conversation's other turns are untouched. The stamp is also the only thing that
lets an owner read a turn with a missing participant at all: an unstamped job
with a missing library (deleted before this mechanism existed, or imported from
elsewhere) keeps the strict "every participant readable" rule and stays 404.

Pure leaf module: SQL text only, no I/O.
"""
from __future__ import annotations


def live_turn_sql(*, dialect: str, alias: str = "") -> str:
    """``WHERE`` fragment: this ``global_ask_jobs`` row is not past its stamp.

    ``alias`` qualifies the column (``"g"`` -> ``g.retained_until``). The
    column is an aware ISO-8601 instant in text on both backends (the
    ``global_ask_jobs`` family keeps every instant in text) and is compared as
    an instant, never as text, against the database's own clock, the same clock the
    ``retained_user_activity`` readers gate ``expires_at`` on, so one deletion's
    projection and its global turns vanish at the same instant. No bind
    parameter: callers splice it in without touching their parameter lists.
    """
    column = f"{alias}.retained_until" if alias else "retained_until"
    if dialect == "postgres":
        return f"({column} IS NULL OR {column}::timestamptz > CURRENT_TIMESTAMP)"
    if dialect == "sqlite":
        return f"({column} IS NULL OR julianday({column}) > julianday('now'))"
    raise ValueError(dialect)


def conversation_live_sql(*, dialect: str, alias: str) -> str:
    """``WHERE`` fragment: the ``global_ask_conversations`` row ``alias`` still
    holds a live turn -- or no turn at all (a conversation is inserted with its
    first job, so that is never true for long). Only a conversation every one of
    whose turns is past its stamp disappears before the purge removes it."""
    # Spelled "holds no expired turn, OR holds a live one" -- the same truth
    # table as "holds a live turn, or no turn at all" -- so the probe that is
    # almost always decisive (no stamped turn in this conversation) reads the
    # partial ``idx_global_ask_jobs_retained (conversation_id, retained_until)``,
    # which holds no entry at all for an unstamped conversation.
    expired = (
        "lj.retained_until::timestamptz <= CURRENT_TIMESTAMP" if dialect == "postgres"
        else "julianday(lj.retained_until) <= julianday('now')"
    )
    return (
        "(NOT EXISTS(SELECT 1 FROM global_ask_jobs lj WHERE lj.conversation_id="
        f"{alias}.id AND lj.retained_until IS NOT NULL AND {expired}) "
        "OR EXISTS(SELECT 1 FROM global_ask_jobs aj WHERE aj.conversation_id="
        f"{alias}.id AND {live_turn_sql(dialect=dialect, alias='aj')}))"
    )


def retained_gone_participant_sql(*, dialect: str, job: str, participant: str) -> str:
    """``WHERE`` fragment: ``participant`` (an SQL expression naming one notebook
    id of the ``global_ask_jobs`` row ``job``) is a DELETED library of a
    RETAINED turn -- the turn carries a stamp and no ``notebooks`` row exists
    for the id. Such a participant no longer takes part in the owner's "every
    participant readable" rule (``GlobalAskService._check_turns`` is the
    Python twin); a library that still exists, readable or not, is not covered.
    """
    if dialect not in {"postgres", "sqlite"}:
        raise ValueError(dialect)
    return (
        f"({job}.retained_until IS NOT NULL AND NOT EXISTS("
        f"SELECT 1 FROM notebooks gone WHERE gone.id={participant}))"
    )


def participant_sql(*, dialect: str, marker: str) -> str:
    """``WHERE`` fragment: this job ran over the library bound at ``marker``.

    PostgreSQL spells it as the jsonb containment ``idx_global_ask_jobs_
    participants`` (0062) indexes, byte for byte the one the attribution rule
    uses (``global_ask_attribution.attribution_sql``), so the stamp at deletion
    reads only the matching rows. SQLite has no such index; ``instr`` on the
    raw payload is a cheap byte prefilter that lets the JSON walk run only on
    the rows that mention the id at all. One bind of the notebook id on
    PostgreSQL, two on SQLite (see ``participant_bind_count``).
    """
    if dialect == "postgres":
        return f"(payload_json::jsonb->'resolved_notebook_ids') @> jsonb_build_array({marker}::text)"
    if dialect == "sqlite":
        return (
            f"(instr(payload_json, {marker}) > 0 AND EXISTS(SELECT 1 FROM json_each("
            f"json_extract(payload_json,'$.resolved_notebook_ids')) p WHERE p.value = {marker}))"
        )
    raise ValueError(dialect)


def participant_bind_count(*, dialect: str) -> int:
    """How many times ``participant_sql`` binds the notebook id."""
    if dialect == "postgres":
        return 1
    if dialect == "sqlite":
        return 2
    raise ValueError(dialect)
