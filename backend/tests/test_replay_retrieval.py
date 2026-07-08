"""回放对照的 compare 纯函数:exact 逐位比较与 topk 集合重叠。"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "replay_retrieval",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "replay_retrieval.py")
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def _rec(ids_scores):
    return {"kg": [{"id": i, "relevance": s} for i, s in ids_scores],
            "ppr_chunks": [{"id": i, "relevance": s} for i, s in ids_scores]}


def test_compare_exact_pass():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["q1"]["kg"]["pass"] is True and rep["_summary"]["all_pass"] is True


def test_compare_exact_fail_on_reorder():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("y", 0.5), ("x", 0.9)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["_summary"]["all_pass"] is False


def test_compare_topk_overlap():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5), ("z", 0.1)])}
    b = {"q1": _rec([("x", 0.8), ("z", 0.6), ("y", 0.2)])}
    rep = replay.compare_runs(a, b, mode="topk", k=2)
    # top-2: {x,y} vs {x,z} → overlap 0.5
    assert abs(rep["q1"]["kg"]["overlap"] - 0.5) < 1e-9
