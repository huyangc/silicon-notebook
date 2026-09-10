"""reflect v2 开闸前 T0 的 rig 题集(设计规格 2026-09-08 §5)。

两份 JSON 数据文件、两个路径常量:题集是**数据**,不是逻辑。`questions.json`
是 34 题的 A/B 语料题集(`load_questions()` 原样读出,不做任何加工);
`state_probes.json` 是 E2(固定状态真实决策,T-EX6)的 12 例自包含 case 集,
守卫在 `backend/tests/test_reflect_state_probe.py`。语料是公开论文,题面里没有
任何私有内容;主库的 notebook id 刻意不在这里(rig 用 `--source-notebook`
显式传)。
"""
from __future__ import annotations

import json
from pathlib import Path

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"
#: E2(固定状态真实决策,T-EX6)的 12 例自包含 case 集,与题集同目录、同一个包。
STATE_PROBES_PATH = QUESTIONS_PATH.parent / "state_probes.json"


def load_questions() -> dict:
    """读题集。返回原始 dict,不做任何加工——加工是 rig 的事。"""
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
