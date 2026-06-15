"""查询理解层(chunk 与 reasoning 共用):规整 + LLM 改写/分解。"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

# 在字母↔数字边界插空格,让 "gpt4" 这类连写匹配上语料 "GPT-4"→tokens "gpt","4"。
# 注意:无法把 "deepseekv2" 拆成 "deepseek v2"(中间无边界)——那类靠 expand_query 的
# LLM 改写写出规范名(DeepSeek-V2)。此处只做边界明确的廉价补充(也惠及无 LLM 回退)。
_LD = re.compile(r"(?<=[A-Za-z]{2})(?=\d)|(?<=[A-Za-z])(?=\d{2,})")


def normalize_terms(q: str) -> str:
    return _LD.sub(" ", q or "")
