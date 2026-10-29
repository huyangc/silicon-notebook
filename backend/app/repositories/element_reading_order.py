"""The reading order of one source's elements -- one definition, both backends.

A parsed element is minted ``el-<source_id>-NNNN`` (``source_ingestion``: the
index from 1, zero-padded to four digits) and an image the offline backfill
adds after its anchor is ``el-<source_id>-NNNN-gMMM``. Every reader used to take
the byte order of those ids as the document order, which holds only while every
index has the same number of digits: from the 10 000th element on,
``...-10000`` sorts between ``...-1000`` and ``...-1001`` and ``...-11340``
between ``...-1134`` and ``...-1135``.

The fix keeps the byte order as the last key and puts the NUMBER OF DIGITS of
the index in front of it (``INDEX_DIGITS_SQL``): a shorter index is a smaller
one, and among indexes of one width byte order is numeric order. Within one
source every id this repository mints has the same prefix, so the pair
``(digits, id)`` is the numeric order, with a backfilled image still right
after its anchor (``...-0012`` < ``...-0012-g001`` < ``...-0013``).

What it changes, and what it leaves alone, by construction:

* A source of at most 9 999 parsed elements: every id has four digits, the key
  is a constant 4, the order is byte for byte the old one.
* An id NOT of the form ``el-<own source_id>-<digits>...`` (knowhow cells
  ``el-kh-<hash>``, promotion evidence ``el-promo-<hash>``, a copied
  notebook's ``el-<uuid>``, smoke seeds): the key is 0, the order is the old
  byte order.
* Only a source past 9 999 elements reads differently -- in its true order.

The key is computed from ``id`` and ``source_id`` alone, so it needs no column,
no backfill and no write path to get anything right: rows written before this
change read in the right order the moment the readers use it. ``substr``,
``length``, ``ltrim`` and ``||`` mean the same on SQLite and PostgreSQL, so the
SAME text is the index expression on both backends (SQLite
``_migration_105``, PostgreSQL ``0085``) and the term every reader orders by;
both planners match an ``ORDER BY`` term against an index expression
structurally, so a reader that qualifies the columns (``e.id``) still uses the
index. ``index_digits`` is the Python twin a keyset cursor uses to rebuild the
key of its last row.

Readers that ordered by ``created_at, id`` now order by ``created_at,
<digits>, id`` (``reading_order_sql``); readers that ordered by ``id`` alone
(chunking, the heading pass, the catalog preview, the image backfill plan) now
order by ``<digits>, id`` (``id_order_for``) -- each keeps its own leading key,
so nothing but the past-9 999 case moves. Keyset continuations run as up to
three index ranges (``after_cursor_sqls``), and an id-ordered reader keeps its
old statement whenever a two-row probe shows the digits cannot reorder it, so
neither pays for the expression on the sources it does not change.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

#: Every element id this repository mints for a parsed source starts with
#: ``el-<source_id>-`` (``source_ingestion``; ``image_backfill`` appends to
#: one of those).
ELEMENT_ID_PREFIX = "el-"
_DIGITS = "0123456789"


def index_digits_sql(alias: str = "") -> str:
    """Number of leading digits after ``el-<source_id>-`` in ``id`` (0 when
    the id does not start with that prefix), over the ``id``/``source_id``
    columns of ``alias`` (or unqualified)."""
    q = f"{alias}." if alias else ""
    rest = f"substr({q}id,length({q}source_id)+5)"
    return (
        f"(CASE WHEN substr({q}id,1,length({q}source_id)+4)="
        f"('{ELEMENT_ID_PREFIX}'||{q}source_id||'-') "
        f"THEN length({rest})-length(ltrim({rest},'{_DIGITS}')) ELSE 0 END)"
    )


#: The unqualified expression -- the text both backends' index DDL carries.
INDEX_DIGITS_SQL = index_digits_sql()


def reading_order_sql(alias: str = "", *, id_collate: str = "") -> str:
    """``created_at, <digits>, id`` over ``alias`` -- the ORDER BY (and row
    comparison) of every reader that pages a source in reading order.
    ``id_collate`` appends e.g. ``COLLATE "C"`` to the id term (PostgreSQL's
    column is already "C"; its readers spell it out)."""
    q = f"{alias}." if alias else ""
    suffix = f" {id_collate}" if id_collate else ""
    return f"{q}created_at,{index_digits_sql(alias)},{q}id{suffix}"


def id_order_sql(alias: str = "", *, id_collate: str = "") -> str:
    """``<digits>, id`` -- for the readers that order by id alone."""
    q = f"{alias}." if alias else ""
    suffix = f" {id_collate}" if id_collate else ""
    return f"{index_digits_sql(alias)},{q}id{suffix}"


def index_digits(source_id: str, element_id: str) -> int:
    """Python twin of ``index_digits_sql`` (pinned equal by the tests)."""
    prefix = f"{ELEMENT_ID_PREFIX}{source_id}-"
    element_id = str(element_id or "")
    if not element_id.startswith(prefix):
        return 0
    rest = element_id[len(prefix):]
    return len(rest) - len(rest.lstrip(_DIGITS))


def reading_order_key(source_id: str, created_at: object, element_id: str) -> tuple:
    """``(created_at, digits, id)`` for one row -- a keyset cursor's bound."""
    return (created_at, index_digits(source_id, element_id), element_id)


