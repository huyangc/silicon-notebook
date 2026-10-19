"""SQL for counting global Ask ratings (👍 / 👎) beside the notebook
``feedback`` table. Pure leaf module, shared by both backends' query stores.

A global rating lives only in ``global_ask_jobs.payload_json`` (``feedback``,
first write wins: ``GlobalAskStore.set_feedback``). The PARTIAL expression
index ``idx_global_ask_jobs_feedback`` (SQLite v96 / PostgreSQL 0076) holds
exactly the rated rows, so every count here carries ``rated_predicate`` and
reads only those rows:

* SQLite matches a partial index's WHERE clause structurally against a term
  of the query's WHERE, so ``rated_predicate("sqlite")`` must stay the very
  expression the migration indexes (``test_global_ask_feedback_counts.py``
  pins the two spellings together); a qualified column (``g.payload_json``)
  still matches.
* PostgreSQL proves the index predicate from the query's, so the
  ``rating_is`` single-value form (``= 'useful'``) uses the index too, as an
  index condition that never re-reads the payload.
"""
from __future__ import annotations

RATINGS = ("useful", "not_useful")


def rating_expression(dialect: str, *, column: str = "payload_json") -> str:
    """The indexed expression: the job's rating ('' / absent = unrated)."""
    if dialect == "sqlite":
        return f"json_extract({column},'$.feedback')"
    if dialect == "postgres":
        return f"({column}::jsonb->>'feedback')"
    raise ValueError(dialect)


def rated_predicate(dialect: str, *, column: str = "payload_json") -> str:
    """The partial index's own predicate: the job carries a rating."""
    return f"{rating_expression(dialect, column=column)} IN ('useful','not_useful')"


def rating_is(dialect: str, rating: str, *, column: str = "payload_json") -> str:
    """One rating, as a condition the partial index can answer."""
    if rating not in RATINGS:
        raise ValueError(rating)
    if dialect == "sqlite":
        # SQLite only uses the partial index when the query repeats its exact
        # predicate, so the equality rides beside it.
        return (
            f"{rated_predicate(dialect, column=column)} AND "
            f"{rating_expression(dialect, column=column)} = '{rating}'"
        )
    return f"{rating_expression(dialect, column=column)} = '{rating}'"


# The admin question overview's stats row. A notebook question's 👍 / 👎 are
# its answer's ``feedback`` rows -- the same rows the notebook analytics panel
# counts -- grouped once so the join probes one row per answer; a global job's
# rating is read by value through the partial index (one index condition per
# rating returning ids only, so no payload is parsed for it). Both joins hang
# off the OUTER ``questions`` rows, never inside a CTE arm: a join inside the
# global arm stops PostgreSQL from dropping that arm's unused ``question``
# output, and every global payload would then be parsed for a count.
NOTEBOOK_FEEDBACK_BY_ANSWER_SQL = (
    "SELECT answer_id,"
    "SUM(CASE WHEN rating='useful' THEN 1 ELSE 0 END) AS useful,"
    "SUM(CASE WHEN rating='not_useful' THEN 1 ELSE 0 END) AS not_useful "
    "FROM feedback GROUP BY answer_id"
)
ADMIN_QUESTION_FEEDBACK_SUMS = (
    "SUM(COALESCE(fb.useful,0)+CASE WHEN gf.rating='useful' THEN 1 ELSE 0 END) AS feedback_useful,"
    "SUM(COALESCE(fb.not_useful,0)+CASE WHEN gf.rating='not_useful' THEN 1 ELSE 0 END) "
    "AS feedback_not_useful,"
    "SUM(CASE WHEN gf.rating='useful' THEN 1 ELSE 0 END) AS global_feedback_useful,"
    "SUM(CASE WHEN gf.rating='not_useful' THEN 1 ELSE 0 END) AS global_feedback_not_useful"
)
ADMIN_QUESTION_FEEDBACK_KEYS = (
    "feedback_useful",
    "feedback_not_useful",
    "global_feedback_useful",
    "global_feedback_not_useful",
)


def admin_question_feedback_joins(dialect: str) -> str:
    """The two LEFT JOINs the stats row sums over, against a ``questions``
    CTE that carries ``answer_id`` (``_admin_questions_cte(feedback=True)``).
    Each right side is unique on its key, so no question row is repeated."""
    text = "::text" if dialect == "postgres" else ""
    return (
        f"LEFT JOIN ({NOTEBOOK_FEEDBACK_BY_ANSWER_SQL}) fb ON fb.answer_id=questions.answer_id "
        "LEFT JOIN ("
        f"SELECT id,'useful'{text} AS rating FROM global_ask_jobs "
        f"WHERE {rating_is(dialect, 'useful')} "
        "UNION ALL "
        f"SELECT id,'not_useful'{text} AS rating FROM global_ask_jobs "
        f"WHERE {rating_is(dialect, 'not_useful')}"
        ") gf ON gf.id=questions.id AND questions.scope='global' "
    )


def admin_question_feedback_totals(stats) -> dict[str, int]:
    """The four feedback sums of one stats row (NULL on an empty page = 0)."""
    return {key: int(stats[key] or 0) for key in ADMIN_QUESTION_FEEDBACK_KEYS}
