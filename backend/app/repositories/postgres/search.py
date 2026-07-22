"""PostgreSQL lexical-candidate SQL, centralized to match expression indexes."""
from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql


PAYLOAD_NAME_EXPRESSION = '(payload ->> \'name\') COLLATE "C"'
TAGS_JSON_EXPRESSION = '(tags_json::text) COLLATE "C"'

_SEARCH_EXPRESSIONS = {
    "knowledge_payload_name": PAYLOAD_NAME_EXPRESSION,
    "memory_tags": TAGS_JSON_EXPRESSION,
    "chunk_text": "text",
    "source_element_text": "text",
    "source_title": "title",
    "source_summary": "summary",
}
@dataclass(frozen=True)
class _SearchTarget:
    table: str
    id_column: str
    text_expression: str
    notebook_column: str
    live_predicate: str = ""


_SEARCH_TARGETS = {
    (target.table, target.id_column, target.text_expression): target
    for target in (
        _SearchTarget(
            "knowledge_objects",
            "id",
            PAYLOAD_NAME_EXPRESSION,
            "notebook_id",
            "status!='deprecated'",
        ),
        _SearchTarget("memory_items", "id", TAGS_JSON_EXPRESSION, "notebook_id"),
        _SearchTarget("chunks", "id", "text", "notebook_id"),
        _SearchTarget("source_elements", "id", "text", "notebook_id"),
        _SearchTarget("sources", "id", "title", "notebook_id"),
        _SearchTarget("sources", "id", "summary", "notebook_id"),
    )
}


def lexical_candidate_sql(
    *,
    table: str,
    id_column: str,
    text_expression: str,
    scoped: bool = False,
    live_only: bool = False,
) -> str:
    """Return bounded trigram SQL for one reviewed table/expression pairing.

    The similarity value is ordering-only adapter state.  Callers expose their
    backend-neutral relevance scores after deterministic application ranking.
    Scope and lifecycle predicates are assembled structurally before the match
    expression; no caller rewrites SQL text or supplies a free-form predicate.
    """
    try:
        target = _SEARCH_TARGETS[(table, id_column, text_expression)]
    except KeyError:
        raise ValueError(
            "unsupported PostgreSQL lexical search table/expression pairing"
        ) from None
    if live_only and not target.live_predicate:
        raise ValueError("PostgreSQL lexical target has no lifecycle predicate")
    table_sql = sql.Identifier(target.table).as_string()
    id_sql = sql.Identifier(target.id_column).as_string()
    predicates = []
    if scoped:
        predicates.append(f"{sql.Identifier(target.notebook_column).as_string()}=%s")
    if live_only:
        predicates.append(target.live_predicate)
    predicates.append(
        f"({target.text_expression} OPERATOR(public.%%) %s OR "
        f"{target.text_expression} ILIKE %s)"
    )
    return (
        f"SELECT {id_sql} AS candidate_id, "
        f"public.similarity({target.text_expression}, %s) AS candidate_similarity "
        f"FROM {table_sql} "
        f"WHERE {' AND '.join(predicates)} "
        f"ORDER BY candidate_similarity DESC, {id_sql} COLLATE \"C\" "
        "LIMIT %s"
    )


def expression(name: str) -> str:
    try:
        return _SEARCH_EXPRESSIONS[name]
    except KeyError:
        raise ValueError("unsupported PostgreSQL lexical search expression") from None


def _candidate_rows(
    connection,
    *,
    table: str,
    id_column: str,
    text_expression: str,
    notebook_id: str,
    query: str,
    limit: int,
    live_only: bool = False,
):
    if limit <= 0:
        return []
    statement = lexical_candidate_sql(
        table=table,
        id_column=id_column,
        text_expression=text_expression,
        scoped=True,
        live_only=live_only,
    )
    return connection.execute(
        statement,
        (query, notebook_id, query, f"%{query}%", max(int(limit), 12)),
    ).fetchall()


