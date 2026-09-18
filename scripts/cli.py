#!/usr/bin/env python3
"""Unified, dependency-free command catalog for silicon-notebook tools.

The catalog selects existing engines; it does not import their application code.
Replacing this process preserves the command's exit status and signal handling.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import NamedTuple, NoReturn


ROOT = Path(__file__).resolve().parent.parent
HELP_FLAGS = frozenset(("-h", "--help"))


class Command(NamedTuple):
    script: str
    description: str
    deployment_env: bool = True
    positional_usage: str | None = None
    module: bool = False


NOTEBOOK_USAGE = "<notebook_id> [--confirm-service-stopped]"
COMMANDS = {
    ("batch",): Command("batch_ingest.py", "批量导入与处理来源"),
    ("scale",): Command("build_scale_index.py", "构建、发布与检查大规模索引"),
    ("diag",): Command("diag.py", "日志、延迟、锁与数据库只读诊断", False),
    ("diagnose", "postgres"): Command("diag_pg_hotpaths.py", "PostgreSQL 热路径诊断"),
    ("diagnose", "retrieval"): Command("diag_retrieval_latency.py", "检索延迟日志分析", False),
    ("diagnose", "mineru"): Command("mineru_probe.py", "测试 MinerU 解析服务"),
    ("maintain", "chunks"): Command("build_chunks.py", "回填笔记本片段与向量", True, NOTEBOOK_USAGE),
    ("maintain", "kg-embeddings"): Command("backfill_kg_embeddings.py", "补全知识对象向量", True, NOTEBOOK_USAGE),
    ("maintain", "reextract"): Command("reextract_notebook.py", "重新提取笔记本来源", True, NOTEBOOK_USAGE),
    ("maintain", "denoise"): Command("denoise_reextract_nb.py", "清理噪声并重新提取知识图谱"),
    ("maintain", "knowhow"): Command("backfill_knowhow_md.py", "回填经验单元 Markdown"),
    ("maintain", "promotion-targets"): Command("backfill_promotion_targets.py", "回填晋升目标"),
    ("maintain", "delete-leftovers"): Command("sweep_legacy_delete_leftovers.py", "清理历史删除残留"),
    ("maintain", "selected-source"): Command("prepare_selected_source_graph.py", "准备所选来源图索引", False),
    ("database", "migrate-postgres"): Command("migrate_sqlite_to_postgres.py", "迁移 SQLite 到 PostgreSQL"),
    ("database", "shadow"): Command("shadow_sqlite_to_postgres.py", "SQLite 与 PostgreSQL 影子校验"),
    ("database", "merge"): Command("merge_dbs.py", "离线合并数据库"),
    ("database", "retrieval-indexes"): Command("build_postgres_retrieval_indexes.py", "构建 PostgreSQL 检索索引"),
    ("database", "hotpath-indexes"): Command("build_hotpath_indexes.py", "构建 PostgreSQL 热路径索引"),
    ("migrate", "model-env"): Command("migrate_legacy_model_env.py", "迁移旧模型环境配置", False),
    ("source", "mineru-batch"): Command("mineru_batch_parse.py", "批量解析 PDF 为 Markdown", False),
    ("source", "embed-images"): Command("embed_md_images.py", "将 Markdown 图片转换为内嵌图片", False),
    ("audit", "source-facts"): Command("audit_source_facts.py", "审计来源事实", False),
    ("audit", "kg-edges"): Command("audit_kg_edge_contract.py", "审计知识图谱边契约"),
    ("audit", "kg-quality"): Command("kg_quality_audit.py", "审计知识图谱质量"),
    ("eval", "replay"): Command("replay_retrieval.py", "重放检索评测"),
    ("eval", "selected-source"): Command("eval_selected_source_graph.py", "评测所选来源图检索"),
    ("eval", "trace-export"): Command("export_reasoning_traces.py", "从显式指定数据库导出推理轨迹", False),
    ("eval", "trace-analyze"): Command("analyze_reasoning_trace.py", "分析推理轨迹文件", False),
    ("eval", "shadow"): Command("reflect_shadow_rig.py", "运行反思影子评测", False),
    ("kg", "build"): Command("app.scripts.build_kg", "离线构建笔记本知识图谱", True, NOTEBOOK_USAGE, True),
    ("kg", "recluster"): Command("app.scripts.recluster_kg", "重建知识对象规范簇", True, NOTEBOOK_USAGE, True),
    ("kg", "reembed"): Command("app.scripts.reembed_kg", "清空并重建知识与关系向量", True, NOTEBOOK_USAGE, True),
    ("kg", "backfill-relations"): Command("app.scripts.backfill_relation_embeddings", "补全关系向量", True, NOTEBOOK_USAGE, True),
    ("extensions", "check"): Command("check_cli_extensions.py", "检查插件导入与配置"),
    ("extensions", "parity"): Command("check_deployment_extension_parity.py", "检查部署插件能力配对"),
    ("extensions", "services"): Command("extension_services.py", "插件配套服务启停、状态与日志", False),
}
ALIASES = {"batch-ingest": "batch"}
SELECTED_ENV_COMMANDS = frozenset({("maintain", "selected-source")})


def print_help(prefix: tuple[str, ...] = (), *, stream=None) -> None:
    stream = stream or sys.stdout
    stem = " ".join(("scripts/cli.sh", *prefix))
    print(f"用法: {stem} <命令> [参数...]\n", file=stream)
    for path, command in COMMANDS.items():
        if path[:len(prefix)] == prefix:
            print(f"  {' '.join(path[len(prefix):]):<26} {command.description}", file=stream)
    print("\n命令后加 --help 查看原有参数；原脚本入口继续可用。", file=stream)
    if not prefix:
        print("batch-ingest 是 batch 的别名。diag 保留原有子命令及默认 slow 行为。", file=stream)


def _exec_without_dotenv(arguments: list[str]) -> NoReturn:
    # Standalone tools own their env-file/explicit-input contracts. Only make
    # repository imports available; never load root .env on their behalf.
    env = os.environ.copy()
    backend = str(ROOT / "backend")
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = backend + (os.pathsep + inherited if inherited else "")
    os.execve(sys.executable, [sys.executable, *arguments], env)


def _requests_help(arguments: list[str]) -> bool:
    for argument in arguments:
        if argument == "--":
            return False
        if argument in HELP_FLAGS:
            return True
    return False


def _selected_path_env_file(arguments: list[str]) -> Path | None:
    """Read only the file-selection switches; the engine owns validation.

    The engine accepts argparse's long-option abbreviations. Leave the full
    argument vector untouched and defer malformed options to their own parser.
    None means no file read for malformed options.
    """
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--env-file")
    try:
        options, _ = parser.parse_known_args(arguments)
    except argparse.ArgumentError:
        return None
    if options.env_file is not None:
        return Path(options.env_file).resolve()
    return ROOT / ".env"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in HELP_FLAGS or args == ["help"]:
        print_help()
        return 0
    args[0] = ALIASES.get(args[0], args[0])
    path = (args[0],)
    if path not in COMMANDS:
        if not any(key[0] == args[0] for key in COMMANDS):
            print("未知命令；运行 scripts/cli.sh --help 查看可用命令。", file=sys.stderr)
            return 2
        if len(args) == 1 or args[1] in HELP_FLAGS:
            print_help(path)
            return 0
        path = tuple(args[:2])
        if path not in COMMANDS:
            print("未知子命令；请查看该命令组的 --help。", file=sys.stderr)
            return 2
    command = COMMANDS[path]
    rest = args[len(path):]
    # Legacy positional engines interpret --help as a notebook identity. Do
    # not import them, construct Settings, or open a repository for help.
    if command.positional_usage and (not rest or _requests_help(rest)):
        print(f"用法: scripts/cli.sh {' '.join(path)} {command.positional_usage}")
        print(command.description)
        if command.script == "backfill_kg_embeddings.py":
            print("环境变量: BACKFILL_SLEEP（默认 3 秒）、BACKFILL_MAX_ROUNDS（默认 80）。")
        return 0 if rest else 2
    arguments = (
        ["-m", command.script, *rest]
        if command.module
        else [str(ROOT / "scripts" / command.script), *rest]
    )
    # Leaf help retains each engine's full parser without loading deployment
    # dotenv. Existing parsers exit before application/plugin composition.
    if _requests_help(rest):
        _exec_without_dotenv(arguments)
    elif path in SELECTED_ENV_COMMANDS:
        selected_file = _selected_path_env_file(rest)
        if selected_file is None:
            _exec_without_dotenv(arguments)
        else:
            from python_env import exec_python

            exec_python(arguments, root=ROOT, paths_only=True, path_env_file=selected_file)
    elif not command.deployment_env:
        _exec_without_dotenv(arguments)
    else:
        from python_env import exec_python

        exec_python(arguments, root=ROOT)
    return 0  # reachable only when exec is replaced by a test double


if __name__ == "__main__":
    raise SystemExit(main())
