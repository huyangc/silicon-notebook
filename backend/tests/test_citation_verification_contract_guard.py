"""check_citation_verification_contract.py 的正向/负向测试。

同 test_enumeration_list_labels_guard.py 的做法:不重写守卫的解析,把真实前端文件复制
到 tmp_path 再做一处漂移,交给守卫的**真实函数**去判——测试自己重写解析器只能证明那份
复制品会失败,证明不了硬门会失败。
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "citation_verification_guard",
    ROOT / "scripts" / "check_citation_verification_contract.py",
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

SOURCE = (ROOT / "frontend" / "app" / "citation-verification.ts").read_text(encoding="utf-8")


def _drifted(tmp_path, old, new):
    assert old in SOURCE, old
    path = tmp_path / "citation-verification.ts"
    path.write_text(SOURCE.replace(old, new, 1), encoding="utf-8")
    return path


def test_the_real_frontend_file_matches_the_backend():
    assert guard.check() == []
    assert guard.main() == 0


@pytest.mark.parametrize("old,new", [
    ('changed: "原文已改动"', 'changed: "原文有改动"'),
    ('"本次回答有部分引用未通过核对"', '"本次回答有引用未通过核对"'),
    ('"回答生成时，有部分引用未通过核对"', '"回答生成时,有部分引用未通过核对"'),
    ("。回答内容照常保留，带标记的引用可点开查看原因。`", "。回答内容照常保留。`"),
    ('.join("、")', '.join("，")'),
    ("`共 ${count(check.failed)} 条`", "`${count(check.failed)} 条`"),
    ('["changed", "source_gone", "unverifiable"]', '["source_gone", "changed", "unverifiable"]'),
], ids=["label", "live-lead", "snapshot-lead", "tail", "joiner", "fallback", "order"])
def test_a_one_character_drift_fails_the_guard(tmp_path, old, new):
    path = _drifted(tmp_path, old, new)
    assert guard.check(path)
    assert guard.main(path) == 1


def test_a_stale_copy_hidden_in_a_comment_does_not_count(tmp_path):
    """Comments are stripped: the real declaration must carry the wording."""
    path = _drifted(
        tmp_path, 'changed: "原文已改动"',
        '/* changed: "原文已改动" */ changed: "原文变了"',
    )
    assert guard.check(path)
