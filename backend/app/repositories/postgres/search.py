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


@dataclass(frozen=True)
class MemoryCandidateScope:
    """Reviewed predicates that must be applied before Memory's LIMIT."""

    owner_id: str
    viewer_id: str
    notebook_id: str | None = None
    statuses: tuple[str, ...] = ()
    origin: str | None = None


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


def memory_candidate_ids(
    connection,
    query: str,
    limit: int,
    *,
    scope: MemoryCandidateScope,
) -> list[str]:
    """Return bounded mixed-language Memory candidates using all v6 indexes."""
    if limit <= 0 or not query.strip():
        return []
    predicates, params = _memory_match_predicates(query, scope)
    params.extend(
        [
            query,
            query,
            query,
            max(12, int(limit)),
        ]
    )
    rows = connection.execute(
        "SELECT id FROM memory_items WHERE "
        f"{' AND '.join(predicates)} "
        "ORDER BY GREATEST(public.similarity(title,%s),"
        "public.similarity(content_md,%s),"
        f"public.similarity({TAGS_JSON_EXPRESSION},%s)) DESC,"
        "id COLLATE \"C\" LIMIT %s",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def memory_match_count(
    connection,
    query: str,
    *,
    scope: MemoryCandidateScope,
) -> int:
    """Count every scoped lexical match without materializing candidate rows."""
    if not query.strip():
        return 0
    predicates, params = _memory_match_predicates(query, scope)
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM memory_items WHERE "
        f"{' AND '.join(predicates)}",
        params,
    ).fetchone()
    return int(row["n"])


def memory_page_candidate_ids(
    connection,
    query: str,
    limit: int,
    offset: int,
    *,
    scope: MemoryCandidateScope,
) -> list[str]:
    """Return one bounded public-list page using the exact scoped matcher."""
    if limit <= 0 or not query.strip():
        return []
    predicates, params = _memory_match_predicates(query, scope)
    params.extend(
        [
            query,
            query,
            query,
            max(1, min(200, int(limit))),
            max(0, int(offset)),
        ]
    )
    rows = connection.execute(
        "SELECT id FROM memory_items WHERE "
        f"{' AND '.join(predicates)} "
        "ORDER BY GREATEST(public.similarity(title,%s),"
        "public.similarity(content_md,%s),"
        f"public.similarity({TAGS_JSON_EXPRESSION},%s)) DESC,"
        "id COLLATE \"C\" LIMIT %s OFFSET %s",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _memory_match_predicates(
    query: str,
    scope: MemoryCandidateScope,
) -> tuple[list[str], list[object]]:
    """Build the one reviewed scope+match predicate shared by page and count."""
    pattern = f"%{query}%"
    predicates = ["created_by=%s"]
    params: list[object] = [scope.owner_id]
    if scope.notebook_id is not None:
        predicates.append("notebook_id=%s")
        params.append(scope.notebook_id)
    if scope.statuses:
        predicates.append("status=ANY(%s)")
        params.append(list(scope.statuses))
    if scope.origin is not None:
        predicates.append("origin=%s")
        params.append(scope.origin)
    predicates.append(
        "EXISTS (SELECT 1 FROM notebooks access_nb "
        "WHERE access_nb.id=memory_items.notebook_id AND "
        "(access_nb.created_by=%s OR EXISTS (SELECT 1 FROM notebook_members access_nm "
        "WHERE access_nm.notebook_id=access_nb.id AND access_nm.user_id=%s)))"
    )
    params.extend([scope.viewer_id, scope.viewer_id])
    params.extend(
        [
            query,
            pattern,
            query,
            pattern,
            query,
            pattern,
        ]
    )
    predicates.append(
        "("
        "title OPERATOR(public.%%) %s OR title ILIKE %s OR "
        "content_md OPERATOR(public.%%) %s OR content_md ILIKE %s OR "
        f"{TAGS_JSON_EXPRESSION} OPERATOR(public.%%) %s OR "
        f"{TAGS_JSON_EXPRESSION} ILIKE %s)"
    )
    return predicates, params


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
