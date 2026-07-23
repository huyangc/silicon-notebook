"""向量的内容寻址缓存装饰器。

per-text 而非 per-batch：否则批次边界一变（embed_batch_size 调整、上游 chunk
数量变化）就全部 miss。

缓存的是后端返回的原始维度向量。4096→1024 的运行时截断发生在消费侧（原向量作为
真相源保留不改写），因此本层与维度决策相互独立。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.core.cache import (
    embed_key,
    embedding_all_finite,
    embedding_batch_dim,
    is_cacheable_embedding,
)


class CachedEmbedder:
    def __init__(self, inner: Any, backend: Any, *, model: str,
                 truncate_chars: int, base_url: str) -> None:
        self._inner = inner
        self._backend = backend
        self._model = model
        self._truncate_chars = truncate_chars
        # 服务身份，进缓存键。per-user 模型配置下同 model 名可指向不同 endpoint，
        # 而缓存跨用户全局共享——只认 model 名会让第二个用户拿到第一个 endpoint 的
        # 向量。base_url 足以隔离且非秘密；api_key 绝不入键（本层也不持有它）。
        # 关键字必填、无默认：唯一生产构造点 make_embedder 必传真实 endpoint，
        # 让"忘了传 → 不同服务静默共用一条缓存"在签名层就不可能发生。
        self._base_url = base_url

    def __getattr__(self, name: str) -> Any:
        # dim / model_status 身份绑定 / source_embedding 预热用的 _ensure 等一律透传。
        #
        # 只有 __dict__ 里找不到 name 时才会走到这里。此时若 _inner 也不在 __dict__
        # （unpickle、copy 出的空壳、__init__ 中途抛异常），`self._inner` 会再次落回
        # 本方法 → 无限递归 RecursionError。直接问实例字典，缺了就如实报 AttributeError。
        #
        # 刻意**不**按 name.startswith("_") 一刀切拒绝私有名：source_embedding._warm_up
        # 靠 getattr(embedder, "_ensure", None) 预建 HTTP 客户端来规避多线程首次触碰的
        # 懒初始化竞态，一刀切会让预热静默退化成 no-op（那里的 getattr 带默认值，不报错）。
        try:
            inner = object.__getattribute__(self, "_inner")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(inner, name)

    def _key(self, text: str) -> str:
        # 必须对截断后的文本取键——后端内部同样只发送截断后的内容。
        # base_url 一并入键：见 __init__，隔离不同 endpoint 的同名模型。
        return embed_key(self._model, text[:self._truncate_chars], self._base_url)

    def _expected_dim(self) -> int:
        """本 embedder 声明的**原生输出维度**——缓存里存的就是这个维度的原始 API 向量
        （4096→1024 的运行时截断在消费侧、绝不在本层，见模块 docstring；inner 直接
        返回后端原维向量，未截断）。取 inner.dim：DashscopeEmbedder 构造时置
        `self.dim = settings.embed_dim`，语义即「存储/模型原生维」，是这条服务向量该
        有的维度的权威来源（比在本层另读 settings 更贴近「这个 inner 会产出多少维」）。

        期望值只用于「命中/写入维度必须等于原生维」这道门（round-8 P2-1）：
        embedding_batch_dim 只查**批内一致**，provider 偶发返回一批内部一致却错维
        （如全 2 维）的向量能过它，写进去就静默零召回、且服务恢复也修不好（命中即不
        再打后端）。要求维度 == 原生维即挡住这类「批内一致但整体错维」。

        拿不到或非正整数（unpickle 空壳、无 dim 的测试双桩、误配）→ 返回 0：下面两处
        用 `if native and ...` 守卫，native==0 时这道门整体让开，退化为「只有既有的批内
        一致门」的旧行为——绝不因取不到维度而拒绝所有命中拖垮主流程。getattr 带默认、
        不抛异常，与「缓存故障永不影响主流程」一致。"""
        dim = getattr(self._inner, "dim", 0)
        return dim if isinstance(dim, int) and dim > 0 else 0

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        expected_dim = self._expected_dim()  # 原生维；0=取不到，下面的门整体让开
        cached: Dict[str, List[float]] = {}
        for key in set(keys):
            try:
                raw = self._backend.get(key)
                # 解码也在 try 之内：坏条目（非 JSON、不是数组）同样只能退化成
                # miss。放在 try 之外的话，一条损坏的缓存行会把异常抛进主流程。
                vec = _decode(raw) if raw is not None else None
                # 命中侧维度门（round-8 P2-1）：存的是原始 API 维向量，命中一条维度
                # 不等于原生维的（provider 曾抖动写入的错维毒、或跨配置残留）→ 当 miss
                # 重取，绝不把错维向量喂给下游（截断/相似度会静默零召回）。native==0
                # 时让开这道门（保持旧行为）。
                if vec is not None and expected_dim and len(vec) != expected_dim:
                    vec = None
                # 命中侧有限性门：写入门（is_cacheable_embedding）现在挡住新的
                # NaN/inf 写入，但**此前已固化**的坏向量仍在缓存里——命中就不再打
                # 后端，会把 NaN 一路喂进相似度计算。历史毒值当 miss 重取。放在 try
                # 内：任何异常都退化为 miss，绝不窜进主流程。
                if vec is not None and not embedding_all_finite(vec):
                    vec = None
            except Exception:      # 缓存故障退化为 miss，绝不影响主流程
                vec = None
            if vec is not None:
                cached[key] = vec

        # 未命中的去重后按原序请求：同批重复文本只打一次后端。
        missing: List[str] = []
        missing_keys: List[str] = []
        seen = set()
        for text, key in zip(texts, keys):
            if key in cached or key in seen:
                continue
            seen.add(key)
            missing.append(text)
            missing_keys.append(key)

        if missing:
            vectors = list(self._inner.embed_texts(missing))
            if len(vectors) != len(missing):
                # 长度不符说明后端异常。**必须抛**，不能把这份响应返回出去：它是对
                # missing 子集的应答，而调用方按 texts 的下标消费
                # （embed_in_chunks 的 out[start + offset] = vec），直接返回会把
                # 后面某条文本的向量装到前面那条头上——正是本层要防的张冠李戴。
                # 也重建不出正确形状：无从得知返回的向量分别对应 missing 里的哪几条。
                # 抛出后各调用点的 per-batch except 会把整批记成"没拿到向量"，与
                # 不带缓存时后端返回短列表的降级结果一致，但没有错配。
                raise RuntimeError(
                    f"embedding backend returned {len(vectors)} vectors "
                    f"for {len(missing)} texts"
                )
            # 准入规则与 LLM 侧对称，见 policy.is_cacheable_embedding：条数对得上
            # 但内容退化（`[[]]`）的响应能过上面那道长度门，写进去就是静默零召回，
            # 且服务恢复后也不会再打后端。本批维度先算一次（O(批大小)）。
            batch_dim = embedding_batch_dim(vectors)
            for key, vec in zip(missing_keys, vectors):
                # 本次调用照常返回拿到的向量（缓存准入不改变主流程结果，退化响应的
                # 处置由上游各调用点的既有降级逻辑负责）——只是不落缓存。
                cached[key] = list(vec)
                if not is_cacheable_embedding(vec, batch_dim):
                    continue
                # 写入侧维度门（round-8 P2-1）：批内一致（batch_dim）挡不住「整批内部
                # 一致却错维」（provider 偶发全返 2 维）。要求维度 == 原生维，才不会把
                # 这种错维向量固化 90 天（命中即不再打后端，服务恢复也修不好）。
                # native==0（取不到维度）时让开，退化为只剩批内一致门（旧行为）。
                if expected_dim and len(vec) != expected_dim:
                    continue
                try:
                    self._backend.put(key, _encode(vec), tag=self._model)
                except Exception:  # 写缓存失败同样不影响主流程
                    pass

        return [cached[key] for key in keys]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]


def _encode(vector: Any) -> str:
    # 写侧的 float() 不能省：numpy 标量不是 JSON 可序列化类型。写只发生在 miss 后。
    return json.dumps([float(x) for x in vector])


def _decode(raw: str) -> Any:
    """缓存里的一条值 → 向量；任何不像向量的东西一律 None（当 miss）。"""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    # 刻意不做 [float(x) for x in value]：_encode 写进去的就是 float 字面量，
    # json.loads 出来已经是 float，逐元素转换只是在每次命中（最热的路径）上白跑
    # 一遍全量扫描、再重建一个上千元素的列表。抽查首元素足以挡住"这压根不是向量"
    # 的坏值，代价 O(1)。
    if value and not isinstance(value[0], (int, float)):
        return None
    return value
