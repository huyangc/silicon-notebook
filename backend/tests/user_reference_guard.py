"""Backend-neutral half of the user-reference registry guard.

Each backend lane reads its migrated catalog into (table, column) sets and
calls ``registry_violations``; an empty list means every column that could
hold a ``users.id`` is classified in ``app.repositories.user_references``.
"""
from __future__ import annotations

import re

from app.repositories.user_references import NOT_USER_REFERENCES, USER_REFERENCE_COLUMNS

# Column names that look like they store a user id.
USER_REFERENCE_NAME = re.compile(r"(^|_)(user|users|owner|actor|author|principal)(_id|_ids)?$|_by$")


def registry_violations(columns: set[tuple[str, str]], user_foreign_keys: set[tuple[str, str]]) -> list[str]:
    registered = {(ref.table, ref.column): ref for ref in USER_REFERENCE_COLUMNS}
    candidates = {
        (table, column) for table, column in columns
        if table != "users" and USER_REFERENCE_NAME.search(column)
    } | user_foreign_keys
    problems = [
        f"{table}.{column} is not classified in USER_REFERENCE_COLUMNS or NOT_USER_REFERENCES"
        for table, column in sorted(candidates)
        if (table, column) not in registered and (table, column) not in NOT_USER_REFERENCES
    ]
    problems += [
        f"{table}.{column} is registered but does not exist"
        for table, column in sorted(set(registered) | set(NOT_USER_REFERENCES))
        if (table, column) not in columns
    ]
    # A kept audit id dangles after the delete, which a foreign key forbids.
    problems += [
        f"{table}.{column} has a foreign key to users but is audit_kept"
        for (table, column), ref in sorted(registered.items())
        if ref.kind == "audit_kept" and (table, column) in user_foreign_keys
    ]
    problems += [
        f"{ref.table}.{ref.column} is registered twice"
        for index, ref in enumerate(USER_REFERENCE_COLUMNS)
        if (ref.table, ref.column, ref.extra_where) in {
            (other.table, other.column, other.extra_where) for other in USER_REFERENCE_COLUMNS[:index]
        }
    ]
    return problems
