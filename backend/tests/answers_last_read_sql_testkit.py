"""Structural reading of the PostgreSQL statements that take a value out of the
conversation's LAST ``answers.payload``.

``answers.payload`` is a large jsonb. ``SELECT a.payload->'linked_report' FROM
answers a WHERE ... ORDER BY a.ordinal + 0 DESC LIMIT 1`` looks like one cheap
read, but the ``->`` is evaluated for every answer of the conversation underneath
the top-N sort -- each payload detoasted -- to keep one of them (60 answers of
~195 kB: 1739 buffers against 77). The shape that costs one payload locates the
last answer by ``ordinal`` alone (a level that may be ordered and limited because
it reads no payload) and extracts from the row an equality join on that
``ordinal`` returns. ``LIST_CONVERSATIONS_SQL`` already was that shape;
``CONVERSATION_TURN_HEAD_SQL`` must stay it.

A plan pin (EXPLAIN buffers or timings) is not used for this: such pins flip with
the visibility map and the planner's statistics. The cost is decided by where the
text puts the extraction, so the guard reads the text -- with a parenthesis-depth
scan that finds each sub-SELECT as its own query level (quotes skipped), not a
line or offset match. No SQL parser is a dependency of the project; this covers
the operators the statements use (``->``, ``->>``, ``#>``, ``#>>``).
"""
from __future__ import annotations

import re

_EXTRACTION = re.compile(r"\b(\w+)\.payload\s*(?:->>?|#>>?)")
_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)


def query_levels(sql: str) -> list[str]:
    """Every query level of ``sql``: the statement itself and each parenthesised
    sub-SELECT (a derived table, a lateral, a scalar subquery), each with its
    inner sub-SELECTs cut out (left as ``<subquery>``). A parenthesis that does
    not open a SELECT (a function call, an expression group) stays in the text of
    the level it is in. Quoted strings and quoted identifiers are skipped."""
    levels: list[str] = []
    stack: list[list[str] | None] = [[]]  # None = a plain parenthesis
    owner: list[list[str]] = [stack[0]]  # the buffer text goes to
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char in "'\"":
            end = index + 1
            while end < length:
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            owner[-1].append(sql[index:end + 1])
            index = end + 1
            continue
        if char == "(":
            if re.match(r"\(\s*SELECT\b", sql[index:], re.IGNORECASE):
                buffer: list[str] = []
                stack.append(buffer)
                owner.append(buffer)
            else:
                stack.append(None)
                owner[-1].append(char)
            index += 1
            continue
        if char == ")":
            if not stack or len(stack) == 1:
                raise ValueError("unbalanced parenthesis in SQL")
            closed = stack.pop()
            if closed is None:
                owner[-1].append(char)
            else:
                owner.pop()
                levels.append("".join(closed))
                owner[-1].append(" <subquery> ")
            index += 1
            continue
        owner[-1].append(char)
        index += 1
    if len(stack) != 1:
        raise ValueError("unbalanced parenthesis in SQL")
    return ["".join(stack[0]), *levels]


def answers_payload_read_problems(sql: str) -> list[str]:
    """What is wrong with how ``sql`` extracts from ``answers.payload``; empty
    when it extracts nothing, or only through the safe shape:

    * no query level that has a ``LIMIT`` extracts from a payload (the extraction
      would run for every candidate row under the sort);
    * every alias a payload is extracted from is an ``answers`` row joined by
      equality on ``ordinal`` to the row a limited sub-SELECT located
      (``JOIN answers last ON last.ordinal = newest.ordinal``)."""
    problems: list[str] = []
    for level in query_levels(sql):
        aliases = sorted(set(_EXTRACTION.findall(level)))
        if not aliases:
            continue
        if _LIMIT.search(level):
            problems.append(
                "a payload is extracted in a query level that also has a LIMIT: "
                + " ".join(level.split())
            )
        for alias in aliases:
            joined = re.search(
                rf"\bJOIN\s+answers\s+{alias}\s+ON\s+{alias}\.ordinal\s*=\s*\w+\.ordinal\b",
                level, re.IGNORECASE,
            )
            if joined is None:
                problems.append(
                    f"{alias}.payload is not read from a row joined by equality "
                    "on ordinal: " + " ".join(level.split())
                )
    return problems