# ------------------------------------------------------------ keyset pages
def after_cursor_sqls(
    columns: Sequence[str], scope: str, *, placeholder: str = "?",
    id_collate: str = "",
) -> "tuple[str, str]":
    """One keyset page in reading order AFTER a cursor row, as the three
    disjoint index ranges past ``(created_at, digits, id)``, in key order:
    same created_at and digits, later id; same created_at, more digits; later
    created_at. Returns ``(head, tail)``: the first range, and the other two
    as one statement; both backends run them through ``page_after``.

    Why not one row comparison ``(created_at, <digits>, id) > (...)``:
    SQLite will not range over an index expression inside a row value (it
    seeks on ``created_at`` alone and filters the rest, so a whole-source walk
    turns quadratic), and on PostgreSQL an ``ORDER BY`` naming the digits
    expression makes every returned row evaluate it (measured about +18% on a
    whole-source walk). The first range orders by ``id`` alone -- the index
    order once ``created_at`` and the digits are fixed by equality -- so the
    common page (one parse instant, one digit width) costs what the old
    ``(created_at, id)`` page cost; the tail runs only for a page the first
    range leaves short (a walk's last page, or the 9 999 -> 10 000 crossing),
    and its own two ranges are index seeks that are usually empty.
    ``columns`` may carry ``expr AS name``; ``scope`` is the conditions the page
    is confined to (``source_id`` first). Placeholders: the head takes the
    scope's own, then ``created_at, digits, id, limit``; the tail takes the
    scope's, ``created_at, digits, limit``, the scope's again, ``created_at,
    limit``, then ``limit``.
    """
    p = placeholder
    suffix = f" {id_collate}" if id_collate else ""
    select = ", ".join(columns)
    names = ", ".join(column.rsplit(" AS ", 1)[-1].strip() for column in columns)

    def ranged(condition: str, order: str, alias: str) -> str:
        return (
            f"SELECT * FROM (SELECT {select}, {INDEX_DIGITS_SQL} AS reading_digits "
            f"FROM source_elements WHERE {scope} AND {condition} "
            f"ORDER BY {order} LIMIT {p}) AS {alias}"
        )

    head = (
        f"SELECT {select} FROM source_elements WHERE {scope} AND "
        f"created_at = {p} AND {INDEX_DIGITS_SQL} = {p} AND id{suffix} > {p} "
        f"ORDER BY id{suffix} LIMIT {p}"
    )
    tail = (
        f"SELECT {names} FROM ("
        + ranged(f"created_at = {p} AND {INDEX_DIGITS_SQL} > {p}",
                 f"{INDEX_DIGITS_SQL}, id{suffix}", "more_digits")
        + " UNION ALL "
        + ranged(f"created_at > {p}", reading_order_sql(id_collate=id_collate),
                 "later_instant")
        + f") AS tail ORDER BY created_at, reading_digits, id{suffix} LIMIT {p}"
    )
    return head, tail


