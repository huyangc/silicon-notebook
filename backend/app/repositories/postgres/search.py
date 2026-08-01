"""PostgreSQL lexical-candidate SQL, centralized to match expression indexes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from psycopg import sql

from app.repositories.like_pattern import LIKE_ESCAPE_CHAR, escape_like_pattern


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


LIKE_ESCAPE_CHARACTER = LIKE_ESCAPE_CHAR


def like_contains_pattern(term: str) -> str:
    """Return a literal "contains" ILIKE pattern for one lexical term.

    Recall terms are raw user text -- the whole question is itself a term -- so
    ``%``, ``_`` and ``\\`` would otherwise reach LIKE as metacharacters and
    silently widen the probe: ``set_db`` would also admit ``setXdb`` and
    ``set db``.  Escaping belongs to the LIKE arm alone; the trigram operator
    and ``public.similarity`` keep the unescaped term so ordering is unchanged.
    """
    return f"%{escape_like_pattern(term)}%"


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


def _candidate_rows_for_terms(
    connection,
    *,
    table: str,
    id_column: str,
    text_expression: str,
    notebook_id: str,
    terms: list[str],
    per_term_limit: int,
    live_only: bool = False,
    allowed_source_ids: list[str] | None = None,
):
    """Run one indexed LATERAL probe per term in a single PostgreSQL query."""
    if not terms or per_term_limit <= 0:
        return []
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
    predicates = [
        f"{sql.Identifier(target.notebook_column).as_string()}=%s",
    ]
    if live_only:
        predicates.append(target.live_predicate)
    source_params: list[object] = []
    if allowed_source_ids is not None:
        if not allowed_source_ids:
            return []
        if target.table == "knowledge_objects":
            predicates.append(
                f"EXISTS (SELECT 1 FROM knowledge_object_sources kos "
                f"WHERE kos.notebook_id={table_sql}.notebook_id "
                f"AND kos.object_id={table_sql}.{id_sql} AND kos.source_id=ANY(%s))"
            )
        elif target.table == "chunks":
            predicates.append(f"{table_sql}.source_id=ANY(%s)")
        else:
            raise ValueError("source-scoped lexical search target is unsupported")
        source_params.append(allowed_source_ids)
    # The LIKE pattern is escaped and wrapped in Python so LIKE metacharacters
    # in the term stay literal; the trigram arm keeps the raw term.
    predicates.append(
        f"({target.text_expression} OPERATOR(public.%%) lexical_terms.term OR "
        f"{target.text_expression} ILIKE lexical_terms.like_pattern "
        f"ESCAPE '{LIKE_ESCAPE_CHARACTER}')"
    )
    term_values = ",".join("(%s,%s,%s)" for _ in terms)
    statement = (
        f"WITH lexical_terms(term_rank,term,like_pattern) AS (VALUES {term_values}) "
        "SELECT candidate.candidate_id,lexical_terms.term_rank,"
        "candidate.candidate_similarity FROM lexical_terms "
        "CROSS JOIN LATERAL ("
        f"SELECT {id_sql} AS candidate_id,"
        f"public.similarity({target.text_expression},lexical_terms.term) "
        f"AS candidate_similarity FROM {table_sql} "
        f"WHERE {' AND '.join(predicates)} "
        f"ORDER BY candidate_similarity DESC,{id_sql} COLLATE \"C\" LIMIT %s"
        ") AS candidate ORDER BY lexical_terms.term_rank,"
        "candidate.candidate_similarity DESC,candidate.candidate_id COLLATE \"C\""
    )
    params = [
        value
        for term_rank, term in enumerate(terms)
        for value in (term_rank, term, like_contains_pattern(term))
    ]
    params.append(notebook_id)
    params.extend(source_params)
    params.append(int(per_term_limit))
    return connection.execute(statement, params).fetchall()


def knowledge_candidate_rows_for_terms(
    connection, notebook_id: str, terms: list[str], per_term_limit: int,
    allowed_source_ids: list[str] | None = None,
):
    return _candidate_rows_for_terms(
        connection,
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        terms=terms,
        per_term_limit=per_term_limit,
        live_only=True,
        allowed_source_ids=allowed_source_ids,
    )


def chunk_candidate_rows_for_terms(
    connection, notebook_id: str, terms: list[str], per_term_limit: int,
    allowed_source_ids: list[str] | None = None,
):
    return _candidate_rows_for_terms(
        connection,
        table="chunks",
        id_column="id",
        text_expression=expression("chunk_text"),
        notebook_id=notebook_id,
        terms=terms,
        per_term_limit=per_term_limit,
        allowed_source_ids=allowed_source_ids,
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


def chunk_exact_candidate_rows(connection, notebook_id: str, needle: str, limit: int):
    """EXACT substring chunk candidates for the identifier fast path.

    Only `ILIKE '%needle%'` — no `OPERATOR(public.%)` similarity branch, since
    "exact" here means exact; the trigram GIN index (`idx_chunks_text_trgm`)
    accelerates ILIKE patterns just as it does the union path's.

    `%`/`_`/`\\` in the needle are escaped, and that is load-bearing rather
    than defensive: `set_db` contains LIKE's single-character wildcard, so the
    unescaped pattern would also accept `setXdb` — a silent precision loss in
    the one code path whose entire purpose is precision.

    `similarity()` supplies the ordering key (and the reported score) exactly
    as `lexical_candidate_sql` does, with the same `C`-collated id tie-break so
    a truncated window is deterministic.
    """
    if limit <= 0 or not (needle or "").strip():
        return []
    text_expression = expression("chunk_text")
    return connection.execute(
        "SELECT id AS candidate_id,source_id,section_path,"
        f"public.similarity({text_expression},%s) AS candidate_similarity "
        f"FROM chunks WHERE notebook_id=%s AND {text_expression} ILIKE %s "
        "ORDER BY candidate_similarity DESC,id COLLATE \"C\" LIMIT %s",
        (needle, notebook_id, f"%{escape_like_pattern(needle)}%", int(limit)),
    ).fetchall()


def chunk_section_rows(
    connection, notebook_id: str, source_id: str, section_path: str, limit: int
):
    """One section's chunks (that node plus its descendants) in document order.

    `ordinal` is PostgreSQL's document order, mirroring the SQLite adapter's
    `rowid` (chunk ids are random surrogates and sort meaninglessly).  The
    subtree predicate and its escaping match the SQLite adapter verbatim;
    PostgreSQL needs no ESCAPE clause because backslash is already its default
    LIKE escape character and the pattern is a bound parameter.
    """
    path = section_path or ""
    if not path or limit <= 0:
        return []
    return connection.execute(
        "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
        "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
        "WHERE c.notebook_id=%s AND c.source_id=%s "
        "AND (c.section_path=%s OR c.section_path LIKE %s) "
        "ORDER BY c.ordinal LIMIT %s",
        (
            notebook_id,
            source_id,
            path,
            escape_like_pattern(path) + " > %",
            int(limit),
        ),
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
    phrase_queries: Sequence[str] = (),
) -> list[str]:
    """Return bounded mixed-language Memory candidates using all v6 indexes."""
    if limit <= 0 or not query.strip():
        return []
    predicates, params = _memory_match_predicates(query, scope, phrase_queries)
    probes = memory_match_probes(query, phrase_queries)
    for probe in probes:
        params.extend([probe, probe, probe])
    params.append(max(12, int(limit)))
    rows = connection.execute(
        "SELECT id FROM memory_items WHERE "
        f"{' AND '.join(predicates)} "
        f"ORDER BY {_memory_rank_expression(probes)} DESC,"
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


def memory_match_probes(
    query: str, phrase_queries: Sequence[str] = ()
) -> list[str]:
    """The whole query first, then each distinct extra phrase probe.

    One ordering, used by both the match predicate and the ranking expression:
    a probe that can admit a row must also be able to rank it, or the row is
    admitted and then cut by the LIMIT before anything phrase-aware sees it.
    """
    probes: list[str] = [query]
    for phrase in phrase_queries:
        value = str(phrase).strip()
        if value and value not in probes:
            probes.append(value)
    return probes


def _memory_rank_expression(probes: Sequence[str]) -> str:
    """`GREATEST` over every probe × every searched column.

    Ranking on the full query alone discards exactly the rows the independent
    phrase probe exists to admit: a memory carrying the quoted phrase but none
    of the surrounding sentence scores near zero against that sentence, so with
    more than `limit` loose matches it never survives the cut (codex #410
    round-7 P2). Bounded by MAX_QUOTED_PHRASES + 1 probes × 3 columns.
    """
    terms = ",".join(
        "public.similarity(title,%s),public.similarity(content_md,%s),"
        f"public.similarity({TAGS_JSON_EXPRESSION},%s)"
        for _ in probes
    )
    return f"GREATEST({terms})"


def _memory_match_predicates(
    query: str,
    scope: MemoryCandidateScope,
    phrase_queries: Sequence[str] = (),
) -> tuple[list[str], list[object]]:
    """Build the one reviewed scope+match predicate shared by page and count.

    `phrase_queries` are additional INDEPENDENT match probes OR-ed into the same
    statement — one more OR group on an already-indexed predicate, not another
    round trip. They exist because this path probes the whole query as one
    value: a user-quoted phrase would otherwise only match a memory that also
    carries the surrounding sentence (codex #410 round-6 P2).
    """
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
    probes = memory_match_probes(query, phrase_queries)
    groups: list[str] = []
    for index, probe in enumerate(probes):
        # The phrase probes are escaped, the whole-query probe deliberately is
        # not. A phrase promises exactness, and `set_db` / `100% coverage` carry
        # LIKE's own wildcards — unescaped they would admit `setXdb` and crowd
        # real matches out of the bounded pool (codex #410 round-7 P2). The
        # whole-query arm keeps its historical unescaped pattern: widening a
        # candidate probe there is imprecise but harmless, and narrowing it now
        # would silently change Memory recall for every existing query.
        pattern = (
            f"%{query}%" if index == 0
            else f"%{escape_like_pattern(probe)}%"
        )
        params.extend([probe, pattern, probe, pattern, probe, pattern])
        groups.append(
            "("
            "title OPERATOR(public.%%) %s OR title ILIKE %s OR "
            "content_md OPERATOR(public.%%) %s OR content_md ILIKE %s OR "
            f"{TAGS_JSON_EXPRESSION} OPERATOR(public.%%) %s OR "
            f"{TAGS_JSON_EXPRESSION} ILIKE %s)"
        )
    predicates.append("(" + " OR ".join(groups) + ")")
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


def deterministic_lexical_score_terms(terms: list[str], text: str) -> float:
    """Score multiple recall terms while building the document grams once."""
    haystack = text.casefold()
    if not terms or not haystack:
        return 0.0
    text_grams = {
        haystack[index : index + 2] for index in range(max(1, len(haystack) - 1))
    }
    best = 0.0
    for query in terms:
        needle = query.casefold().strip()
        if not needle:
            continue
        if needle in haystack:
            score = 2.0 + min(1.0, len(needle) / max(1, len(haystack)))
        else:
            grams = {
                needle[index : index + 2]
                for index in range(max(1, len(needle) - 1))
            }
            score = len(grams & text_grams) / max(1, len(grams | text_grams))
        best = max(best, score)
    return best
