from app.eval.retrieval_metrics import recall_at_k, mrr, run_recall


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c"], ["b", "z"], k=3) == 0.5   # 1 of 2 gold in top-3
    assert recall_at_k(["a", "b", "c"], ["a", "b"], k=1) == 0.5   # only top-1 counts
    assert recall_at_k(["a"], [], k=3) is None                    # no gold -> undefined


def test_mrr():
    assert mrr(["a", "b", "c"], ["b"]) == 0.5        # first gold at rank 2
    assert mrr(["a", "b"], ["a"]) == 1.0             # rank 1
    assert mrr(["a", "b"], ["z"]) == 0.0             # no gold retrieved


class _FakeHit:
    def __init__(self, oid):
        self.object_id = oid


class _FakeRepo:
    def _retrieve_scored(self, nb, q, **k):
        return [_FakeHit("o2"), _FakeHit("o1"), _FakeHit("o3")]


def test_run_recall_skips_unannotated_and_scores_annotated():
    questions = [
        {"id": "q1", "question": "x", "gold_object_ids": ["o1"]},
        {"id": "q2", "question": "y"},                       # no gold -> skipped
    ]
    rows = run_recall(_FakeRepo(), "nb", questions, k=3)
    assert len(rows) == 1
    assert rows[0]["id"] == "q1"
    assert rows[0]["recall_at_k"] == 1.0                     # o1 present in top-3
    assert rows[0]["mrr"] == 0.5                             # o1 at rank 2
