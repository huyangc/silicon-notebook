"""qwen3-rerank(DashScope text-rerank)。单次批量调用;候选超 max_docs 自动切 batch
线程池并发 + 按 relevance_score 合并。失败/未配置 → 原序下标(降级)。"""
from __future__ import annotations
import concurrent.futures as _cf
import logging
from typing import List
import requests

logger = logging.getLogger("silicon_notebook.rerank")


class RerankClient:
    def __init__(self, settings):
        self.settings = settings
        self.model = (getattr(settings, "rerank_model", "") or "").strip()
        self.base_url = (getattr(settings, "rerank_base_url", "") or "").rstrip("/")
        self.api_key = getattr(settings, "rerank_api_key", "") or ""
        self.max_docs = max(1, getattr(settings, "rerank_max_docs", 500))

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)

    def rerank(self, query: str, documents: List[str], on_error=None) -> List[int]:
        if not self.configured or not documents:
            return list(range(len(documents)))
        try:
            scored = (self._rerank_batch(query, documents) if len(documents) <= self.max_docs
                      else self._rerank_split(query, documents))
            order, seen = [], set()
            for r in sorted(scored, key=lambda r: r["relevance_score"], reverse=True):
                i = r["index"]
                if 0 <= i < len(documents) and i not in seen:
                    seen.add(i); order.append(i)
            order += [i for i in range(len(documents)) if i not in seen]
            return order
        except Exception as exc:
            logger.warning("rerank failed, fallback to identity: %s", exc)
            if on_error is not None:
                on_error(exc)
            return list(range(len(documents)))

    def _rerank_batch(self, query: str, documents: List[str]) -> List[dict]:
        # DashScope text-rerank(原生):POST {base}/services/rerank/text-rerank/text-rerank,
        # body {model, input:{query,documents}, parameters};结果在 output.results[].{index,relevance_score}。
        # 注:DashScope 无 OpenAI-compatible /reranks 端点(compatible-mode 下 404),故走原生服务路径。
        resp = requests.post(
            f"{self.base_url}/services/rerank/text-rerank/text-rerank",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model,
                  "input": {"query": query, "documents": documents},
                  "parameters": {"return_documents": False, "top_n": len(documents)}},
            timeout=getattr(self.settings, "openai_compat_timeout_seconds", 30))
        resp.raise_for_status()
        return resp.json()["output"]["results"]

    def _rerank_split(self, query: str, documents: List[str]) -> List[dict]:
        batches = [(i, documents[i:i + self.max_docs]) for i in range(0, len(documents), self.max_docs)]
        workers = max(1, min(getattr(self.settings, "embed_concurrency", 8), len(batches)))
        out: List[dict] = []
        def one(item):
            base, docs = item
            return [{"index": base + r["index"], "relevance_score": r["relevance_score"]}
                    for r in self._rerank_batch(query, docs)]
        with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(one, batches):
                out.extend(part)
        return out
