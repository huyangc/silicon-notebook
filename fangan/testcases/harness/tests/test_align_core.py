from harness import align


def span(line, c0, c1, f="x.md"):
    return {"file": f, "line_start": line, "line_end": line, "char_start": c0, "char_end": c1}


def test_iou_identical():
    assert align.span_iou(span(1, 0, 100), span(1, 0, 100)) == 1.0


def test_iou_disjoint():
    assert align.span_iou(span(1, 0, 50), span(1, 60, 100)) == 0.0


def test_iou_half_overlap():
    # [0,100) vs [50,150): overlap 50, union 150
    assert abs(align.span_iou(span(1, 0, 100), span(1, 50, 150)) - (50 / 150)) < 1e-6


def test_iou_different_file_is_zero():
    assert align.span_iou(span(1, 0, 100, "a.md"), span(1, 0, 100, "b.md")) == 0.0


def test_iou_different_line_low():
    # different lines => intervals far apart in encoded space => 0 overlap
    assert align.span_iou(span(1, 0, 100), span(9, 0, 100)) == 0.0


def test_greedy_match_picks_best():
    scores = {("g1", "p1"): 0.9, ("g1", "p2"): 0.2, ("g2", "p1"): 0.1, ("g2", "p2"): 0.8}
    al = align.greedy(["g1", "g2"], ["p1", "p2"], scores, thresh=0.5)
    assert al["g2p"] == {"g1": "p1", "g2": "p2"}
    assert al["unmatched_gold"] == [] and al["unmatched_pred"] == []


def test_greedy_threshold_drops_low():
    scores = {("g1", "p1"): 0.3}
    al = align.greedy(["g1"], ["p1"], scores, thresh=0.5)
    assert al["g2p"] == {}
    assert al["unmatched_gold"] == ["g1"] and al["unmatched_pred"] == ["p1"]
