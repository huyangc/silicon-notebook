from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
import json


def test_answer_prompt_states_marker_and_inference_rules():
    p = answer_prompt("q?", "k1: [concept] Engram — def: ...")
    assert "[k1]" in p or "[k_i]" in p              # marker convention present
    assert "推断" in p or "inference" in p.lower()   # inference must be self-labelled
    assert "k1: [concept] Engram" in p               # context block embedded
    assert "answer" in ANSWER_SCHEMA_HINT and "grounded" in ANSWER_SCHEMA_HINT


def test_answer_prompt_includes_history_when_present():
    from app.services.prompts import answer_prompt
    p = answer_prompt("follow up?", "k1: [concept] X", history_block="User: prev q\nAssistant: prev a")
    assert "prev q" in p and "prev a" in p
    p2 = answer_prompt("q?", "k1: [concept] X")   # default no history
    assert "prev q" not in p2


def test_answer_prompt_forbids_fabricated_citation():
    from app.services.prompts import answer_prompt
    p = answer_prompt("q?", "k1: [concept] X")
    assert "DIRECTLY from that specific knowledge item" in p
    assert "MUST NOT contain any [k]" in p
    assert "NEVER attach [k]" in p
    # 原有推断标注规则仍在
    assert "推断" in p


def test_extract_prompt_excludes_enumerated_values_and_meta_claims():
    from app.services.kg.extract import _prompt
    p = _prompt("[1] sample text", "Section 1", "textbook")
    # concept:取值枚举不独立成节点
    assert "enumerated settings" in p and "Do NOT emit Concepts" in p
    # claim:不抽标题/前言/元叙述
    assert "stands alone as truth-evaluable" in p
    assert "section headings" in p
    assert "narrative/meta sentences about the document" in p
