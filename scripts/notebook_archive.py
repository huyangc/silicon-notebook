#!/usr/bin/env python3
"""Single-file notebook archive CLI: ``export``, ``inspect`` and ``import``.

``scripts/cli.sh notebook export --notebook <id>... (--out <file>|--out-dir <dir>)
[--no-vectors] [--json]`` writes each notebook to one ``.snbk`` archive. It is
read-only against the database and opens it as a schema guest
(``app.services.maintenance_cli.open_cli_repository``), so it may run beside the
serving process; on PostgreSQL each notebook is read inside one REPEATABLE READ
transaction (a long export holds back vacuum's horizon for its duration).

``scripts/cli.sh notebook inspect <file> [--json]`` opens nothing but the file:
it validates the zip directory, the manifest, every checksum and every row
count, and prints a summary.

Exit status: 0 success; 1 the archive is invalid or the export failed; 2 a
usage error or an unknown notebook; 3 the database is not at this build's
schema (the shared schema-guest refusal). Several notebooks export in order and
stop at the first failure -- an unknown notebook, an export error, an I/O error
on the output (``ENOSPC``...), a database failure (a dropped connection, a
query timeout; reported by exception type and SQLSTATE only, never the driver
message, which can carry connection details) or a schema/connection refusal; the archives
already written stay, and ``--json`` prints ``{"exported": [...], "failed":
[...]}`` in every one of those cases.

``scripts/cli.sh notebook import <file>... --owner <username> [--name N]
[--dry-run] [--confirm-service-stopped] [--rebuild-scale auto|skip]
[--reembed auto|skip] [--json]`` imports each archive as a new, independent
notebook owned by ``--owner`` (``app.services.notebook_transfer.import_``). It
writes through the maintenance repository (``open_maintenance_cli_repository``:
schema guest, the database-wide operator lock, and on PostgreSQL the stopped
service confirmed with ``--confirm-service-stopped``), then runs what the import
leaves to its caller, synchronously: the re-embedding legs the report names
(``notebook_reembed``) and, on PostgreSQL, a full scale-index build
(``app.migration.sync.scale_rebuild``; SQLite reports it as skipped).
``--dry-run`` opens the database read-only (no confirmation needed), runs the
whole preflight and the row mapping and writes nothing. Files import in order
and stop at the first failure; the notebooks already imported stay. Exit
status: 0 every file imported (re-embedding or the scale build may still
report a problem; the import itself is done); 1 a file was refused or the
import failed (nothing of that file remains); 2 a usage error, an unknown
owner or a missing confirmation; 3 the schema refusal. ``--json`` prints
``{"imported": [...], "failed": [...]}`` in every case.

The format is ``app.domain.notebook_archive``; docs/operations.md has the
runbook.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/cli.sh notebook",
        description="笔记本单文件导出（.snbk）与检查。导出只读，按 schema 访客打开数据库。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="把笔记本导出为单个 .snbk 文件（只读）")
    export.add_argument(
        "--notebook", action="append", required=True, metavar="ID",
        help="要导出的笔记本 id；可重复给出多本",
    )
    target = export.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", type=Path, help="输出文件（只导出一本时）")
    target.add_argument(
        "--out-dir", type=Path, help="输出目录；每本写成 <笔记本 id>.snbk",
    )
    export.add_argument(
        "--no-vectors", action="store_true",
        help="不带四类向量表（导入端会重新生成）；问题向量照常保留",
    )
    export.add_argument("--json", action="store_true", help="输出 JSON 报告")
    inspect = commands.add_parser("inspect", help="校验 .snbk 文件并打印摘要（不打开数据库）")
    inspect.add_argument("file", type=Path)
    inspect.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    imported = commands.add_parser(
        "import", help="把 .snbk 文件导入为新的独立笔记本（写库；PostgreSQL 需先停服）",
    )
    imported.add_argument("file", type=Path, nargs="+", help="要导入的 .snbk 文件；可给多个")
    imported.add_argument("--owner", required=True, metavar="USERNAME", help="新笔记本的所有者（用户名）")
    imported.add_argument("--name", help="新笔记本的名称（默认沿用导出时的名称；只导入一个文件时可用）")
    imported.add_argument(
        "--dry-run", action="store_true", help="只做预检与映射预演，不写入任何东西",
    )
    imported.add_argument(
        "--confirm-service-stopped", action="store_true",
        help="确认服务已停止（PostgreSQL 上写库必需）",
    )
    imported.add_argument(
        "--rebuild-scale", choices=("auto", "skip"), default="auto",
        help="导入后重建 scale 索引（PostgreSQL；SQLite 记为跳过）",
    )
    imported.add_argument(
        "--reembed", choices=("auto", "skip"), default="auto",
        help="导入后同步补齐需要重新嵌入的向量",
    )
    imported.add_argument("--json", action="store_true", help="输出 JSON 报告")
    return parser


def _targets(options) -> list[tuple[str, Path]]:
    notebooks = list(dict.fromkeys(options.notebook))
    if options.out is not None:
        if len(notebooks) != 1:
            raise ValueError("--out 只能配一本 --notebook；多本请用 --out-dir")
        return [(notebooks[0], options.out)]
    from app.services.notebook_transfer.export import default_archive_name

    return [(nb, options.out_dir / default_archive_name(nb)) for nb in notebooks]


def _export(options) -> int:
    from app.core.config import get_settings
    from app.services.maintenance_cli import MaintenanceCliError, open_cli_repository
    from app.services.notebook_transfer.export import (
        NotebookExportError,
        NotebookNotFoundError,
        embedding_identities,
        export_notebook,
    )

    try:
        targets = _targets(options)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for _notebook, path in targets:
        if path.exists():
            print(f"error: 输出文件已存在: {path}", file=sys.stderr)
            return 2
        if not path.parent.is_dir():
            print(f"error: 输出目录不存在: {path.parent}", file=sys.stderr)
            return 2
    settings = get_settings()
    database_errors = _database_errors()
    reports = []
    failure: "dict | None" = None
    status = 0
    current: "str | None" = None
    try:
        with open_cli_repository(settings) as repository:
            store = repository._runtime.sharing_store  # noqa: SLF001 -- CLI composition root
            embeddings = embedding_identities(settings)
            schemas = repository.list_object_schemas()
            for notebook_id, path in targets:
                current = notebook_id
                try:
                    report = export_notebook(
                        store, notebook_id, path,
                        storage_dir=Path(repository.storage_dir), settings=settings,
                        embeddings=embeddings, global_object_schemas=schemas,
                        include_vectors=not options.no_vectors,
                    )
                except NotebookNotFoundError:
                    failure = {"notebook_id": notebook_id, "error": "笔记本不存在或正在删除"}
                    status = 2
                    break
                except (NotebookExportError, OSError, *database_errors) as exc:
                    # OSError: the output side failed (ENOSPC, EIO...); a
                    # database error: the connection dropped or a read timed
                    # out (codex #974 r1).
                    failure = {"notebook_id": notebook_id, "error": _describe(exc)}
                    status = 1
                    break
                reports.append(report)
                if not options.json:
                    _print_export(report)
    except MaintenanceCliError as exc:
        failure = {"notebook_id": current, "error": str(exc)}
        status = exc.exit_code
    except database_errors as exc:
        # Opening the repository or reading the schema registry failed.
        failure = {"notebook_id": current, "error": _describe(exc)}
        status = 1
    if failure is not None:
        # The archives already written stay; the report says which ones.
        where = f"{failure['notebook_id']}: " if failure["notebook_id"] else ""
        print(f"error: {where}{failure['error']}", file=sys.stderr)
    if options.json:
        print(json.dumps(
            {"exported": [report.to_dict() for report in reports],
             "failed": [] if failure is None else [failure]},
            ensure_ascii=False, indent=2,
        ))
    return status


def _database_errors() -> "tuple[type[BaseException], ...]":
    """The driver exceptions a dropped connection or a timed-out read raises:
    ``sqlite3.Error`` and ``psycopg.Error`` (``psycopg_pool``'s ``PoolTimeout``
    and ``PoolClosed`` subclass it). Neither is an ``OSError``."""
    import sqlite3

    errors: "list[type[BaseException]]" = [sqlite3.Error]
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg ships with the backend
        pass
    else:
        errors.append(psycopg.Error)
    return tuple(errors)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or exc}"
    if isinstance(exc, _database_errors()):
        # Type and SQLSTATE only: a driver message can name the host, port,
        # role or database of the connection.
        sqlstate = getattr(exc, "sqlstate", None)
        suffix = f" (SQLSTATE {sqlstate})" if sqlstate else ""
        return f"数据库错误: {type(exc).__name__}{suffix}"
    return str(exc)


def _print_export(report) -> None:
    rows = sum(report.rows.values())
    print(
        f"[export] {report.notebook_id} -> {report.path} "
        f"({report.archive_bytes} bytes; {rows} rows, {report.files} files"
        f"{'' if report.vectors_included else ', no vectors'})"
    )
    if report.skipped_links:
        print(f"  skipped {report.skipped_links} symbolic links / special files")
    if report.missing_files:
        print(f"  {report.missing_files} files vanished while the export ran")
    for name in report.stale_part_files:
        print(f"  warning: leftover part file from a killed export: {name} "
              "(delete it once no export to this path is running)", file=sys.stderr)
    if report.missing_source_files:
        print(f"  {report.missing_source_files} sources have no file in the notebook tree")
    for key, count in report.normalized.items():
        print(f"  normalized {key}: {count}")


def _inspect(options) -> int:
    from app.services.notebook_transfer.archive_reader import ArchiveError, inspect_archive

    try:
        inspection = inspect_archive(options.file)
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    summary = inspection.summary()
    if options.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    notebook = summary["notebook"]
    print(f"[inspect] {summary['path']}: OK (format v{summary['format_version']})")
    print(f"  notebook: {notebook.get('name', '')} (source id {notebook.get('id', '')})")
    print(f"  exported_at: {summary['exported_at']}")
    print(f"  rows: {sum(summary['rows'].values())}, files: {summary['files']['count']} "
          f"({summary['files']['bytes']} bytes)")
    print(f"  vectors: {'included' if summary['vectors_included'] else 'not included'}")
    for workload, identity in summary["embeddings"].items():
        label = "unbound" if identity is None else f"{identity['model']} dim={identity['dim']}"
        print(f"    {workload}: {label}")
    if summary["kg"]["starts_dirty"]:
        print("  kg: an imported copy starts marked for rebuild")
    return 0


def _import(options) -> int:
    from app.core.config import get_settings
    from app.services.maintenance_cli import (
        MaintenanceCliError,
        open_cli_repository,
        open_maintenance_cli_repository,
    )
    from app.services.notebook_transfer.import_ import NotebookImportError

    files = list(dict.fromkeys(options.file))
    imported: list[dict] = []
    failure: "dict | None" = None
    if options.name is not None and len(files) != 1:
        failure, status = {"file": None, "error": "--name 只能配一个导入文件"}, 2
        return _finish_import(options, imported, failure, status)
    settings = get_settings()
    database_errors = _database_errors()
    status, current = 0, None
    opened = (
        open_cli_repository(settings) if options.dry_run
        else open_maintenance_cli_repository(
            settings, confirm_service_stopped=options.confirm_service_stopped
        )
    )
    try:
        with opened as repository:
            owner = repository.maintenance.resolve_owner_profile(options.owner)
            if owner is None:
                failure = {"file": None, "error": f"找不到用户（或用户名不唯一）: {options.owner}"}
                return _finish_import(options, imported, failure, 2)
            for path in files:
                current = str(path)
                try:
                    entry = _import_one(repository, path, owner.id, options)
                except NotebookImportError as exc:
                    failure, status = {"file": current, "error": str(exc)}, 1
                    break
                except (OSError, *database_errors) as exc:
                    failure, status = {"file": current, "error": _describe(exc)}, 1
                    break
                except Exception as exc:  # noqa: BLE001 -- the service compensated; report it
                    # Anything else (a validation failure, a bug): the import of
                    # this file left nothing behind, and the report must still be
                    # printed. The class name only: a message could carry anything.
                    failure, status = {"file": current, "error": f"导入失败: {type(exc).__name__}"}, 1
                    break
                imported.append(entry)
    except MaintenanceCliError as exc:
        failure, status = {"file": current, "error": str(exc)}, exc.exit_code
    except database_errors as exc:
        failure, status = {"file": current, "error": _describe(exc)}, 1
    if not options.dry_run and options.rebuild_scale == "auto":
        _rebuild_scale(settings, imported)
    return _finish_import(options, imported, failure, status)


def _import_one(repository, path: Path, owner_id: str, options) -> dict:
    """One archive: the import, then (not on a dry run) its re-embedding legs,
    synchronously, in this order (the import has already projected its Knowhow
    tables; the legs must follow that)."""
    service = repository._runtime.notebook_import  # noqa: SLF001 -- CLI composition root
    report = service.import_notebook(
        path, owner_id=owner_id, name=options.name, dry_run=options.dry_run,
        progress=lambda event: _print_progress(path, event),
    )
    entry = {"file": str(path), "import": report.to_dict()}
    if options.dry_run:
        entry["reembed"] = {"state": "dry_run", "legs": list(report.reembed_legs)}
    elif not report.reembed_legs:
        entry["reembed"] = {"state": "not_needed", "legs": []}
    elif options.reembed == "skip":
        entry["reembed"] = {"state": "skipped", "legs": list(report.reembed_legs)}
    else:
        entry["reembed"] = _reembed(repository, report)
    if not options.json:
        _print_import(entry)
    return entry


def _reembed(repository, report) -> dict:
    import dataclasses

    from app.services.notebook_reembed import NotebookReembedService

    try:
        status = NotebookReembedService(repository).run(
            report.notebook_id, legs=report.reembed_legs
        )
    except Exception as exc:  # noqa: BLE001 -- the import itself is done
        # The class name only (as the per-file loop; a database error as
        # ``_describe`` puts it): a model service's message -- an OSError's
        # too, a URLError is one -- can carry its URL.
        error = (
            _describe(exc) if isinstance(exc, _database_errors())
            else f"重新嵌入失败: {type(exc).__name__}"
        )
        return {"state": "failed", "legs": list(report.reembed_legs), "error": error}
    return {
        "state": status.state,
        "legs": list(status.legs),
        "progress": {leg: dataclasses.asdict(p) for leg, p in status.progress.items()},
        "error": status.error,
    }


#: Re-embedding outcomes after which a scale index may be built: an index built
#: on a notebook still missing vectors would be incomplete.
_SCALE_READY = ("succeeded", "not_needed")


def _rebuild_scale(settings, imported: list[dict]) -> None:
    """A full scale-index build of every imported notebook whose vectors are
    complete, after the maintenance repository is closed (``scale_rebuild``
    opens its own). A notebook whose re-embedding was skipped or did not
    succeed is reported as skipped: build its index once its vectors are in."""
    from app.migration.sync.scale_rebuild import aborted_result, rebuild_after_import

    ids = []
    for entry in imported:
        if entry["reembed"]["state"] in _SCALE_READY:
            ids.append(entry["import"]["notebook_id"])
            continue
        entry["scale"] = {
            "outcome": "skipped:reembed_not_done",
            "note": (
                "scale 索引: 向量尚未补齐（重新嵌入被跳过或未成功），本次未重建；补齐向量后"
                "再运行 scripts/build_scale_index.py build --notebook "
                f"{entry['import']['notebook_id']} --full"
            ),
            "warnings": [],
        }
    if not ids:
        return
    try:
        result = rebuild_after_import(settings, ids)
    except BaseException as exc:  # noqa: BLE001 -- the receipt is still printed
        result = aborted_result(ids, exc)
    for entry in imported:
        if "scale" in entry:
            continue
        entry["scale"] = {
            "outcome": result.outcomes.get(entry["import"]["notebook_id"], ""),
            "note": result.note,
            "warnings": list(result.warnings),
        }


def _finish_import(options, imported: list[dict], failure: "dict | None", status: int) -> int:
    if failure is not None:
        where = f"{failure['file']}: " if failure["file"] else ""
        print(f"error: {where}{failure['error']}", file=sys.stderr)
    if options.json:
        print(json.dumps(
            {"imported": imported, "failed": [] if failure is None else [failure]},
            ensure_ascii=False, indent=2,
        ))
    elif options.rebuild_scale == "auto" and not options.dry_run:
        for entry in imported:
            scale = entry.get("scale") or {}
            if scale.get("note"):
                print(f"  {scale['note']}")
            for warning in scale.get("warnings", ()):
                print(f"  warning: {warning}", file=sys.stderr)
    return status


def _print_progress(path: Path, event) -> None:
    if event.stage != "rows":
        print(f"[import] {path.name}: {event.stage}", file=sys.stderr, flush=True)


def _print_import(entry: dict) -> None:
    report = entry["import"]
    label = "would import" if report["dry_run"] else f"-> {report['notebook_id']}"
    rows = sum(report["rows"].values())
    print(f"[import] {entry['file']} {label} 「{report['name']}」 "
          f"({rows} rows, {report['files']} files, owner {report['owner_id']})")
    kept = [w for w, v in report["vectors"].items() if v["kept"]]
    print(f"  vectors kept: {', '.join(kept) or 'none'}; "
          f"reembed: {entry['reembed']['state']} {entry['reembed']['legs']}")
    questions = report["chunk_questions"]
    print(f"  questions: kept {questions['kept']}, re-embedded {questions['reembedded']}, "
          f"dropped {questions['dropped']} {questions['reason']}".rstrip())
    for key in ("cleared", "dropped"):
        for column, count in report[key].items():
            print(f"  {key} {column}: {count}")
    if report["mount_edges_dropped"]:
        print(f"  mount edges not carried: {report['mount_edges_dropped']}")
    if report["sources_reset"]:
        print(f"  unfinished sources set to failed: {report['sources_reset']}")
    if report["global_object_types"]["added"]:
        print(f"  object types added: {', '.join(report['global_object_types']['added'])}")
    for table in report["projection_failures"]:
        print(f"  warning: knowhow table {table} projection failed", file=sys.stderr)


def main(argv: "list[str] | None" = None) -> int:
    options = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if options.command == "export":
        return _export(options)
    if options.command == "import":
        return _import(options)
    return _inspect(options)


if __name__ == "__main__":
    raise SystemExit(main())
