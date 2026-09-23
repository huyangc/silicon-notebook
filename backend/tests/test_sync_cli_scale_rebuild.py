"""守卫: PR-4 T2——`sync import` 完成后的 scale 索引自动重建
(`app.migration.sync.scale_rebuild` + `cli._cmd_import` 的接线)。

这里不真建索引：`import_package` 与 `app.services.scale_build_cli` 的
`verify_migration_ledger`/`open_scale_build_repository`/`run_build` 全部
monkeypatch，只盯本任务自己的契约：

- 「要不要 scale 索引」只问服务内那一个 `status()["eligible"]`，不在 sync 侧另立阈值；
- 需要的**一律全量重建**：包对已索引行是替换语义，追加式 fold 的前提不成立
  （codex #791 R1 P1）；
- 组装仓库之前必须先过迁移 ledger 闸（与 `scale_build_cli.main` 同序）；
- 「构建器拒绝」「别处正在建」是跳过不是失败，只有真失败才发警告；
- 三种「本来就不该重建」的跑（`--dry-run`、`already_applied`、`--rebuild-scale skip`）
  对建索引桩必须是零调用；
- 重建失败/中断只进报告与警告，**不改变导入的退出码**——导入已经 done；
- 人读模式下导入摘要先于 scale 段打出（重建可能跑几个小时）；
- `--json` 带 `scale_rebuild` / `scale_rebuild_mode` / `scale_rebuild_note`，
  且这三个字段是 CLI 层合并的（不落 `sync_imports.report_json`）。

真正的构建路径由 tests/test_scale_build_cli*.py 覆盖，导入相位本身由
tests/test_sync_import.py 覆盖。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.migration.sync import cli, scale_rebuild
from app.migration.sync.import_ import (
    ImportReport,
    UserMappingResult,
)
from app.services import scale_build_cli


def _settings(tmp_path, monkeypatch, *, postgres: bool = True) -> Settings:
    url = (
        "postgresql://sync:sync@localhost:5432/sync_cli_scale"
        if postgres
        else f"sqlite:///{tmp_path / 'sync_cli.db'}"
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("SILICON_NOTEBOOK_SYNC_ENV", raising=False)
    return Settings()


def _report(**overrides) -> ImportReport:
    fields = dict(
        package_id="pkg-id",
        source_env="dev",
        package_created_at="2026-01-01T00:00:00+00:00",
        already_applied=False,
        dry_run=False,
        notebooks=(),
        tables={},
        skipped_rows=(),
        skipped_row_total=0,
        user_mapping=UserMappingResult(matched={}, created={}, unmatched=()),
        groups_created=(),
        files_copied=0,
        warnings=(),
    )
    fields.update(overrides)
    return ImportReport(**fields)


class _FakeRepository:
    """The one read the rebuild makes, and nothing else.

    ``notebooks`` maps a notebook id to its ``eligible`` verdict; an id that is
    absent raises ``KeyError``, the way ``scale_index_status`` does for a
    notebook this environment does not have (live). ``states`` only fills the
    ``state`` field so a test can prove it is NOT consulted.
    """

    def __init__(self, *, notebooks=None, states=None, status_error=None):
        self._notebooks = dict(notebooks or {})
        self._states = dict(states or {})
        self._status_error = dict(status_error or {})
        self.closed = False

    def scale_index_status(self, notebook_id: str) -> dict:
        if notebook_id in self._status_error:
            raise self._status_error[notebook_id]
        if notebook_id not in self._notebooks:
            raise KeyError(notebook_id)
        return {
            "eligible": self._notebooks[notebook_id],
            "state": self._states.get(notebook_id, "stale"),
            "exists": True,
        }

    def notebook_copy_stats(self, notebook_id: str) -> dict:  # pragma: no cover
        raise AssertionError(
            "copyable is already the last clause of eligible(); asking it "
            "separately would be a second definition of 'large enough to index'"
        )

    def _resolve_scale_mode(self, notebook_id: str, mode: str) -> str:
        raise AssertionError(
            "fold/full must NOT be resolved here: a package replaces rows that "
            "are already indexed, so an append-only fold is unsound"
        )


def _eligible(*notebook_ids, **kwargs) -> _FakeRepository:
    return _FakeRepository(
        notebooks={notebook_id: True for notebook_id in notebook_ids}, **kwargs
    )


def _install(
    monkeypatch, repository, *, run_build=None, open_error=None, ledger_error=None
):
    """Point the rebuild at ``repository``.

    Returns a recorder with ``builds`` (the ``(notebook_id, mode)`` pairs
    ``run_build`` was called with), ``opened`` and ``ledger_checks``.
    """
    rig = SimpleNamespace(builds=[], opened=0, ledger_checks=[])

    def fake_ledger(database_url):
        rig.ledger_checks.append(database_url)
        if ledger_error is not None:
            raise ledger_error
        return (1, 1)

    @contextmanager
    def fake_open(settings):
        rig.opened += 1
        if open_error is not None:
            raise open_error
        try:
            yield repository
        finally:
            repository.closed = True

    def default_run_build(repo, notebook_id, *, mode, report):
        rig.builds.append((notebook_id, mode))
        report("stage build: 1 ms")
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    def recording_run_build(repo, notebook_id, *, mode, report):
        rig.builds.append((notebook_id, mode))
        return run_build(repo, notebook_id, mode=mode, report=report)

    monkeypatch.setattr(scale_build_cli, "verify_migration_ledger", fake_ledger)
    monkeypatch.setattr(scale_build_cli, "open_scale_build_repository", fake_open)
    monkeypatch.setattr(
        scale_build_cli,
        "run_build",
        default_run_build if run_build is None else recording_run_build,
    )
    return rig


def _run_import(tmp_path, monkeypatch, report, *argv) -> int:
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    return cli.main(["import", str(tmp_path / "pkg"), *argv])


# ------------------------------------------------------------------ 参数解析


def test_build_parser_rebuild_scale_defaults_to_auto():
    args = cli.build_parser().parse_args(["import", "/tmp/pkg"])
    assert args.rebuild_scale == scale_rebuild.REBUILD_AUTO


def test_build_parser_rebuild_scale_accepts_skip():
    args = cli.build_parser().parse_args(
        ["import", "/tmp/pkg", "--rebuild-scale", "skip"]
    )
    assert args.rebuild_scale == scale_rebuild.REBUILD_SKIP


def test_build_parser_rebuild_scale_rejects_other_values():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["import", "/tmp/pkg", "--rebuild-scale", "always"]
        )


# ------------------------------------------------------------------ 候选集合


def test_candidates_drop_notebooks_the_import_just_retired():
    """3d 已经把 nb-2 翻成 deleting 并排了删除作业；给它建索引会和那个作业抢
    同一批工件根目录。"""
    assert scale_rebuild.rebuild_candidates(
        ("nb-1", "nb-2", "nb-3"), ("nb-2",)
    ) == ("nb-1", "nb-3")


def test_candidates_drop_notebooks_the_import_found_mid_copy():
    """`notebooks_delete_skipped` 是导入自己认定的「目标端正在深拷贝」。那种本
    对 `require_write_admission` 不算 live，构建器一定拒绝——与其产出一条运维
    没法处理的回执，不如用导入已经拿到的事实直接不列入候选。"""
    assert scale_rebuild.rebuild_candidates(
        ("nb-1", "nb-2"), (), ("nb-2",)
    ) == ("nb-1",)


def test_candidates_preserve_order_and_deduplicate():
    assert scale_rebuild.rebuild_candidates(("nb-2", "nb-1", "nb-2")) == (
        "nb-2",
        "nb-1",
    )


def test_copying_notebooks_never_reach_the_builder(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1", "nb-2"))
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(
            notebooks=("nb-1", "nb-2"), notebooks_delete_skipped=("nb-2",)
        ),
        "--json",
    )
    assert exit_code == 0
    assert rig.builds == [("nb-1", "full")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {"nb-1": "built"}


# ------------------------------------------------------------------ 判定口径


def test_every_eligible_notebook_is_rebuilt_in_full(tmp_path, monkeypatch, capsys):
    """codex #791 R1 P1：`state` 与 fold 都只对**新增**敏感。包对已索引行是替换
    语义（同一 source 的 chunks/knowledge 被原地覆盖、或整条 source 被删），这种
    改写后 delta 为空、`state` 仍是 `indexed`、fold 原样返回旧 manifest——两道闸
    都会把一份陈旧索引留在线上。所以：eligible 的本一律全量重建，`state` 不参与
    判定。"""
    _settings(tmp_path, monkeypatch)
    repository = _eligible(
        "nb-indexed",
        "nb-stale",
        "nb-unindexed",
        states={
            "nb-indexed": "indexed",
            "nb-stale": "stale",
            "nb-unindexed": "unindexed",
        },
    )
    rig = _install(monkeypatch, repository)
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(notebooks=("nb-indexed", "nb-stale", "nb-unindexed")),
        "--json",
    )
    assert exit_code == 0
    assert rig.builds == [
        ("nb-indexed", "full"),
        ("nb-stale", "full"),
        ("nb-unindexed", "full"),
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-indexed": "built",
        "nb-stale": "built",
        "nb-unindexed": "built",
    }


def test_the_mode_is_never_anything_but_full(tmp_path, monkeypatch, capsys):
    """`_FakeRepository._resolve_scale_mode` 直接 assert 失败：这个模块不许再去
    问 fold/full。"""
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",)), "--json"
    )
    assert exit_code == 0
    assert {mode for _, mode in rig.builds} == {"full"}
    payload = json.loads(capsys.readouterr().out)
    # 没有 "folded" 这个回执值了。
    assert "folded" not in payload["scale_rebuild"].values()


def test_ineligible_notebook_is_left_alone(tmp_path, monkeypatch, capsys):
    """`eligible` 是服务内唯一那一份「这本要不要 scale 索引」的定义——它最后一条
    分支就是 `not copyable`，所以体量小的本已经折在里面，不需要第二份口径。"""
    _settings(tmp_path, monkeypatch)
    repository = _FakeRepository(notebooks={"nb-1": True, "nb-2": False})
    rig = _install(monkeypatch, repository)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    assert rig.builds == [("nb-1", "full")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"]["nb-2"] == "skipped:not_eligible"
    assert payload["warnings"] == []


def test_unknown_notebook_is_skipped_not_failed(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-gone")), "--json"
    )
    assert exit_code == 0
    assert rig.builds == [("nb-1", "full")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"]["nb-gone"] == "skipped:unknown_notebook"
    assert payload["warnings"] == []


# --------------------------------------------------------------- 迁移 ledger


def test_ledger_is_verified_before_the_repository_is_composed(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",)), "--json"
    )
    assert exit_code == 0
    assert len(rig.ledger_checks) == 1
    assert rig.opened == 1
    json.loads(capsys.readouterr().out)


def test_ledger_mismatch_refuses_before_composing_anything(
    tmp_path, monkeypatch, capsys
):
    """checkout 与线上库差一个迁移时，用错版本的代码建出来的索引是**静默错误**的
    ——而且会原子换名顶掉一份健康索引。`scale_build_cli.main` 为此在组装之前跑
    ledger 闸；自动路径不许绕过它。"""
    _settings(tmp_path, monkeypatch)
    rig = _install(
        monkeypatch,
        _eligible("nb-1", "nb-2"),
        ledger_error=scale_build_cli.ScaleBuildCliError("ledger mismatch"),
    )
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    assert rig.opened == 0
    assert rig.builds == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-1": "failed:ScaleBuildCliError",
        "nb-2": "failed:ScaleBuildCliError",
    }
    assert any("build_scale_index.py" in w for w in payload["warnings"])


# ------------------------------------------------------- 拒绝/忙 是跳过不是失败


def test_builder_refusal_is_a_skip_without_a_warning(tmp_path, monkeypatch, capsys):
    """`run_build` 对「不是 live 笔记本」（正在拷贝/导入/删除）以及不可用的
    indexing pipeline 抛 `ScaleBuildCliError`：它在动手之前就拒绝了。运维手动
    重跑同一条命令也会被同样拒绝，所以不发警告。"""
    _settings(tmp_path, monkeypatch)

    def refusing(repo, notebook_id, *, mode, report):
        raise scale_build_cli.ScaleBuildCliError(f"unknown notebook: {notebook_id}")

    _install(monkeypatch, _eligible("nb-1"), run_build=refusing)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",)), "--json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {"nb-1": "skipped:refused_by_builder"}
    assert payload["warnings"] == []


def test_another_builder_holding_the_claim_is_a_skip(tmp_path, monkeypatch, capsys):
    """`status()["building"]` 读的是进程内集合，在新起的 CLI 进程里恒为空，所以
    在线服务正在建的本一定走到这里才被发现——跨进程 claim 才是那个真答案。它
    说明系统在正常工作，不是重建出了问题。"""
    _settings(tmp_path, monkeypatch)

    def busy_first(repo, notebook_id, *, mode, report):
        if notebook_id == "nb-1":
            raise scale_build_cli.ScaleBuildCliBusy(
                "another builder holds /srv/storage/kg_index/nb-1"
            )
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    _install(monkeypatch, _eligible("nb-1", "nb-2"), run_build=busy_first)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {"nb-1": "skipped:busy", "nb-2": "built"}
    assert payload["warnings"] == []


def test_busy_stays_a_scale_build_cli_failure_for_existing_callers():
    """子类化是为了不改既有调用方：`main` 的 `except ScaleBuildCliFailure`
    （退出码 1）必须继续接住它。"""
    assert issubclass(
        scale_build_cli.ScaleBuildCliBusy, scale_build_cli.ScaleBuildCliFailure
    )


# ------------------------------------------------------------------ 失败隔离


def test_build_failure_keeps_exit_code_zero_and_records_the_class(
    tmp_path, monkeypatch, capsys
):
    """导入已经 done，重建失败不能把它变成失败的跑。报告只记异常类名——失败
    文本可能带存储路径或数据库 URL，而这份回执要被打印和序列化。"""
    _settings(tmp_path, monkeypatch)

    def flaky(repo, notebook_id, *, mode, report):
        if notebook_id == "nb-1":
            raise scale_build_cli.ScaleBuildCliFailure(
                "swap refused under /srv/storage/kg_index/nb-1"
            )
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    rig = _install(monkeypatch, _eligible("nb-1", "nb-2"), run_build=flaky)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    # 每本互不影响：nb-1 失败之后 nb-2 仍然跑了。
    assert [notebook for notebook, _ in rig.builds] == ["nb-1", "nb-2"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-1": "failed:ScaleBuildCliFailure",
        "nb-2": "built",
    }
    warning = "".join(payload["warnings"])
    assert "nb-1" in warning
    assert "build_scale_index.py" in warning
    assert "/srv/storage" not in warning


def test_repository_open_failure_marks_every_candidate(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    _install(monkeypatch, _eligible("nb-1"), open_error=RuntimeError("no connection"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-1": "failed:RuntimeError",
        "nb-2": "failed:RuntimeError",
    }


def test_status_read_failure_does_not_stop_the_next_notebook(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    repository = _eligible(
        "nb-1", "nb-2", status_error={"nb-1": RuntimeError("read timeout")}
    )
    rig = _install(monkeypatch, repository)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    assert rig.builds == [("nb-2", "full")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"]["nb-1"] == "failed:RuntimeError"


def test_ctrl_c_stops_building_without_failing_the_import(
    tmp_path, monkeypatch, capsys
):
    """Ctrl-C 的意思是「别建了」，不是「导入失败了」：剩下的本记成
    skipped:interrupted，但 `sync import` 仍然退出 0 并打出导入摘要——导入在这
    一步开始之前就已经提交，报成中断会把运维推去重跑一个已经应用过的包。"""
    _settings(tmp_path, monkeypatch)

    def interrupted(repo, notebook_id, *, mode, report):
        if notebook_id == "nb-2":
            raise KeyboardInterrupt
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    rig = _install(
        monkeypatch, _eligible("nb-1", "nb-2", "nb-3"), run_build=interrupted
    )
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2", "nb-3")), "--json"
    )
    assert exit_code == 0
    # nb-3 未被尝试：运维说了停。
    assert [notebook for notebook, _ in rig.builds] == ["nb-1", "nb-2"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-1": "built",
        "nb-2": "skipped:interrupted",
        "nb-3": "skipped:interrupted",
    }
    assert any("Ctrl-C" in warning for warning in payload["warnings"])


def test_ctrl_c_while_composing_the_repository_is_also_absorbed(
    tmp_path, monkeypatch, capsys
):
    """组装（`prime_extension_admission`）和收尾各自是秒级真实墙钟；Ctrl-C 落在
    那里曾经会穿到 `main`，把一次已提交的导入报成 interrupted / 退出 2。"""
    _settings(tmp_path, monkeypatch)
    _install(monkeypatch, _eligible("nb-1"), open_error=KeyboardInterrupt())
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2"), files_copied=7)
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "文件数: 7" in out
    assert "scale 索引: 重建 0、跳过 2、失败 0" in out
    assert "  nb-1: skipped:interrupted" in out
    assert "  nb-2: skipped:interrupted" in out


def test_ctrl_c_escaping_the_pass_still_yields_a_report():
    """`aborted_result` 是最后一道兜底（第二次 Ctrl-C 正好落在结果构造上）。"""
    result = scale_rebuild.aborted_result(("nb-1",), KeyboardInterrupt())
    assert result.outcomes == {"nb-1": "skipped:interrupted"}
    other = scale_rebuild.aborted_result(("nb-1",), MemoryError())
    assert other.outcomes == {"nb-1": "failed:MemoryError"}


# ------------------------------------------------------------- 不该重建的跑


def test_dry_run_never_touches_the_builder(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(notebooks=("nb-1",), dry_run=True),
        "--dry-run",
    )
    assert exit_code == 0
    assert rig.builds == []
    assert rig.ledger_checks == []
    assert "scale 索引:" not in capsys.readouterr().out


def test_already_applied_never_touches_the_builder(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",), already_applied=True)
    )
    assert exit_code == 0
    assert rig.builds == []
    assert rig.ledger_checks == []
    assert "scale 索引:" not in capsys.readouterr().out


def test_rebuild_scale_skip_never_touches_the_builder(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(notebooks=("nb-1",)),
        "--rebuild-scale",
        "skip",
    )
    assert exit_code == 0
    assert rig.builds == []
    assert rig.ledger_checks == []
    out = capsys.readouterr().out
    assert "--rebuild-scale skip" in out
    assert "build_scale_index.py" in out


def test_package_without_notebooks_prints_no_scale_section(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    rig = _install(monkeypatch, _eligible())
    exit_code = _run_import(tmp_path, monkeypatch, _report())
    assert exit_code == 0
    assert rig.builds == []
    assert "scale 索引" not in capsys.readouterr().out


# ------------------------------------------------------------------- SQLite


def test_sqlite_backend_skips_without_opening_a_builder(
    tmp_path, monkeypatch, capsys
):
    """SQLite 没有跨进程建索引 claim（`scale_build_cli.require_postgres` 的理由），
    所以离线构建器根本不可用。这是「跳过」不是「失败」。"""
    _settings(tmp_path, monkeypatch, postgres=False)
    rig = _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(tmp_path, monkeypatch, _report(notebooks=("nb-1",)))
    assert exit_code == 0
    assert rig.builds == []
    assert rig.opened == 0
    assert rig.ledger_checks == []
    out = capsys.readouterr().out
    assert "SQLite 后端不支持离线 scale 构建" in out
    assert "scale 索引: 重建 0、跳过 1、失败 0" in out
    assert "  nb-1: skipped:sqlite_backend" in out


def test_sqlite_backend_json_records_every_notebook(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, postgres=False)
    _install(monkeypatch, _eligible())
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1", "nb-2")), "--json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {
        "nb-1": "skipped:sqlite_backend",
        "nb-2": "skipped:sqlite_backend",
    }
    assert "SQLite" in payload["scale_rebuild_note"]


# --------------------------------------------------------------------- 输出


def test_human_summary_counts_and_lists_only_the_unfinished(
    tmp_path, monkeypatch, capsys
):
    """成功的本不逐行刷屏；跳过和失败的逐本一行，因为那是运维要跟进的。"""
    _settings(tmp_path, monkeypatch)
    repository = _FakeRepository(
        notebooks={"nb-built": True, "nb-skipped": False, "nb-failed": True}
    )

    def flaky(repo, notebook_id, *, mode, report):
        if notebook_id == "nb-failed":
            raise RuntimeError("boom")
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    _install(monkeypatch, repository, run_build=flaky)
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(
            notebooks=("nb-built", "nb-skipped", "nb-failed"), files_copied=3
        ),
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scale 索引: 重建 1、跳过 1、失败 1" in out
    assert "  nb-skipped: skipped:not_eligible" in out
    assert "  nb-failed: failed:RuntimeError" in out
    assert "nb-built:" not in out


def test_import_summary_is_printed_before_the_rebuild_starts(
    tmp_path, monkeypatch, capsys
):
    """重建可能跑几个小时。摘要必须先落地，否则运维对着一个不动的屏幕不知道
    导入到底成没成。"""
    _settings(tmp_path, monkeypatch)
    seen_at_build_time: list[str] = []

    def peeking(repo, notebook_id, *, mode, report):
        seen_at_build_time.append(capsys.readouterr().out)
        return {"notebook_id": notebook_id, "mode": mode, "result": {}}

    _install(monkeypatch, _eligible("nb-1"), run_build=peeking)
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",), files_copied=3)
    )
    assert exit_code == 0
    # 构建开始那一刻，导入摘要已经在 stdout 上了，scale 段还没有。
    assert "文件数: 3" in seen_at_build_time[0]
    assert "scale 索引:" not in seen_at_build_time[0]
    assert "scale 索引: 重建 1" in capsys.readouterr().out


def test_detail_lines_are_capped_like_the_mirror_section(
    tmp_path, monkeypatch, capsys
):
    """一次导入可以涉及上百本；逐本刷屏会把真正要跟进的那几行埋掉。"""
    _settings(tmp_path, monkeypatch)
    notebooks = tuple(f"nb-{index:03d}" for index in range(25))
    repository = _FakeRepository(
        notebooks={notebook_id: False for notebook_id in notebooks}
    )
    _install(monkeypatch, repository)
    exit_code = _run_import(tmp_path, monkeypatch, _report(notebooks=notebooks))
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scale 索引: 重建 0、跳过 25、失败 0" in out
    assert out.count(": skipped:not_eligible") == 20
    assert "另有 5 本未列出" in out


def test_rebuild_warnings_are_their_own_section_after_the_import_summary(
    tmp_path, monkeypatch, capsys
):
    """导入自己的警告和重建的警告分开成段：后者是导入完成之后才产生的，混进
    前者会让人以为导入本身出了问题。"""
    _settings(tmp_path, monkeypatch)

    def failing(repo, notebook_id, *, mode, report):
        raise RuntimeError("boom")

    _install(monkeypatch, _eligible("nb-1"), run_build=failing)
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(notebooks=("nb-1",), warnings=("导入自己的一条警告",)),
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "  导入自己的一条警告" in out
    assert "scale 重建警告:" in out
    assert "nb-1 的 scale 索引重建失败" in out
    assert out.index("警告:") < out.index("scale 重建警告:")


def test_build_stage_lines_go_to_stderr_not_stdout(tmp_path, monkeypatch, capsys):
    """`--json` 的 stdout 必须只有那一个 JSON 对象。"""
    _settings(tmp_path, monkeypatch)
    _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path, monkeypatch, _report(notebooks=("nb-1",)), "--json"
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["scale_rebuild"] == {"nb-1": "built"}
    assert "scale[nb-1] stage build: 1 ms" in captured.err


def test_build_settings_carry_the_offline_statement_timeout(tmp_path, monkeypatch):
    """在线默认 statement timeout 是给交互请求定的，会掐死一次多小时的构建。"""
    settings = _settings(tmp_path, monkeypatch)
    seen: dict = {}

    @contextmanager
    def fake_open(build_settings):
        seen["timeout"] = build_settings.postgres_statement_timeout_seconds
        yield _FakeRepository(notebooks={"nb-1": False})

    monkeypatch.setattr(
        scale_build_cli, "verify_migration_ledger", lambda url: (1, 1)
    )
    monkeypatch.setattr(scale_build_cli, "open_scale_build_repository", fake_open)
    result = scale_rebuild.rebuild_after_import(settings, ("nb-1",))
    assert result.outcomes == {"nb-1": "skipped:not_eligible"}
    assert seen["timeout"] == scale_build_cli.DEFAULT_STATEMENT_TIMEOUT_SECONDS


def test_rebuild_after_import_with_no_candidates_opens_nothing(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)

    def explode(*args, **kwargs):
        raise AssertionError("must not touch the builder for zero candidates")

    monkeypatch.setattr(scale_build_cli, "verify_migration_ledger", explode)
    monkeypatch.setattr(scale_build_cli, "open_scale_build_repository", explode)
    assert scale_rebuild.rebuild_after_import(
        settings, ()
    ) == scale_rebuild.ScaleRebuildResult()


# ------------------------------------------------------------- JSON 形状


def test_json_carries_mode_and_note_so_an_empty_map_is_never_ambiguous(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    _install(monkeypatch, _eligible("nb-1"))
    exit_code = _run_import(
        tmp_path,
        monkeypatch,
        _report(notebooks=("nb-1",)),
        "--rebuild-scale",
        "skip",
        "--json",
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {}
    assert payload["scale_rebuild_mode"] == "skip"
    assert "--rebuild-scale skip" in payload["scale_rebuild_note"]


def test_json_auto_with_nothing_to_do_is_an_empty_map_and_no_note(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    _install(monkeypatch, _eligible())
    exit_code = _run_import(tmp_path, monkeypatch, _report(), "--json")
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_rebuild"] == {}
    assert payload["scale_rebuild_mode"] == "auto"
    assert payload["scale_rebuild_note"] is None


def test_import_report_json_does_not_leak_into_the_stored_report(
    tmp_path, monkeypatch
):
    """三个 scale 字段只在 CLI 层合并：重建发生在 `sync_imports.report_json`
    落盘之后，`ImportReport.as_json()` 带上它们就只会永远存空值。"""
    report = _report(notebooks=("nb-1",))
    stored = report.as_json()
    assert not [key for key in stored if key.startswith("scale_rebuild")]
    merged = cli._import_report_as_json(
        report,
        scale_rebuild.ScaleRebuildResult(outcomes={"nb-1": "built"}),
        "auto",
    )
    assert merged["scale_rebuild"] == {"nb-1": "built"}
    assert report.as_json() == stored


def test_package_dir_and_flags_still_reach_import_package(tmp_path, monkeypatch):
    """新增的 `--rebuild-scale` 不是 `import_package` 的参数。"""
    _settings(tmp_path, monkeypatch, postgres=False)
    captured: dict = {}

    def fake_import(settings_arg, package_dir, **kwargs):
        captured["package_dir"] = package_dir
        captured["kwargs"] = kwargs
        return _report()

    monkeypatch.setattr(cli, "import_package", fake_import)
    exit_code = cli.main(
        ["import", str(tmp_path / "pkg"), "--rebuild-scale", "skip"]
    )
    assert exit_code == 0
    assert captured["package_dir"] == Path(tmp_path / "pkg")
    assert "rebuild_scale" not in captured["kwargs"]