def knowledge_candidate_rows(connection, notebook_id: str, query: str, limit: int):
    return _candidate_rows(
        connection,
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        query=query,
        limit=limit,
        live_only=True,
    )


def chunk_candidate_rows(connection, notebook_id: str, query: str, limit: int):
    return _candidate_rows(
        connection,
        table="chunks",
        id_column="id",
        text_expression=expression("chunk_text"),
        notebook_id=notebook_id,
        query=query,
        limit=limit,
    )


def knowledge_candidate_documents(connection, ids):
    values = list(ids)
    if not values:
        return []
    return connection.execute(
        f"SELECT id,{PAYLOAD_NAME_EXPRESSION} AS name "
        "FROM knowledge_objects WHERE id=ANY(%s) AND status!='deprecated'",
        (values,),
    ).fetchall()


def chunk_candidate_documents(connection, ids):
    values = list(ids)
    if not values:
        return []
    return connection.execute(
        "SELECT id,text FROM chunks WHERE id=ANY(%s)",
        (values,),
    ).fetchall()


def notebook_source_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT * FROM sources WHERE notebook_id=%s "
        "AND source_type NOT IN ('memory','knowhow') AND "
        "(title ILIKE %s OR summary ILIKE %s OR file_name ILIKE %s) "
        "ORDER BY created_at,id COLLATE \"C\" LIMIT %s",
        (notebook_id, pattern, pattern, pattern, limit),
    ).fetchall()


def notebook_element_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT se.*,s.title AS source_title FROM source_elements se "
        "JOIN sources s ON s.id=se.source_id "
        "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow') "
        "AND (se.text ILIKE %s OR se.location_label ILIKE %s OR s.title ILIKE %s) "
        "ORDER BY se.ordinal LIMIT %s",
        (notebook_id, pattern, pattern, pattern, limit),
    ).fetchall()


def notebook_knowledge_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT id,object_type,payload FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' AND "
        f"({PAYLOAD_NAME_EXPRESSION} ILIKE %s OR "
        "(payload::text) COLLATE \"C\" ILIKE %s) "
        "ORDER BY ordinal LIMIT %s",
        (notebook_id, pattern, pattern, limit),
    ).fetchall()


def mention_claim_rows(connection, notebook_id: str):
    return connection.execute(
        f"SELECT id,{PAYLOAD_NAME_EXPRESSION} AS nm FROM knowledge_objects "
        "WHERE notebook_id=%s AND object_type='claim' AND status!='deprecated' "
        "ORDER BY ordinal",
        (notebook_id,),
    ).fetchall()


def prepare_mention_scan(connection, rows) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.mention_scan_claims")
    connection.execute(
        "CREATE TEMP TABLE mention_scan_claims("
        "claim_index bigint PRIMARY KEY,text text COLLATE \"C\" NOT NULL)"
    )
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO mention_scan_claims(claim_index,text) VALUES (%s,%s)",
            list(rows),
        )
    connection.execute(
        "CREATE INDEX mention_scan_claims_text_trgm ON mention_scan_claims "
        "USING gin (text public.gin_trgm_ops)"
    )


def mention_scan_matches(connection, alias: str):
    return connection.execute(
        "SELECT claim_index AS rowid FROM mention_scan_claims "
        "WHERE text OPERATOR(public.%%) %s OR text ILIKE %s ORDER BY claim_index",
        (alias, f"%{alias}%"),
    )


def drop_mention_scan(connection) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.mention_scan_claims")


def deterministic_lexical_score(query: str, text: str) -> float:
    """Backend-neutral score used after PostgreSQL candidate generation."""
    needle = query.casefold().strip()
    haystack = text.casefold()
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 2.0 + min(1.0, len(needle) / max(1, len(haystack)))
    grams = {needle[index : index + 2] for index in range(max(1, len(needle) - 1))}
    text_grams = {
        haystack[index : index + 2] for index in range(max(1, len(haystack) - 1))
    }
    return len(grams & text_grams) / max(1, len(grams | text_grams))
