from app.services.kg.models import Node, Edge
from app.eval.sa_calibration import (
    compound_claim_rate, sparse_edge_count, validity_scope_fill_rate,
    evaluate_gate,
)


def _claim(name, vs=None):
    return Node(id=name, type="Claim", name=name, validity_scope=vs or {})


def test_compound_claim_rate_flags_real_compounds_only():
    claims = [
        _claim("The gain is high; the bandwidth is low"),                  # ; -> compound
        _claim("The output is the Volterra series and H_m is its kernel"), # and + 'H_m is' -> compound
        _claim("It saturates. It then clips"),                             # 2 sentences -> compound
        _claim("I_D depends on V_GS"),                                     # atomic (no leading and/but)
        _claim("A_2w falls to zero as w->0 and as w->inf"),                # coordinated 'and as' -> atomic
        _claim("Valid for f_1, ..., f_n"),                                 # ellipsis -> atomic
    ]
    assert compound_claim_rate(claims) == 0.5   # 3 of 6 are real compounds


def test_compound_ignores_coordination_and_ellipsis():
    # the false positives the OLD heuristic wrongly flagged must NOT be flagged now
    atomic = [
        _claim("A_{2w1} falls to zero as w1 approaches zero and as w1 goes to infinity"),
        _claim("substituting w1 for w3 and -w2 for w2"),
        _claim("H_m in terms of H_1, ..., H_{m-1} without solving nonlinear equations"),
    ]
    assert compound_claim_rate(atomic) == 0.0
    # genuine clause-level coordination IS still flagged
    assert compound_claim_rate([_claim("they do not appear here and could have been omitted")]) == 1.0


def test_sparse_edge_count():
    edges = [Edge(id="1", type="depends_on", source_id="a", target_id="b"),
             Edge(id="2", type="contrasts_with", source_id="a", target_id="b"),
             Edge(id="3", type="about", source_id="a", target_id="b")]
    assert sparse_edge_count(edges) == 2


def test_validity_scope_fill_rate():
    nodes = [_claim("a", {"region": ["sat"]}), _claim("b"),
             Node(id="f", type="Formula", name="f", validity_scope={"range": "DC"}),
             Node(id="c", type="Concept", name="c")]
    assert round(validity_scope_fill_rate(nodes), 3) == round(2 / 3, 3)


def test_evaluate_gate_relative_compound_passes():
    # numbers re-evaluated offline on the captured A/B claims (improved heuristic)
    old = {"sparse_per_1k_tok": 0.0, "compound_claim_rate": 0.333,
           "validity_scope_fill_rate": 0.0}
    new = {"sparse_per_1k_tok": 0.358, "compound_claim_rate": 0.167,
           "validity_scope_fill_rate": 0.034}
    r = evaluate_gate(old, new)
    assert r["checks"]["compound_reduced_vs_old"] is True   # 0.167 <= 0.8*0.333 and < 0.5
    assert r["checks"]["sparse_density_up_1.5x"] is True
    assert r["checks"]["validity_scope_present"] is True
    assert r["pass"] is True


def test_compound_probe_fps_stay_atomic():
    # Held-out probes (2026-06-11) that exposed the overfitted ruler — frozen so
    # the sample fossils (h-word pattern, bare "and the/that") cannot return.
    atomic = [
        _claim("The capacitance between the gate and the source is C_gs"),
        _claim("The cascode provides low noise and high output resistance"),
        _claim("The mismatch between V_in and the reference causes offset"),
        _claim("The gain of the NMOS and that of the PMOS are matched"),
    ]
    assert compound_claim_rate(atomic) == 0.0


def test_compound_probe_compounds_flagged():
    compounds = [
        _claim("增益提高；带宽下降"),                                   # full-width ；
        _claim("The gain increases while the bandwidth drops"),          # while-clause
        _claim("V_th shifts, which causes the bias point to drift"),     # ", which"
    ]
    assert compound_claim_rate(compounds) == 1.0


def test_compound_known_blind_spot_documented():
    # KNOWN limitation, asserted on purpose: "and the <noun> <verb>" clauses are
    # NOT flagged — dropping bare "the" was the price of fixing the FP probes
    # above. If this assertion ever flips, re-verify those FP probes still pass.
    assert compound_claim_rate([_claim("The gain is high and the bandwidth is low")]) == 0.0


def test_evaluate_gate_fails_when_compound_not_reduced():
    old = {"sparse_per_1k_tok": 1.0, "compound_claim_rate": 0.30,
           "validity_scope_fill_rate": 0.1}
    new = {"sparse_per_1k_tok": 2.0, "compound_claim_rate": 0.29,   # > 0.8*0.30=0.24 -> not reduced
           "validity_scope_fill_rate": 0.1}
    r = evaluate_gate(old, new)
    assert r["checks"]["compound_reduced_vs_old"] is False
    assert r["pass"] is False
