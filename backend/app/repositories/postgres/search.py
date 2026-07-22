"""PostgreSQL lexical-candidate SQL, centralized to match expression indexes."""
from __future__ import annotations

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
_SEARCH_TABLES = {
    "knowledge_objects": frozenset({"id"}),
    "memory_items": frozenset({"id"}),
    "chunks": frozenset({"id"}),
    "source_elements": frozenset({"id"}),
    "sources": frozenset({"id"}),
}


def lexical_candidate_sql(
    *, table: str, id_column: str, text_expression: str
) -> str:
    """Return bounded trigram candidate SQL for a strict identifier whitelist.

    The similarity value is ordering-only adapter state.  Callers expose their
    backend-neutral relevance scores after deterministic application ranking.
    """
    if table not in _SEARCH_TABLES or id_column not in _SEARCH_TABLES[table]:
        raise ValueError("unsupported PostgreSQL lexical search identifier")
    if text_expression not in _SEARCH_EXPRESSIONS.values():
        raise ValueError("unsupported PostgreSQL lexical search expression")
    table_sql = sql.Identifier(table).as_string()
    id_sql = sql.Identifier(id_column).as_string()
    return (
        f"SELECT {id_sql} AS candidate_id, "
        f"public.similarity({text_expression}, %s) AS candidate_similarity "
        f"FROM {table_sql} "
        f"WHERE ({text_expression} OPERATOR(public.%%) %s OR "
        f"{text_expression} ILIKE %s) "
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
    notebook_column: str,
    text_expression: str,
    notebook_id: str,
    query: str,
    limit: int,
):
    if notebook_column != "notebook_id":
        raise ValueError("unsupported PostgreSQL lexical notebook identifier")
    if limit <= 0:
        return []
    base = lexical_candidate_sql(
        table=table, id_column=id_column, text_expression=text_expression
    )
    statement = base.replace(
        f"FROM \"{table}\" WHERE ",
        f"FROM \"{table}\" WHERE \"notebook_id\"=%s AND ",
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
        notebook_column="notebook_id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        query=query,
        limit=limit,
    )


def chunk_candidate_rows(connection, notebook_id: str, query: str, limit: int):
    return _candidate_rows(
        connection,
        table="chunks",
        id_column="id",
        notebook_column="notebook_id",
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
