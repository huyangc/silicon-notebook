"""The PostgreSQL statements that read the conversation's last answer read ONE
payload.

``list_conversations`` and the MCP one-turn read (``conversation_turn``) both
need the ``linked_report`` receipt of a conversation's LAST answer. Extracting it
inside the ``ORDER BY ... LIMIT 1`` subquery decodes every answer's payload
under the sort; the statements instead locate the last answer by ``ordinal`` and
extract from that one row. The text guard is in
``tests/answers_last_read_sql_testkit.py`` (why not an EXPLAIN pin: there); this
file holds the production constants to it, and the guard itself to a known-bad
and a known-good statement so a guard that can no longer fail is caught here.
"""
from __future__ import annotations

import pytest

from app.repositories.postgres.ask_state_store import (
    CONVERSATION_TURN_HEAD_SQL,
    LAST_ANSWER_JOIN_SQL,
    LAST_ANSWER_LINKED_REPORT_SQL,
    LIST_CONVERSATIONS_SQL,
)
from tests.answers_last_read_sql_testkit import (
    answers_payload_read_problems,
    query_levels,
)

#: The shape the first version of ``conversation_turn`` shipped: the extraction
#: sits in the subquery that orders and limits (1739 buffers on 60 answers of
#: ~195 kB against 77 for the shape above).
EXTRACTION_UNDER_THE_SORT = (
    "SELECT c.id, "
    "(SELECT a.payload->'linked_report' FROM answers a "
    "  WHERE a.conversation_id = c.id "
    "  ORDER BY a.ordinal + 0 DESC LIMIT 1) AS linked_report "
    "FROM conversations c WHERE c.id = %s"
)


@pytest.mark.parametrize(
    "sql",
    [LIST_CONVERSATIONS_SQL, CONVERSATION_TURN_HEAD_SQL],
    ids=["list_conversations", "conversation_turn"],
)
def test_the_statement_reads_one_payload_by_ordinal(sql):
    assert answers_payload_read_problems(sql) == []
    # Not vacuous: the statement does extract the receipt, from the joined row.
    assert LAST_ANSWER_LINKED_REPORT_SQL in sql and LAST_ANSWER_JOIN_SQL in sql


def test_the_two_statements_share_one_definition_of_the_last_answer():
    for sql in (LIST_CONVERSATIONS_SQL, CONVERSATION_TURN_HEAD_SQL):
        assert sql.count("ORDER BY a.ordinal + 0 DESC LIMIT 1") == 1
        assert sql.count(LAST_ANSWER_JOIN_SQL) == 1


@pytest.mark.parametrize(
    "sql, expected",
    [
        (EXTRACTION_UNDER_THE_SORT, 2),
        # Another operator on the same payload, in the limited level.
        (
            "SELECT (SELECT a.payload->>'x' FROM answers a "
            "WHERE a.conversation_id = c.id ORDER BY a.ordinal DESC LIMIT 1) "
            "FROM conversations c",
            2,
        ),
        (
            "SELECT (SELECT a.payload #> '{linked_report}' FROM answers a "
            "WHERE a.conversation_id = c.id ORDER BY a.ordinal DESC LIMIT 1) "
            "FROM conversations c",
            2,
        ),
        # Located with a LIMIT, then extracted in the SAME limited level.
        (
            "SELECT last.payload->'x' FROM conversations c "
            "LEFT JOIN LATERAL (SELECT a.ordinal, a.payload->'x' AS v FROM answers a "
            "WHERE a.conversation_id = c.id ORDER BY a.ordinal DESC LIMIT 1) newest ON true "
            "LEFT JOIN answers last ON last.ordinal = newest.ordinal",
            2,
        ),
        # The right shape without the equality join: extraction from every row.
        (
            "SELECT a.payload->'x' FROM conversations c "
            "JOIN answers a ON a.conversation_id = c.id",
            1,
        ),
    ],
    ids=["original", "arrow-text", "path-operator", "lateral-extracts", "no-ordinal-join"],
)
def test_the_guard_rejects_an_extraction_that_runs_per_candidate_row(sql, expected):
    assert len(answers_payload_read_problems(sql)) == expected


def test_the_guard_accepts_the_located_row_and_ignores_other_reads():
    assert answers_payload_read_problems(
        "SELECT last.payload->'x' FROM conversations c "
        "LEFT JOIN LATERAL (SELECT a.ordinal FROM answers a "
        "WHERE a.conversation_id = c.id ORDER BY a.ordinal + 0 DESC LIMIT 1) newest ON true "
        "LEFT JOIN answers last ON last.ordinal = newest.ordinal WHERE c.id = %s"
    ) == []
    # The one-answer read by position takes the whole column, extracts nothing.
    assert answers_payload_read_problems(
        "SELECT id, question, payload, created_at FROM answers "
        "WHERE conversation_id = %s ORDER BY created_at, ordinal LIMIT 1 OFFSET %s"
    ) == []
    assert answers_payload_read_problems("SELECT 1") == []


def test_query_levels_find_each_subselect_and_skip_quotes():
    levels = query_levels(
        "SELECT f(x), (SELECT 1 FROM t LIMIT 1), '(SELECT bogus)' AS s "
        "FROM u JOIN LATERAL (SELECT y FROM v WHERE w = ')') n ON true"
    )
    assert len(levels) == 3
    assert "f(x)" in levels[0] and "<subquery>" in levels[0]
    assert "(SELECT bogus)" in levels[0], "a quoted parenthesis is text, not a level"
    assert "LIMIT" not in levels[0] and "LIMIT 1" in levels[1]
    assert "WHERE w = ')'" in levels[2]
    with pytest.raises(ValueError):
        query_levels("SELECT (SELECT 1")
