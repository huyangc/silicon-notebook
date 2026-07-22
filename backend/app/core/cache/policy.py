"""缓存策略：key 怎么算、什么不该缓存。与存储实现分离——换 backend 不碰本文件。"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List


def llm_key(model: str, messages: List[Dict[str, str]], schema_hint: str) -> str:
    """LLM 响应的缓存键 = sha256(model + messages 全文 + schema_hint)。

    prompt 全文进 key 意味着改 prompt 即自动全冷，不需要维护版本号。
    temperature 是 chat_json 的固定常量，刻意排除；若将来加了 per-call
    temperature，必须并入 key。

    注意：本函数的输出必须与历史实现逐字节一致，否则已有缓存全部失效。
    """
    payload = json.dumps(
        {"model": model, "messages": messages, "schema": schema_hint},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embed_key(model: str, truncated_text: str) -> str:
    """单条文本的向量缓存键。

    传入的必须是**截断后**的文本（DashscopeEmbedder 内部先做
    `t[:embed_truncate_chars]` 才发 API）。用原文取哈希会让两个前 N 字符相同的
    长文本各占一条缓存却拿到完全相同的向量，白白损失命中率；对截断后文本取哈希
    还顺带捕获了 embed_truncate_chars 的配置变更。
    """
    payload = json.dumps({"model": model, "text": truncated_text},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
