"""缓存策略：key 怎么算、什么不该缓存。与存储实现分离——换 backend 不碰本文件。"""
from __future__ import annotations

import hashlib
import json
import math
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


def embedding_all_finite(vector: Sequence[float]) -> bool:
    """向量每个分量都是**有限数值**（非 NaN/±inf、且确实是数）。

    `math.isfinite` 对 numpy 标量也经 `__float__` 生效；碰到根本不是数的元素
    （None/str/…）会抛 TypeError，这里一并按「不是有限向量」处理并返回 False——
    绝不把异常抛给调用方（准入判定不在 try 里，异常会窜进主流程，违反「缓存故障
    永不影响主流程」）。生成器写法遇到第一个坏值就短路。
    """
    try:
        return all(math.isfinite(x) for x in vector)
    except (TypeError, ValueError):
        return False


def is_cacheable_embedding(vector: Sequence[float], expected_dim: int) -> bool:
    """这一条向量值不值得写进缓存。

    退化响应的真实形态是**条数正确、内容为空**（`[[]]`）：长度对得上，上游
    「返回条数与输入不符 → raise」那道门放它过，于是一次服务抖动被固化 90 天。
    后果比 LLM 侧更隐蔽——零向量在检索层不报错，只是静默零召回；而且**服务恢复
    也修不好**：命中缓存就不会再打后端，只能手动删缓存文件，而没人知道要去删。

    三道门：

    1. **空向量**：`[]`（或任何 len 0）一律不写。这是实测到的退化形态。
    2. **与本批公共维度不符**：`expected_dim` 由 embedding_batch_dim 给出，
       批内维度不一致时它是 0，于是整批都写不进去。
    3. **含非有限值**：尺寸正确却带 `NaN`/`inf` 的向量（provider 偶发返回）——长度、
       维度两道门都过得去，写进去会被 `_encode` 序列化、重放整个 TTL，把 NaN 灌进
       相似度计算（污染排序/阈值）。要求每个分量都是有限数值（embedding_all_finite）。

    expected_dim 是**必填位置参数**，不给默认值：留了默认值，调用方漏传就会静默
    退化成只剩第 1 道门，而那正是上一轮评审抓到的缺陷形状。
    """
    n = len(vector)
    if n == 0:
        return False
    if n != expected_dim:
        return False
    return embedding_all_finite(vector)


def llm_key(
    model: str,
    messages: List[Dict[str, str]],
    schema_hint: str,
    base_url: str,
    *,
    temperature: float = 1.0,
    top_p: float = 1.0,
    max_tokens: Optional[int] = None,
    thinking_mode: Optional[str] = None,
) -> str:
    """LLM 响应的缓存键 = sha256(base_url + model + messages 全文 + schema_hint +
    有效生成参数 temperature/top_p/max_tokens/显式 thinking mode)。

    prompt 全文进 key 意味着改 prompt 即自动全冷，不需要维护版本号。

    **生成参数进 key（本轮修复）**：`chat_json` 的 temperature/top_p/max_tokens 是
    per-call 的（抽取用高 token 预算、答案合成用另一档）。相同 messages + schema 但
    不同采样设置、或不同 token 预算是**语义不同的 upstream 请求**，绝不能共用一条
    缓存。max_tokens 尤其关键：本仓库文档化的截断补救手段就是调大
    `KG_EXTRACT_MAX_TOKENS` 重跑——若它不在 key 里，调大后仍会命中被截断的旧响应。
    这与「截断响应不入缓存」（is_cacheable_llm_response）互为**两半**：那条防坏响应
    被写入，这条防参数变了还命中旧响应。

    ⚠ **必须传解析后的有效值，而非原始入参**：`chat_json` 里 max_tokens=None 会回落
    到 `settings.openai_compat_max_tokens`。调用方须先解析出实际生效的预算再传进来，
    否则 `max_tokens=None` 与显式传入等价默认值会算出两条不同 key（虚假 miss，白白
    多打一次模型）。temperature/top_p 无 None 回落，直接用 chat_json 的入参即可——
    默认值 1.0/1.0 正是 chat_json 的采样默认（DeepSeek-V4 官方推荐），因此这三个给
    默认值是「有自然默认」的合理选择，与 base_url「无自然默认→必填」的取舍不同；
    产品侧唯一构造点（chat_json）总是显式传入解析后的有效值。

    ``base_url`` 是**服务身份**，不是可选项：本仓库有 per-user 模型配置，两个用户
    可以配同一个 model 名却指向不同 endpoint。缓存是跨用户全局共享的，若 key 只认
    model 名，第二个用户会拿到第一个 endpoint 的响应——他自己选的模型服务根本没被
    调用。base_url 足以区分 endpoint（正是这个场景），且它不是秘密（出现在日志、
    错误信息里）。**绝不把 api_key 放进 key 材料**：key 会经 stats()/日志/调试路径
    间接暴露；同 endpoint 换 key 由 evict_tag(model) 逃生口兜底，无需入 key。所以
    这里刻意只取 base_url，不用含 api_key 的 model_config_fingerprint()。

    base_url 位置参数、无默认值：漏传就是上一轮评审抓到的那类缺陷（不同服务静默
    共用一条缓存），让调用方在签名层就无法忘记，与 is_cacheable_embedding 的
    expected_dim 同一防呆思路。
    """
    material = {
        "base_url": base_url,
        "model": model,
        "messages": messages,
        "schema": schema_hint,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    # Preserve every provider-default cache key byte-for-byte.  An explicit
    # thinking mode is a different upstream request, however, and must never
    # reuse a response generated under the provider default (DeepSeek V4's
    # default is thinking-enabled).  Conditional inclusion avoids a global
    # cache cold-start for workloads whose request shape did not change.
    if thinking_mode is not None:
        material["thinking_mode"] = thinking_mode
    payload = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embed_key(model: str, truncated_text: str, base_url: str) -> str:
    """单条文本的向量缓存键 = sha256(base_url + model + 截断后文本)。

    传入的必须是**截断后**的文本（DashscopeEmbedder 内部先做
    `t[:embed_truncate_chars]` 才发 API）。用原文取哈希会让两个前 N 字符相同的
    长文本各占一条缓存却拿到完全相同的向量，白白损失命中率；对截断后文本取哈希
    还顺带捕获了 embed_truncate_chars 的配置变更。

    ``base_url`` 是服务身份，纳入 key 的原因与 llm_key 完全相同（per-user 模型配置
    下同 model 名可指向不同 endpoint，全局共享缓存不能张冠李戴）。同样**绝不含
    api_key**：embedder 层本就不持有它，base_url 已足以隔离 endpoint。位置参数、
    无默认值，理由同上。
    """
    payload = json.dumps({"base_url": base_url, "model": model, "text": truncated_text},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
