#!/usr/bin/env python3
"""Single-file notebook archive CLI: ``export`` and ``inspect``.

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
stop at the first failure; the archives already written stay, and ``--json``
prints ``{"exported": [...], "failed": [...]}`` either way.

Import (``notebook import``) is not part of this build yet. The format is
``app.domain.notebook_archive``; docs/operations.md has the runbook.
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
    reports = []
    failure: "dict | None" = None
    status = 0
    try:
        with open_cli_repository(settings) as repository:
            store = repository._runtime.sharing_store  # noqa: SLF001 -- CLI composition root
            embeddings = embedding_identities(settings)
            schemas = repository.list_object_schemas()
            for notebook_id, path in targets:
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
                except NotebookExportError as exc:
                    failure = {"notebook_id": notebook_id, "error": str(exc)}
                    status = 1
                    break
                reports.append(report)
                if not options.json:
                    _print_export(report)
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if failure is not None:
        # The archives already written stay; the report says which ones.
        print(f"error: {failure['notebook_id']}: {failure['error']}", file=sys.stderr)
    if options.json:
        print(json.dumps(
            {"exported": [report.to_dict() for report in reports],
             "failed": [] if failure is None else [failure]},
            ensure_ascii=False, indent=2,
        ))
    return status


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


def main(argv: "list[str] | None" = None) -> int:
    options = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if options.command == "export":
        return _export(options)
    return _inspect(options)


if __name__ == "__main__":
    raise SystemExit(main())
