"""reflect v2 开闸前 T0 的 rig 题集(设计规格 2026-09-08 §5)。

只有一个 JSON 数据文件和读它的两行代码:题集是**数据**,不是逻辑。语料是公开
论文,题面里没有任何私有内容;主库的 notebook id 刻意不在这里(rig 用
`--source-notebook` 显式传)。
"""
from __future__ import annotations

import json
from pathlib import Path

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"


def load_questions() -> dict:
    """读题集。返回原始 dict,不做任何加工——加工是 rig 的事。"""
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