def page_after(
    execute: Callable[[str, tuple], list],
    statements: "tuple[str, str]",
    scope_params: Sequence[object],
    key: Sequence[object],
    limit: int,
) -> list:
    """Run ``after_cursor_sqls``' head, and its tail only when the head left
    the page short (``execute(sql, params) -> rows``).

    The two are separate statements, as each page of a walk already was: an
    element swap committing between them yields exactly what it yields between
    two pages -- the new generation's rows past the cursor key -- so a walk's
    consistency contract is unchanged."""
    head, tail = statements
    created_at, digits, element_id = key
    scope = tuple(scope_params)
    rows = list(execute(head, (*scope, created_at, digits, element_id, limit)))
    if len(rows) < limit:
        need = limit - len(rows)
        rows.extend(execute(tail, (*scope, created_at, digits, need,
                                   *scope, created_at, need, need)))
    return rows


# ------------------------------------------------- readers ordered by id
def id_order_probe_sql(*, placeholder: str = "?", id_collate: str = "") -> str:
    """The first and the last element of a source in reading order, as
    ``(created_at, digits)`` -- two index descents on
    ``idx_source_elements_reading_order``; bind the source id twice.
    ``id_order_for`` reads them."""
    p = placeholder
    suffix = f" {id_collate}" if id_collate else ""
    probe = f"SELECT created_at, {INDEX_DIGITS_SQL} AS digits FROM source_elements WHERE source_id = {p} "
    return (
        f"SELECT * FROM ({probe}ORDER BY {reading_order_sql(id_collate=id_collate)} LIMIT 1) AS first_row "
        "UNION ALL "
        f"SELECT * FROM ({probe}ORDER BY created_at DESC, {INDEX_DIGITS_SQL} DESC, "
        f"id{suffix} DESC LIMIT 1) AS last_row"
    )


def id_order_for(probe_rows: Sequence[Any], *, id_collate: str = "") -> str:
    """The ORDER BY a reader that orders by ``<digits>, id`` should send, from
    ``id_order_probe_sql``'s two rows.

    The first and last element in reading order agree on ``created_at`` and
    the digits only when every element of the source does (the index is
    ordered by exactly those two first), and then ``<digits>, id`` IS the byte
    order of ``id``: the reader sends its old ``ORDER BY id`` -- same text,
    same plan, same cost (every parsed source of at most 9 999 elements,
    backfilled images included). When only ``created_at`` agrees, ``<digits>,
    id`` is the index order, read as such. Otherwise (several write instants,
    e.g. a knowhow source) the expression is sorted on.

    The probe and the reader are two statements, not one snapshot (SQLite
    reads on the shared thread-local connection without a manual ``BEGIN``;
    PostgreSQL runs READ COMMITTED). An element swap committing between them
    can only matter if it changes the answer, and a swap always writes a new
    ``created_at`` generation -- which every caller already checks before it
    writes anything derived from the read: chunk building compare-and-sets
    ``sources.chunked_at`` (nulled by the swap), the navigation pass stamps
    against that same ``chunked_at``, the image backfill re-reads ``COUNT``
    and ``MAX(created_at)`` in its write transaction, and a catalog apply
    re-checks ``source_element_generation``. So a stale probe can at worst
    order a read that is then thrown away; a read transaction (or REPEATABLE
    READ) around the pair would buy nothing the callers do not already
    enforce."""
    suffix = f" {id_collate}" if id_collate else ""
    rows = [(row["created_at"], row["digits"]) for row in probe_rows]
    if len(rows) < 2 or rows[0] == rows[1]:
        return f"id{suffix}"
    if rows[0][0] == rows[1][0]:
        return reading_order_sql(id_collate=id_collate)
    return id_order_sql(id_collate=id_collate)
