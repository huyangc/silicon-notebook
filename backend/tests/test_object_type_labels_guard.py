"""check_object_type_labels_contract.py 的负向/正向测试。

review 揪出:守卫正则会把「// concept: "…"」这种被注释掉的条目也抓进来,让守卫被注释
骗过。这里证明 strip_ts_comments 修掉了这个洞,并锁定 live 前后端一致。
"""
import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "otl_guard", ROOT / "scripts" / "check_object_type_labels_contract.py"
)
otl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otl)


def _pairs(body: str) -> dict:
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', body))


def test_strip_removes_line_and_block_comments():
    body = """
      concept: "概念 Concept",
      // claim: "论断 Claim",
      /* formula: "公式 Formula", */
      procedure: "过程 Procedure",
    """
    # 剥注释后:只剩真实的两条
    assert _pairs(otl.strip_ts_comments(body)) == {
        "concept": "概念 Concept",
        "procedure": "过程 Procedure",
    }
    # 不剥注释(旧行为)会误抓被注释的 claim/formula —— 证明这个洞真实存在
    raw = _pairs(body)
    assert "claim" in raw and "formula" in raw


def test_commented_out_entry_makes_guard_detect_mismatch():
    """注释掉一个真实条目 → 前端少一个 key → 与后端不等(守卫应失败)。"""
    full = 'concept: "x", claim: "y", formula: "z", procedure: "w"'
    commented = 'concept: "x", /* claim: "y", */ formula: "z", procedure: "w"'
    assert _pairs(otl.strip_ts_comments(full)) != _pairs(otl.strip_ts_comments(commented))
    assert "claim" not in _pairs(otl.strip_ts_comments(commented))


def test_live_frontend_labels_match_backend():
    assert otl.frontend_labels() == dict(otl.OBJECT_TYPE_LABELS)
