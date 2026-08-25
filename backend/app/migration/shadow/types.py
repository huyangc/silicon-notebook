from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TableClass(StrEnum):
    REPLICATED = "replicated"
    # Durable only for the lifetime of one backend-local worker.  These tables
    # exist in both paired schemas and remain part of schema totality, but a
    # shadow migration must neither snapshot nor capture/apply their rows.
    LOCAL_EPHEMERAL = "local-ephemeral"
    REBUILT = "rebuilt"
    SHARED_FILESYSTEM = "shared-filesystem"
    SHADOW_INTERNAL = "shadow-internal"


class ReplicationKeyKind(StrEnum):
    DECLARED_PK = "declared_pk"
    SHADOW_UNIQUE = "shadow_unique"


@dataclass(frozen=True)
class TableSpec:
    name: str
    table_class: TableClass
    replication_key: tuple[str, ...]
    key_kind: ReplicationKeyKind
    copy_rank: int
    blob_columns: tuple[str, ...] = ()
    path_columns: tuple[str, ...] = ()
    sqlite_null_guard_columns: tuple[str, ...] = ()
    sqlite_to_postgres: str = "identity"
    postgres_to_sqlite: str = "identity"


@dataclass(frozen=True)
class ReplicationGuardSpec:
    name: str
    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class SchemaPair:
    sqlite_version: int
    postgres_version: int
    epoch: int


@dataclass(frozen=True)
class SchemaValidationReport:
    schema_version: int
    ordinary_tables: frozenset[str]
    rebuilt_roots: frozenset[str] = frozenset()
    sqlite_null_guard_columns: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Manifest:
    schema_pair: SchemaPair
    tables: tuple[TableSpec, ...]

    @property
    def replicated(self) -> tuple[TableSpec, ...]:
        return tuple(
            spec for spec in self.tables if spec.table_class is TableClass.REPLICATED
        )

    @property
    def replicated_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.replicated)

    @property
    def rebuilt_names(self) -> tuple[str, ...]:
        return tuple(
            spec.name for spec in self.tables if spec.table_class is TableClass.REBUILT
        )

    @property
    def local_ephemeral_names(self) -> tuple[str, ...]:
        return tuple(
            spec.name
            for spec in self.tables
            if spec.table_class is TableClass.LOCAL_EPHEMERAL
        )

    @property
    def application_names(self) -> tuple[str, ...]:
        """Physical application tables, including non-replicated local stages."""
        return (*self.replicated_names, *self.local_ephemeral_names)
