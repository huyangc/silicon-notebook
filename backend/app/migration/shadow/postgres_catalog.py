"""Migration-derived PostgreSQL business-catalog validation.

The validated generation is whatever ``POSTGRES_SCHEMA_MANIFEST`` declares; this
module deliberately spells no schema version of its own.  See ``_SCHEMA_LABEL``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.migration.shadow.manifest import replication_guard_specs
from app.migration.shadow.types import Manifest
from app.repositories.postgres.migrator import load_migrations
from app.repositories.postgres.schema_manifest import (
    POSTGRES_ROWID_ORDINAL_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
)


# Operator wording is derived, never hardcoded.  Every schema bump would
# otherwise have to hand-edit ~30 literals across this module and replicator.py,
# and historically that failed silently: the strings sat at "v11" straight
# through the v12 and v13 bumps, and the v14 bump only fixed them by rewriting
# all of them by hand again.  ``validate_manifest`` pins ``MANIFEST.schema_pair``
# to both ``RUNNING_SCHEMA_PAIR`` and ``POSTGRES_SCHEMA_MANIFEST``, so this one
# constant is provably the generation these validators actually check.
_SCHEMA_LABEL = f"PostgreSQL schema v{POSTGRES_SCHEMA_MANIFEST.postgres_version}"

_MIGRATIONS = load_migrations(
    Path(__file__).parents[2] / "repositories" / "postgres" / "migrations"
)
_IDENT = r"[a-z][a-z0-9_]*"


def _balanced(value: str, start: int) -> tuple[str, int]:
    depth = 0
    quoted = False
    index = start
    while index < len(value):
        char = value[index]
        if char == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return value[start + 1 : index], index + 1
        index += 1
    raise ValueError("packaged PostgreSQL migration has unbalanced SQL")


def _split_top_level(value: str) -> tuple[str, ...]:
    result: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for index, char in enumerate(value):
        if char == "'":
            quoted = not quoted
        elif not quoted:
            depth += char == "("
            depth -= char == ")"
            if char == "," and depth == 0:
                result.append(value[start:index].strip())
                start = index + 1
    result.append(value[start:].strip())
    return tuple(item for item in result if item)


def _names(value: str) -> tuple[str, ...]:
    return tuple(item.strip().strip('"').lower() for item in _split_top_level(value))


_TYPE_CAST = (
    r"::\s*(?:\"?[a-z_][a-z0-9_]*\"?\.)?\"?[a-z_][a-z0-9_]*\"?"
    r"(?:\s+(?:with|without)\s+time\s+zone)?(?:\[\])?"
)
_LITERAL_CAST = re.compile(
    rf"(?P<literal>'(?:''|[^'])*'|(?<![a-z0-9_])[-+]?\d+(?:\.\d+)?)"
    rf"\s*{_TYPE_CAST}",
    re.I,
)
_ARRAY_LITERAL_ITEM = r"(?:'(?:''|[^'])*'|[-+]?\d+(?:\.\d+)?|NULL)"
_ARRAY_LITERAL_CAST = re.compile(
    rf"(?P<array>ARRAY\s*\[\s*{_ARRAY_LITERAL_ITEM}"
    rf"(?:\s*,\s*{_ARRAY_LITERAL_ITEM})*\s*\])\s*{_TYPE_CAST}",
    re.I,
)
_MEMBERSHIP = re.compile(
    r"(?P<left>\"?[a-z_][a-z0-9_]*\"?)\s*"
    r"(?P<operator>=\s*ANY|<>\s*ALL|!=\s*ALL)\s*"
    r"\(\s*ARRAY\s*\[(?P<items>[^]]*)\]\s*\)",
    re.I | re.S,
)


def _canonical_expression(
    value: str | None,
    *,
    context: str,
) -> tuple[str, ...]:
    """Canonicalize migration SQL and PostgreSQL deparse without losing logic.

    PostgreSQL adds casts and rewrites ``IN``/``NOT IN`` to ``ANY``/``ALL``.
    Those presentation changes are normalized, while boolean/comparison/null
    operators remain part of the contract.  ``context`` is explicit so future
    deparse compatibility cannot silently broaden every SQL surface.
    """
    if value is None:
        return ()
    if context not in {"check", "default", "index_key", "predicate"}:
        raise ValueError("unknown PostgreSQL expression canonicalization context")
    # PG deparse annotates literals (including ARRAY members) with inferred
    # types.  Normalize only those decorations.  A cast on a column or an
    # expression is executable schema semantics and must remain in the token
    # contract (e.g. dimension::smallint or revoked_at::date).
    normalized = _LITERAL_CAST.sub(lambda match: match.group("literal"), value)
    normalized = _ARRAY_LITERAL_CAST.sub(lambda match: match.group("array"), normalized)
    if context in {"check", "predicate"}:
        normalized = _MEMBERSHIP.sub(
            lambda match: (
                f"{match.group('left')} "
                + (
                    "IN"
                    if match.group("operator").upper().replace(" ", "") == "=ANY"
                    else "NOT IN"
                )
                + f" ({match.group('items')})"
            ),
            normalized,
        )
    tokens = re.findall(
        r"'(?:''|[^'])*'|::|\|\||<>|!=|<=|>=|=|<|>|"
        r'"[^"]+"|[a-z_][a-z0-9_]*|[-+]?[0-9]+(?:\.[0-9]+)?',
        normalized,
        re.I,
    )
    result: list[str] = []
    for token in tokens:
        if token.startswith(("'", '"')):
            canonical = token
        else:
            canonical = token.lower()
        if context == "default" and re.fullmatch(r"'[-+]?\d+(?:\.\d+)?'", token):
            canonical = canonical[1:-1]
        if canonical == "!=":
            canonical = "<>"
        elif canonical == "check" and context == "check":
            continue
        elif canonical == "array" and context in {"check", "predicate"}:
            continue
        result.append(canonical)
    return tuple(result)


@dataclass(frozen=True)
class ConstraintContract:
    table: str
    kind: str
    columns: tuple[str, ...]
    referenced_table: str | None = None
    referenced_columns: tuple[str, ...] = ()
    update_action: str = "a"
    delete_action: str = "a"
    check_tokens: tuple[str, ...] = ()
    deferrable: bool = False
    initially_deferred: bool = False
    match_type: str = ""


@dataclass(frozen=True)
class IndexContract:
    table: str
    unique: bool
    method: str
    keys: tuple[tuple[str, ...], ...]
    predicate_tokens: tuple[str, ...]
    nulls_not_distinct: bool = False


@dataclass(frozen=True)
class ColumnContract:
    data_type: str
    nullable: bool
    is_identity: str
    collation: str | None
    default_tokens: tuple[str, ...]
    identity_generation: str | None = None
    identity_start: str | None = None
    identity_increment: str | None = None
    identity_minimum: str | None = None
    identity_maximum: str | None = None
    identity_cycle: str | None = None
    domain_schema: str | None = None
    domain_name: str | None = None
    generated: str = "NEVER"
    generation_expression: str | None = None
    collation_schema: str | None = None


_ACTION = {
    None: "a",
    "NO ACTION": "a",
    "RESTRICT": "r",
    "CASCADE": "c",
    "SET NULL": "n",
    "SET DEFAULT": "d",
}


def _constraint(name: str, table: str, clause: str) -> ConstraintContract:
    upper = " ".join(clause.upper().split())
    kind = (
        "p"
        if upper.startswith("PRIMARY KEY")
        else "u"
        if upper.startswith("UNIQUE")
        else "c"
        if upper.startswith("CHECK")
        else "f"
    )
    first = clause.find("(")
    body, end = _balanced(clause, first)
    columns = () if kind == "c" else _names(body)
    deferrable = (
        re.search(r"\bNOT\s+DEFERRABLE\b", clause, re.I) is None
        and re.search(r"\bDEFERRABLE\b", clause, re.I) is not None
    )
    initially_deferred = (
        re.search(r"\bINITIALLY\s+DEFERRED\b", clause, re.I) is not None
    )
    if kind != "f":
        return ConstraintContract(
            table=table,
            kind=kind,
            columns=columns,
            check_tokens=(
                _canonical_expression(body, context="check") if kind == "c" else ()
            ),
            deferrable=deferrable,
            initially_deferred=initially_deferred,
        )
    reference = re.search(rf"\bREFERENCES\s+({_IDENT})\s*\(", clause[end:], re.I)
    if reference is None:
        raise ValueError(f"packaged foreign key is invalid: {name}")
    ref_start = end + reference.end() - 1
    ref_body, _ = _balanced(clause, ref_start)
    update = re.search(
        r"\bON\s+UPDATE\s+(NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT)",
        clause,
        re.I,
    )
    delete = re.search(
        r"\bON\s+DELETE\s+(NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT)",
        clause,
        re.I,
    )
    match = re.search(r"\bMATCH\s+(FULL|PARTIAL|SIMPLE)\b", clause, re.I)
    return ConstraintContract(
        table,
        kind,
        columns,
        reference.group(1).lower(),
        _names(ref_body),
        _ACTION[None if update is None else update.group(1).upper()],
        _ACTION[None if delete is None else delete.group(1).upper()],
        (),
        deferrable,
        initially_deferred,
        {None: "s", "FULL": "f", "PARTIAL": "p", "SIMPLE": "s"}[
            None if match is None else match.group(1).upper()
        ],
    )


def _expected_constraints() -> dict[str, ConstraintContract]:
    result: dict[str, ConstraintContract] = {}
    for migration in _MIGRATIONS:
        text = migration.sql
        for create in re.finditer(rf"\bCREATE\s+TABLE\s+({_IDENT})\s*\(", text, re.I):
            body, _ = _balanced(text, create.end() - 1)
            for item in _split_top_level(body):
                match = re.match(
                    rf"\s*CONSTRAINT\s+({_IDENT})\s+(.+)$", item, re.I | re.S
                )
                if match:
                    contract = _constraint(
                        match.group(1), create.group(1), match.group(2)
                    )
                    previous = result.setdefault(match.group(1), contract)
                    if previous != contract:
                        raise ValueError(
                            "packaged PostgreSQL constraint contract is ambiguous"
                        )
        for alter in re.finditer(
            rf"\bALTER\s+TABLE\s+({_IDENT})\s+ADD\s+CONSTRAINT\s+({_IDENT})\s+([^;]+);",
            text,
            re.I | re.S,
        ):
            contract = _constraint(alter.group(2), alter.group(1), alter.group(3))
            previous = result.setdefault(alter.group(2), contract)
            if previous != contract:
                raise ValueError("packaged PostgreSQL constraint contract is ambiguous")
    return result


def _column_contract(definition: str) -> ColumnContract:
    type_match = re.match(
        r"\s*(timestamp\s+with\s+time\s+zone|timestamptz|double\s+precision|"
        r"character\s+varying|bigint|integer|smallint|text|jsonb|bytea|boolean|numeric|real)\b",
        definition,
        re.I,
    )
    if type_match is None:
        raise ValueError("packaged PostgreSQL column type is unsupported")
    data_type = " ".join(type_match.group(1).lower().split())
    if data_type == "timestamptz":
        data_type = "timestamp with time zone"
    default = re.search(
        r"(?<!BY )\bDEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|COLLATE|GENERATED\s+BY)|$)",
        definition,
        re.I | re.S,
    )
    identity = bool(
        re.search(
            r"\bGENERATED\s+BY\s+DEFAULT\s+AS\s+IDENTITY\b",
            definition,
            re.I,
        )
    )
    return ColumnContract(
        data_type=data_type,
        nullable=(
            not identity and re.search(r"\bNOT\s+NULL\b", definition, re.I) is None
        ),
        is_identity="YES" if identity else "NO",
        collation=("C" if re.search(r'\bCOLLATE\s+"C"', definition, re.I) else None),
        default_tokens=_canonical_expression(
            None if default is None else default.group(1), context="default"
        ),
        identity_generation="BY DEFAULT" if identity else None,
        identity_start="1" if identity else None,
        identity_increment="1" if identity else None,
        identity_minimum="1" if identity else None,
        identity_maximum="9223372036854775807" if identity else None,
        identity_cycle="NO",
        collation_schema=(
            "pg_catalog" if re.search(r'\bCOLLATE\s+"C"', definition, re.I) else None
        ),
    )


def _expected_columns() -> dict[str, dict[str, ColumnContract]]:
    result: dict[str, dict[str, ColumnContract]] = {}
    for migration in _MIGRATIONS:
        text = migration.sql
        for create in re.finditer(rf"\bCREATE\s+TABLE\s+({_IDENT})\s*\(", text, re.I):
            body, _ = _balanced(text, create.end() - 1)
            table = create.group(1)
            columns = result.setdefault(table, {})
            for item in _split_top_level(body):
                match = re.match(rf"\s*({_IDENT})\s+(.+)$", item, re.I | re.S)
                if match is None or match.group(1).lower() == "constraint":
                    continue
                columns[match.group(1)] = _column_contract(match.group(2))
        for alter in re.finditer(
            rf"\bALTER\s+TABLE\s+({_IDENT})\s+ADD\s+COLUMN\s+"
            rf"(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})\s+([^;]+);",
            text,
            re.I | re.S,
        ):
            result.setdefault(alter.group(1), {})[alter.group(2)] = _column_contract(
                alter.group(3)
            )
    return result


def _expected_indexes(
    columns: dict[str, dict[str, ColumnContract]],
) -> dict[str, IndexContract]:
    result: dict[str, IndexContract] = {}
    pattern = re.compile(
        rf"\bCREATE\s+(UNIQUE\s+)?INDEX\s+({_IDENT})\s+ON\s+({_IDENT})"
        rf"(?:\s+USING\s+({_IDENT}))?\s*\(",
        re.I | re.S,
    )
    for migration in _MIGRATIONS:
        for match in pattern.finditer(migration.sql):
            body, end = _balanced(migration.sql, match.end() - 1)
            tail = migration.sql[end : migration.sql.find(";", end)]
            predicate = re.search(r"\bWHERE\s+(.+)$", tail, re.I | re.S)
            keys: list[tuple[str, ...]] = []
            for item in _split_top_level(body):
                tokens = _canonical_expression(item, context="index_key")
                if (
                    len(tokens) >= 2
                    and tokens[-2] == "public"
                    and tokens[-1].endswith("_ops")
                ):
                    # Opclass schema/OID semantics are validated separately
                    # from pg_opclass. Normalize only this parsed trailing
                    # migration opclass qualifier, never arbitrary expressions.
                    tokens = tokens[:-2] + (tokens[-1],)
                if "collate" not in tokens and any(
                    token in columns.get(match.group(3), {})
                    and columns[match.group(3)][token].collation == "C"
                    for token in tokens
                ):
                    if tokens[-1].endswith("_ops"):
                        tokens = tokens[:-1] + ("collate", '"C"', tokens[-1])
                    else:
                        tokens += ("collate", '"C"')
                keys.append(tokens)
            result[match.group(2)] = IndexContract(
                match.group(3),
                match.group(1) is not None,
                (match.group(4) or "btree").lower(),
                tuple(keys),
                _canonical_expression(
                    None if predicate is None else predicate.group(1),
                    context="predicate",
                ),
            )
    return result


EXPECTED_CONSTRAINTS = _expected_constraints()
EXPECTED_COLUMNS = _expected_columns()
EXPECTED_OPERATIONAL_INDEXES = _expected_indexes(EXPECTED_COLUMNS)
EXPECTED_LEDGER_COLUMNS = {
    "version": _column_contract("integer NOT NULL"),
    "checksum": _column_contract("text NOT NULL"),
    "applied_at": _column_contract("timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP"),
}
EXPECTED_LEDGER_CONSTRAINT = ConstraintContract(
    "silicon_schema_migrations", "p", ("version",)
)


def _plain_index_keys(
    table: str, columns: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    keys: list[tuple[str, ...]] = []
    for column in columns:
        tokens = (column,)
        if EXPECTED_COLUMNS[table][column].collation == "C":
            tokens += ("collate", '"C"')
        keys.append(tokens)
    return tuple(keys)


def _expected_all_indexes(manifest: Manifest) -> dict[str, IndexContract]:
    result = dict(EXPECTED_OPERATIONAL_INDEXES)
    for name, constraint in EXPECTED_CONSTRAINTS.items():
        if constraint.kind not in {"p", "u"}:
            continue
        result[name] = IndexContract(
            constraint.table,
            True,
            "btree",
            _plain_index_keys(constraint.table, constraint.columns),
            (),
        )
    for guard in replication_guard_specs(manifest):
        result[guard.name] = IndexContract(
            guard.table,
            True,
            "btree",
            _plain_index_keys(guard.table, guard.columns),
            (),
        )
    result["silicon_schema_migrations_pkey"] = IndexContract(
        "silicon_schema_migrations",
        True,
        "btree",
        (("version",),),
        (),
    )
    return result


def _value(row: Any, name: str, position: int) -> Any:
    return row[name] if hasattr(row, "keys") else row[position]


def _table_semantics_are_exact(row: Any) -> bool:
    return (
        _value(row, "relkind", 1) == "r"
        and _value(row, "relpersistence", 2) == "p"
        and _value(row, "owner", 3) == _value(row, "expected_owner", 4)
        and _value(row, "relacl", 5) is None
        and not bool(_value(row, "relrowsecurity", 6))
        and not bool(_value(row, "relforcerowsecurity", 7))
        and not bool(_value(row, "relispartition", 8))
    )


def _index_flags_are_live(row: Any) -> bool:
    return all(
        bool(_value(row, name, position))
        for name, position in (("indisvalid", 4), ("indisready", 5), ("indislive", 6))
    )


def _index_key_tokens(
    conn: Any,
    *,
    index_oid: Any,
    position: int,
    option: int,
    opclass_oid: int,
    collation_oid: int,
) -> tuple[str, ...]:
    row = conn.execute(
        "SELECT pg_get_indexdef(%s,%s,true) AS key",
        (index_oid, position),
    ).fetchone()
    tokens = list(_canonical_expression(_value(row, "key", 0), context="index_key"))
    descending = bool(option & 1)
    nulls_first = bool(option & 2)
    if descending:
        tokens.append("desc")
    # PostgreSQL's implicit order is ASC NULLS LAST / DESC NULLS FIRST. Only
    # non-default NULL ordering must be added to the migration-derived tokens.
    if nulls_first != descending:
        tokens.extend(("nulls", "first" if nulls_first else "last"))
    if collation_oid:
        collation = conn.execute(
            "SELECT c.collname,n.nspname AS namespace FROM pg_collation c "
            "JOIN pg_namespace n ON n.oid=c.collnamespace WHERE c.oid=%s",
            (collation_oid,),
        ).fetchone()
        if collation is None or _value(collation, "namespace", 1) != "pg_catalog":
            raise ValueError(f"{_SCHEMA_LABEL} index collation schema drifted")
        name = str(_value(collation, "collname", 0)).replace('"', '""')
        tokens.extend(("collate", f'"{name}"'))
    opclass = conn.execute(
        "SELECT o.opcname,o.opcdefault,n.nspname AS namespace "
        "FROM pg_opclass o JOIN pg_namespace n ON n.oid=o.opcnamespace WHERE o.oid=%s",
        (opclass_oid,),
    ).fetchone()
    if opclass is None:
        raise ValueError(f"{_SCHEMA_LABEL} index opclass is missing")
    if not bool(_value(opclass, "opcdefault", 1)):
        if _value(opclass, "namespace", 2) != "public":
            raise ValueError(f"{_SCHEMA_LABEL} non-default index opclass schema drifted")
        tokens.append(str(_value(opclass, "opcname", 0)))
    return tuple(tokens)


def _validate_fk_constraint_triggers(
    conn: Any,
    *,
    business_schema: str,
    business_tables: tuple[str, ...],
) -> None:
    rows = conn.execute(
        "SELECT g.oid AS trigger_oid,g.tgenabled,g.tgisinternal,"
        "c.oid AS constraint_oid,c.conname,ct.relname AS constraint_table,"
        "cn.nspname AS constraint_schema,rt.relname AS referenced_table,"
        "rn.nspname AS referenced_schema,ot.relname AS trigger_table "
        "FROM pg_trigger g "
        "JOIN pg_class ot ON ot.oid=g.tgrelid "
        "JOIN pg_namespace own ON own.oid=ot.relnamespace "
        "LEFT JOIN pg_constraint c ON c.oid=g.tgconstraint "
        "LEFT JOIN pg_class ct ON ct.oid=c.conrelid "
        "LEFT JOIN pg_namespace cn ON cn.oid=ct.relnamespace "
        "LEFT JOIN pg_class rt ON rt.oid=c.confrelid "
        "LEFT JOIN pg_namespace rn ON rn.oid=rt.relnamespace "
        "WHERE own.nspname=%s AND ot.relname=ANY(%s) AND g.tgisinternal "
        "ORDER BY c.conname,g.oid",
        (business_schema, list(business_tables)),
    ).fetchall()
    expected = {
        name: contract
        for name, contract in EXPECTED_CONSTRAINTS.items()
        if contract.kind == "f"
    }
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        name = _value(row, "conname", 4)
        if not isinstance(name, str) or name not in expected:
            raise ValueError(f"{_SCHEMA_LABEL} internal trigger contract drifted")
        grouped.setdefault(name, []).append(row)
    if set(grouped) != set(expected):
        raise ValueError(f"{_SCHEMA_LABEL} foreign-key trigger set drifted")
    for name, contract in expected.items():
        trigger_rows = grouped[name]
        constraint_oids = {_value(row, "constraint_oid", 3) for row in trigger_rows}
        actual_owners = Counter(
            str(_value(row, "trigger_table", 9)) for row in trigger_rows
        )
        expected_owners = Counter((contract.table, contract.table))
        if contract.referenced_table is None:
            raise ValueError(f"{_SCHEMA_LABEL} foreign-key contract is incomplete")
        expected_owners.update((contract.referenced_table, contract.referenced_table))
        if (
            len(trigger_rows) != 4
            or len(constraint_oids) != 1
            or actual_owners != expected_owners
            or any(
                not bool(_value(row, "tgisinternal", 2))
                or _value(row, "tgenabled", 1) != "O"
                or _value(row, "constraint_table", 5) != contract.table
                or _value(row, "constraint_schema", 6) != business_schema
                or _value(row, "referenced_table", 7) != contract.referenced_table
                or _value(row, "referenced_schema", 8) != business_schema
                for row in trigger_rows
            )
        ):
            raise ValueError(f"{_SCHEMA_LABEL} foreign-key trigger contract drifted")


def _validate_identity_sequences(
    conn: Any,
    *,
    business_schema: str,
    business_tables: tuple[str, ...],
) -> None:
    rows = conn.execute(
        "SELECT t.relname AS table_name,a.attname AS column_name,"
        "sn.nspname AS sequence_schema,s.relname AS sequence_name,"
        "pg_get_userbyid(s.relowner) AS owner,current_user AS expected_owner,"
        "s.relpersistence,s.relacl,format_type(q.seqtypid,NULL) AS data_type,"
        "q.seqstart,q.seqincrement,q.seqmin,q.seqmax,q.seqcache,q.seqcycle,"
        "d.deptype FROM pg_class t "
        "JOIN pg_namespace tn ON tn.oid=t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum>0 "
        "JOIN pg_depend d ON d.refclassid='pg_class'::regclass "
        "AND d.refobjid=t.oid AND d.refobjsubid=a.attnum "
        "AND d.classid='pg_class'::regclass AND d.deptype IN ('a','i') "
        "JOIN pg_class s ON s.oid=d.objid AND s.relkind='S' "
        "JOIN pg_namespace sn ON sn.oid=s.relnamespace "
        "JOIN pg_sequence q ON q.seqrelid=s.oid "
        "WHERE tn.nspname=%s AND t.relname=ANY(%s) ORDER BY t.relname",
        (business_schema, list(business_tables)),
    ).fetchall()
    expected_tables = set(POSTGRES_ROWID_ORDINAL_TABLES)
    actual_tables = {str(_value(row, "table_name", 0)) for row in rows}
    if len(rows) != len(expected_tables) or actual_tables != expected_tables:
        raise ValueError(f"{_SCHEMA_LABEL} identity sequence set drifted")
    for row in rows:
        if (
            _value(row, "column_name", 1) != "ordinal"
            or _value(row, "sequence_schema", 2) != business_schema
            or _value(row, "owner", 4) != _value(row, "expected_owner", 5)
            or _value(row, "relpersistence", 6) != "p"
            or _value(row, "relacl", 7) is not None
            or _value(row, "data_type", 8) != "bigint"
            or int(_value(row, "seqstart", 9)) != 1
            or int(_value(row, "seqincrement", 10)) != 1
            or int(_value(row, "seqmin", 11)) != 1
            or int(_value(row, "seqmax", 12)) != 9_223_372_036_854_775_807
            or int(_value(row, "seqcache", 13)) != 1
            or bool(_value(row, "seqcycle", 14))
            or _value(row, "deptype", 15) != "i"
        ):
            raise ValueError(f"{_SCHEMA_LABEL} identity sequence contract drifted")


def validate_postgres_business_catalog(
    conn: Any,
    *,
    business_schema: str,
    manifest: Manifest,
    guard_names: frozenset[str],
) -> None:
    """Reject missing, extra, invalid, or same-name semantic catalog drift.

    The generation checked is ``manifest.schema_pair``, which ``validate_manifest``
    pins to ``RUNNING_SCHEMA_PAIR``; the name carries no schema version so it
    cannot go stale the way ``..._v11_catalog`` did across three bumps.
    """
    catalog_tables = ("silicon_schema_migrations", *manifest.application_names)
    table_rows = conn.execute(
        "SELECT c.relname AS table_name,c.relkind,c.relpersistence,"
        "pg_get_userbyid(c.relowner) AS owner,current_user AS expected_owner,c.relacl,"
        "c.relrowsecurity,c.relforcerowsecurity,c.relispartition "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname",
        (business_schema,),
    ).fetchall()
    actual_tables = {str(_value(row, "table_name", 0)) for row in table_rows}
    if actual_tables != set(catalog_tables):
        raise ValueError(f"{_SCHEMA_LABEL} business table catalog drifted")
    for row in table_rows:
        if not _table_semantics_are_exact(row):
            raise ValueError(f"{_SCHEMA_LABEL} business table semantics drifted")

    inherited = conn.execute(
        "SELECT 1 FROM pg_inherits i "
        "JOIN pg_class child ON child.oid=i.inhrelid "
        "JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace "
        "JOIN pg_class parent ON parent.oid=i.inhparent "
        "JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace "
        "WHERE (child_ns.nspname=%s AND child.relname=ANY(%s)) "
        "OR (parent_ns.nspname=%s AND parent.relname=ANY(%s)) LIMIT 1",
        (
            business_schema,
            list(catalog_tables),
            business_schema,
            list(catalog_tables),
        ),
    ).fetchone()
    if inherited is not None:
        raise ValueError(f"{_SCHEMA_LABEL} business table inheritance drifted")

    unexpected_trigger = conn.execute(
        "SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=%s AND t.relname=ANY(%s) AND NOT g.tgisinternal LIMIT 1",
        (business_schema, list(catalog_tables)),
    ).fetchone()
    unexpected_rule = conn.execute(
        "SELECT 1 FROM pg_rewrite r JOIN pg_class t ON t.oid=r.ev_class "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=%s AND t.relname=ANY(%s) LIMIT 1",
        (business_schema, list(catalog_tables)),
    ).fetchone()
    unexpected_policy = conn.execute(
        "SELECT 1 FROM pg_policy p JOIN pg_class t ON t.oid=p.polrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=%s AND t.relname=ANY(%s) LIMIT 1",
        (business_schema, list(catalog_tables)),
    ).fetchone()
    if any(
        item is not None
        for item in (unexpected_trigger, unexpected_rule, unexpected_policy)
    ):
        raise ValueError(f"{_SCHEMA_LABEL} business table behavior drifted")

    column_rows = conn.execute(
        "SELECT table_name,column_name,data_type,is_nullable,is_identity,"
        "identity_generation,identity_start,identity_increment,identity_minimum,"
        "identity_maximum,identity_cycle,collation_name,column_default,domain_schema,"
        "domain_name,is_generated,generation_expression,collation_schema "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=ANY(%s) "
        "ORDER BY table_name,ordinal_position",
        (business_schema, list(catalog_tables)),
    ).fetchall()
    actual_columns: dict[str, dict[str, ColumnContract]] = {}
    for row in column_rows:
        actual_columns.setdefault(str(_value(row, "table_name", 0)), {})[
            str(_value(row, "column_name", 1))
        ] = ColumnContract(
            data_type=str(_value(row, "data_type", 2)),
            nullable=str(_value(row, "is_nullable", 3)) == "YES",
            is_identity=str(_value(row, "is_identity", 4)),
            collation=_value(row, "collation_name", 11),
            default_tokens=_canonical_expression(
                _value(row, "column_default", 12), context="default"
            ),
            identity_generation=_value(row, "identity_generation", 5),
            identity_start=_value(row, "identity_start", 6),
            identity_increment=_value(row, "identity_increment", 7),
            identity_minimum=_value(row, "identity_minimum", 8),
            identity_maximum=_value(row, "identity_maximum", 9),
            identity_cycle=_value(row, "identity_cycle", 10),
            domain_schema=_value(row, "domain_schema", 13),
            domain_name=_value(row, "domain_name", 14),
            generated=str(_value(row, "is_generated", 15)),
            generation_expression=_value(row, "generation_expression", 16),
            collation_schema=_value(row, "collation_schema", 17),
        )
    expected_columns = dict(EXPECTED_COLUMNS)
    expected_columns["silicon_schema_migrations"] = EXPECTED_LEDGER_COLUMNS
    if actual_columns != expected_columns:
        raise ValueError(f"{_SCHEMA_LABEL} column catalog drifted")

    _validate_identity_sequences(
        conn,
        business_schema=business_schema,
        business_tables=manifest.replicated_names,
    )

    rows = conn.execute(
        "SELECT c.conname,c.contype,c.convalidated,t.relname AS table_name,"
        "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ord) "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum ORDER BY k.ord) AS columns,"
        "rt.relname AS referenced_table,ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(attnum,ord) "
        "JOIN pg_attribute a ON a.attrelid=rt.oid AND a.attnum=k.attnum ORDER BY k.ord) AS referenced_columns,"
        "c.confupdtype,c.confdeltype,c.condeferrable,c.condeferred,c.confmatchtype,"
        "rn.nspname AS referenced_schema,pg_get_constraintdef(c.oid,true) AS definition "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace "
        "LEFT JOIN pg_class rt ON rt.oid=c.confrelid LEFT JOIN pg_namespace rn ON rn.oid=rt.relnamespace "
        "WHERE n.nspname=%s AND t.relname=ANY(%s) ORDER BY c.conname",
        (business_schema, list(catalog_tables)),
    ).fetchall()
    actual_names = {str(_value(row, "conname", 0)) for row in rows}
    expected_constraints = dict(EXPECTED_CONSTRAINTS)
    expected_constraints["silicon_schema_migrations_pkey"] = EXPECTED_LEDGER_CONSTRAINT
    if actual_names != set(expected_constraints):
        raise ValueError(f"{_SCHEMA_LABEL} constraint catalog drifted")
    for row in rows:
        name = str(_value(row, "conname", 0))
        expected = expected_constraints[name]
        is_foreign = expected.kind == "f"
        actual = ConstraintContract(
            str(_value(row, "table_name", 3)),
            str(_value(row, "contype", 1)),
            () if expected.kind == "c" else tuple(_value(row, "columns", 4) or ()),
            _value(row, "referenced_table", 5) if is_foreign else None,
            tuple(_value(row, "referenced_columns", 6) or ()) if is_foreign else (),
            str(_value(row, "confupdtype", 7)) if is_foreign else "a",
            str(_value(row, "confdeltype", 8)) if is_foreign else "a",
            (
                _canonical_expression(_value(row, "definition", 13), context="check")
                if expected.kind == "c"
                else ()
            ),
            bool(_value(row, "condeferrable", 9)),
            bool(_value(row, "condeferred", 10)),
            str(_value(row, "confmatchtype", 11)) if is_foreign else "",
        )
        referenced_schema = _value(row, "referenced_schema", 12)
        if (
            not bool(_value(row, "convalidated", 2))
            or (is_foreign and referenced_schema != business_schema)
            or actual != expected
        ):
            raise ValueError(f"{_SCHEMA_LABEL} constraint definition drifted")

    _validate_fk_constraint_triggers(
        conn,
        business_schema=business_schema,
        business_tables=catalog_tables,
    )

    rows = conn.execute(
        "SELECT x.oid,x.relname AS index_name,t.relname AS table_name,i.indisunique,i.indisvalid,"
        "i.indisready,i.indislive,am.amname,"
        "i.indnkeyatts,i.indnatts,pg_get_expr(i.indpred,i.indrelid,true) AS predicate,"
        "i.indoption,i.indclass::oid[] AS opclass_oids,"
        "i.indcollation::oid[] AS collation_oids,i.indnullsnotdistinct "
        "FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace JOIN pg_index i ON i.indexrelid=x.oid "
        "JOIN pg_class t ON t.oid=i.indrelid JOIN pg_am am ON am.oid=x.relam "
        "WHERE n.nspname=%s AND t.relname=ANY(%s) ORDER BY x.relname",
        (business_schema, list(catalog_tables)),
    ).fetchall()
    expected_indexes = _expected_all_indexes(manifest)
    expected_guard_names = {
        definition.name for definition in replication_guard_specs(manifest)
    }
    if guard_names != expected_guard_names:
        raise ValueError(f"{_SCHEMA_LABEL} shadow guard contract drifted")
    actual_names = {str(_value(row, "index_name", 1)) for row in rows}
    if actual_names != set(expected_indexes):
        raise ValueError(f"{_SCHEMA_LABEL} index catalog drifted")
    for row in rows:
        name = str(_value(row, "index_name", 1))
        if not _index_flags_are_live(row):
            raise ValueError(f"{_SCHEMA_LABEL} index is not valid, ready, and live")
        expected = expected_indexes[name]
        key_count = int(_value(row, "indnkeyatts", 8))
        raw_options = _value(row, "indoption", 11)
        options = (
            tuple(int(item) for item in str(raw_options).split())
            if isinstance(raw_options, str)
            else tuple(raw_options or ())
        )
        opclass_oids = tuple(_value(row, "opclass_oids", 12) or ())
        collation_oids = tuple(_value(row, "collation_oids", 13) or ())
        keys = tuple(
            _index_key_tokens(
                conn,
                index_oid=_value(row, "oid", 0),
                position=position,
                option=int(options[position - 1]),
                opclass_oid=int(opclass_oids[position - 1]),
                collation_oid=int(collation_oids[position - 1]),
            )
            for position in range(1, key_count + 1)
        )
        actual = IndexContract(
            str(_value(row, "table_name", 2)),
            bool(_value(row, "indisunique", 3)),
            str(_value(row, "amname", 7)),
            keys,
            _canonical_expression(_value(row, "predicate", 10), context="predicate"),
            bool(_value(row, "indnullsnotdistinct", 14)),
        )
        if int(_value(row, "indnatts", 9)) != key_count or actual != expected:
            raise ValueError(f"{_SCHEMA_LABEL} index definition drifted")

    extension = conn.execute(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'"
    ).fetchone()
    if extension is None or _value(extension, "nspname", 0) != "public":
        raise ValueError(f"{_SCHEMA_LABEL} pg_trgm extension contract drifted")
