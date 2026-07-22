"""向量的内容寻址缓存装饰器。

per-text 而非 per-batch：否则批次边界一变（embed_batch_size 调整、上游 chunk
数量变化）就全部 miss。

缓存的是后端返回的原始维度向量。4096→1024 的运行时截断发生在消费侧（原向量作为
真相源保留不改写），因此本层与维度决策相互独立。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.cache import embed_key


class CachedEmbedder:
    def __init__(self, inner: Any, backend: Any, *, model: str,
                 truncate_chars: int) -> None:
        self._inner = inner
        self._backend = backend
        self._model = model
        self._truncate_chars = truncate_chars

    def __getattr__(self, name: str) -> Any:
        # dim / embed_query / model_status 身份绑定等一律透传。
        return getattr(self._inner, name)

    def _key(self, text: str) -> str:
        # 必须对截断后的文本取键——后端内部同样只发送截断后的内容。
        return embed_key(self._model, text[:self._truncate_chars])

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        cached: Dict[str, List[float]] = {}
        for key in set(keys):
            try:
                raw = self._backend.get(key)
            except Exception:      # 缓存故障退化为 miss，绝不影响主流程
                raw = None
            if raw is not None:
                vec = _decode(raw)
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
            vectors = self._inner.embed_texts(missing)
            # 长度不符说明后端异常——不写缓存，也不假装对齐。
            if len(vectors) == len(missing):
                for key, vec in zip(missing_keys, vectors):
                    cached[key] = list(vec)
                    try:
                        self._backend.put(key, _encode(vec), tag=self._model)
                    except Exception:
                        pass
            else:
                return list(vectors)

        return [cached[key] for key in keys]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]


def _encode(vector: Any) -> str:
    import json
    return json.dumps([float(x) for x in vector])


def _decode(raw: str) -> Any:
    import json
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    return [float(x) for x in value]
