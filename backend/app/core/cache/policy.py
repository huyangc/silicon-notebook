"""缓存策略：key 怎么算、什么不该缓存。与存储实现分离——换 backend 不碰本文件。"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence


def is_cacheable_llm_response(content: str, finish_reason: Optional[str]) -> bool:
    """这条 LLM 响应值不值得写进缓存。写错一次就固化整个 TTL（默认 90 天）。

    三道门，任一不过就不写：

    1. **空回退 `"{}"`**：chat_json 在拿不到内容时的兜底。缓存它等于把一次偶发
       退化永久固化在这条 prompt 上。
    2. **`json.loads` 解析不了**：被截断的 JSON（`{"objects": [{"name": "a`）
       经 strip_json_fences 后仍是非空串，能过第 1 道门，但上游 safe_json 解析
       不出来 → 该抽取窗口静默产出 0 个节点。缓存它等于把这个 0 固化整个 TTL；
       更要命的是 max_tokens **不在缓存键里**，本项目文档化的截断补救手段（调大
       KG_EXTRACT_MAX_TOKENS 重跑）会直接命中这条垃圾缓存而彻底失效。
    3. **`finish_reason == "length"`**：内容碰巧仍是合法 JSON、但已被输出预算
       截断的罕见情形（模型正好在闭合括号处被切）。语义上仍是残缺答案，同样
       不缓存——它骗得过 json.loads，骗不过 finish_reason。

    第 2、3 道门互为补充，缺一不可：截断通常表现为解析失败，但不总是；而
    finish_reason 在某些 OpenAI 兼容实现上可能缺失（取到 None），此时靠解析兜底。
    每次成功调用多一次 json.loads，相对一次网络往返可忽略。
    """
    if not content or content == "{}":
        return False
    if (finish_reason or "").strip().lower() == "length":
        return False
    try:
        json.loads(content)
    except Exception:
        return False
    return True


def embedding_batch_dim(vectors: Sequence[Sequence[float]]) -> int:
    """这一份 embedding 响应的**公共维度**；不可信时返回 0。

    真实 embedding 服务对同一 model 恒返回定长向量，所以「同一次响应里出现了两种
    长度」本身就是响应退化的信号：无从判断哪一种才是对的，只能整份作废。返回 0
    让 is_cacheable_embedding 对该批每一条都判否——0 不可能等于任何合法维度，也
    不可能被空向量（len 0）蒙混过关，因为那里另有一道非空门。

    与 LLM 侧 is_cacheable_llm_response 对称：宁可多打一次后端，也不把一份可疑
    响应固化整个 TTL（默认 90 天）。
    """
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        return 0
    return dims.pop()


def is_cacheable_embedding(vector: Sequence[float], expected_dim: int) -> bool:
    """这一条向量值不值得写进缓存。

    退化响应的真实形态是**条数正确、内容为空**（`[[]]`）：长度对得上，上游
    「返回条数与输入不符 → raise」那道门放它过，于是一次服务抖动被固化 90 天。
    后果比 LLM 侧更隐蔽——零向量在检索层不报错，只是静默零召回；而且**服务恢复
    也修不好**：命中缓存就不会再打后端，只能手动删缓存文件，而没人知道要去删。

    两道门：

    1. **空向量**：`[]`（或任何 len 0）一律不写。这是实测到的退化形态。
    2. **与本批公共维度不符**：`expected_dim` 由 embedding_batch_dim 给出，
       批内维度不一致时它是 0，于是整批都写不进去。

    expected_dim 是**必填位置参数**，不给默认值：留了默认值，调用方漏传就会静默
    退化成只剩第 1 道门，而那正是本次评审抓到的缺陷形状。
    """
    n = len(vector)
    if n == 0:
        return False
    return n == expected_dim


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
