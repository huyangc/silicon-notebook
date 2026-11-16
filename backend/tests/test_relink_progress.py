from contextvars import Context
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.kg.relink_progress import (
    observe_relink_progress,
    report_relink_progress,
)


def _progress(done=0, elapsed=0, total=3, **extra):
    return dict(processed_sources=done, total_sources=total,
                pending_sources=total - done, edges_added=done,
                elapsed_seconds=elapsed, resumed=False, **extra)


def test_observers_are_scoped_cleaned_up_and_do_not_expose_content():
    outer, inner = [], []
    with observe_relink_progress(outer.append):
        with pytest.raises(RuntimeError), observe_relink_progress(inner.append):
            report_relink_progress(_progress(source_id="private", text="secret"))
            Context().run(report_relink_progress, _progress())
            raise RuntimeError("stop")
        report_relink_progress(_progress(1))
    report_relink_progress(_progress(2))
    assert len(outer) == 2
    assert len(inner) == 1
    assert outer[0] == _progress()
    assert "source_id" not in inner[0]
    assert "text" not in inner[0]


def test_observer_failure_and_mutation_do_not_affect_other_observers():
    seen = []

    def broken(payload):
        payload["processed_sources"] = 900
        raise ValueError("private error")

    with observe_relink_progress(broken), observe_relink_progress(seen.append):
        report_relink_progress(_progress(1))
    assert seen == [_progress(1)]


def test_cli_relink_progress_prints_start_interval_and_completion(capsys):
    from app.services.batch_ingest import _relink_progress_printer

    events = []
    observer = _relink_progress_printer(15, events.append)
    observer(_progress())
    observer(_progress(1, elapsed=1))
    observer(_progress(2, elapsed=16))
    observer(_progress(3, elapsed=17))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert "0/3 来源" in lines[0]
    assert "2/3 来源" in lines[1]
    assert "3/3 来源" in lines[2]
    assert "待处理 0" in lines[2]
    assert [row["processed_sources"] for row in events] == [0, 2, 3]


def test_cli_relink_progress_reports_empty_and_resumed_runs(capsys):
    from app.services.batch_ingest import _relink_progress_printer

    observer = _relink_progress_printer(15, lambda _: None)
    observer({**_progress(total=0), "resumed": True})
    output = capsys.readouterr().out
    assert "0/0 来源" in output
    assert "继续未完成任务" in output


def test_cli_reports_total_after_receipt_preparation(capsys):
    from app.services.batch_ingest import _relink_progress_printer

    observer = _relink_progress_printer(15, lambda _: None)
    with observe_relink_progress(observer):
        report_relink_progress(_progress(total=0, preparing=True))
    preparation = capsys.readouterr().out
    assert "正在准备补连队列" in preparation
    assert "0/0" not in preparation
    observer(_progress(total=3, elapsed=1))
    assert "0/3 来源" in capsys.readouterr().out


def test_kg_command_subscribes_during_build_and_cleans_up(capsys):
    from app.services.batch_ingest import run_kg

    def build(_notebook_id, **_kwargs):
        report_relink_progress(_progress())
        report_relink_progress(_progress(3, elapsed=1))
        return {"built": [], "failed": []}

    repo = SimpleNamespace(
        configured=lambda _: True,
        settings=SimpleNamespace(kg_incremental_fusion_enabled=True),
        maintenance=SimpleNamespace(),
        build_notebook_kg=build,
    )
    run_kg(repo, "test", no_rebuild=True, report_interval=0)
    lines = capsys.readouterr().out
    assert "[relink] 补上关联: 0/3 来源" in lines
    assert "[relink] 补上关联: 3/3 来源" in lines
    report_relink_progress(_progress())
    assert capsys.readouterr().out == ""
    assert repo.settings.kg_incremental_fusion_enabled is True


_DERIVED_STEPS = (
    ("rebuild_canonical_relations", "聚合关联"),
    ("rebuild_mention_bridge", "扫描共同提及"),
    ("_refresh_viz_after_rebuild", "更新图谱预览"),
    ("maybe_auto_index", "检查检索索引"),
    ("rebuild_communities", "划分主题板块与分析"),
)


def _derived_service(timeline, failing=None):
    methods = {}
    for name, _label in _DERIVED_STEPS:
        def action(notebook_id, *, _name=name, **kwargs):
            assert notebook_id == "test"
            if _name in {"rebuild_canonical_relations", "rebuild_mention_bridge",
                          "rebuild_communities"}:
                assert kwargs["force"] is True
            timeline.append(("action", _name))
            if _name == failing:
                raise RuntimeError("failed")
        methods[name] = action
    return SimpleNamespace(
        event_log=SimpleNamespace(emit=Mock(), logger=SimpleNamespace(exception=Mock())),
        scale_artifacts=SimpleNamespace(maybe_auto_index=methods.pop("maybe_auto_index")),
        **methods,
    )


def test_derived_tail_reports_before_work_and_after_completion():
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    timeline = []
    service = _derived_service(timeline)

    def progress(message, done, total):
        assert (done, total) == (0, 0)
        timeline.append(("progress", message))

    KnowledgeLifecycleService._unified_rebuild_derived_tail(service, "test", progress=progress)
    assert len(timeline) == 3 * len(_DERIVED_STEPS)
    for index, (name, label) in enumerate(_DERIVED_STEPS):
        before, action, after = timeline[index * 3:index * 3 + 3]
        assert before == ("progress", label + "…")
        assert action == ("action", name)
        assert after[0] == "progress"
        assert after[1].startswith(label + "完成 (")
        assert after[1].endswith("s)")


def test_derived_tail_observer_failure_cannot_prevent_work():
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    timeline = []

    def broken(*_args):
        raise RuntimeError("observer unavailable")

    KnowledgeLifecycleService._unified_rebuild_derived_tail(
        _derived_service(timeline), "test", progress=broken,
    )
    assert timeline == [("action", name) for name, _ in _DERIVED_STEPS]


@pytest.mark.parametrize("failing,event", [
    ("rebuild_canonical_relations", "canonical_relations_rebuild_failed"),
    ("rebuild_mention_bridge", "mention_bridge_rebuild_failed"),
    ("maybe_auto_index", None),
    ("rebuild_communities", "communities_rebuild_failed"),
])
def test_derived_tail_failure_remains_isolated_and_never_reports_success(failing, event):
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    timeline = []
    messages = []
    service = _derived_service(timeline, failing=failing)
    KnowledgeLifecycleService._unified_rebuild_derived_tail(
        service, "test", progress=lambda message, *_: messages.append(message),
    )
    assert timeline == [("action", name) for name, _ in _DERIVED_STEPS]
    failed_label = dict(_DERIVED_STEPS)[failing]
    assert failed_label + "…" in messages
    assert not any(message.startswith(failed_label + "完成") for message in messages)
    if event is None:
        service.event_log.logger.exception.assert_called_once()
    else:
        service.event_log.emit.assert_called_once()
        assert service.event_log.emit.call_args.args[0]["kind"] == event
