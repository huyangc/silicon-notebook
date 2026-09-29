"""qwen3-rerank(DashScope text-rerank)。单次批量调用;候选超 max_docs 自动切 batch
线程池并发 + 按 relevance_score 合并。失败/未配置 → 原序下标(降级)。"""
from __future__ import annotations
import logging
from typing import List
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("silicon_notebook.rerank")


_OPENAI_RERANK_STYLES = frozenset({"openai", "vllm", "cohere", "compatible"})


def normalize_rerank_api_style(value: object) -> str:
    normalized = str(value or "dashscope").strip().lower()
    return "openai" if normalized in _OPENAI_RERANK_STYLES else "dashscope"


class RerankClient:
    # OpenAI-compatible /rerank styles (vLLM, Cohere, etc. serve this shape:
    # flat body, top-level results). Canonical config value is "openai".
    _OPENAI_STYLES = frozenset({"openai", "vllm", "cohere", "compatible"})

    def __init__(self, settings, *, model="", base_url="", api_key="", max_docs=None, api_style="dashscope", max_connections=None):
        self.settings = settings
        self.model = (model or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.max_docs = max(1, max_docs if max_docs is not None else getattr(settings, "rerank_max_docs", 500))
        self.api_style = normalize_rerank_api_style(api_style)
        self._session = None
        if max_connections is not None:
            maximum = max(1, int(max_connections))
            self._session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=maximum,
                pool_maxsize=maximum,
                max_retries=0,
                pool_block=False,
            )
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)

    def rerank(self, query: str, documents: List[str], on_error=None, *,
               cancel_event=None, timeout=None) -> List[int]:
        # 形参与 ``RerankClientPort`` 逐一相同(``test_model_client_ports_match_
        # concrete_call_signatures`` 钉住)。这是**不经调度器**的直连入口:前面
        # 既没有队列也没有共享熔断器,调用方预算只能约束这一次阻塞的 HTTP
        # 请求,所以在这里下传为请求超时。经调度器的 ``ScheduledRerankClient``
        # 刻意**不**这么做(调用方私有预算不能把共享服务判成故障、打开熔断器),
        # 它只调 ``_rerank_batch(query, docs)``。已取消的信号在发请求前照抛。
        if not self.configured or not documents:
            return list(range(len(documents)))
        if cancel_event is not None and cancel_event.is_set():
            from app.services.cancellation import AskCancelled
            raise AskCancelled()
        try:
            scored = (self._rerank_batch(query, documents) if timeout is None
                      else self._rerank_batch(query, documents, timeout=timeout))
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

    def _rerank_batch(self, query: str, documents: List[str],
                      timeout: float | None = None) -> List[dict]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if timeout is None or float(timeout) <= 0:
            timeout = getattr(self.settings, "openai_compat_timeout_seconds", 30)
        if self.api_style in self._OPENAI_STYLES:
            # OpenAI 兼容(vLLM/Cohere 等):POST {base}/rerank,扁平 body,结果在顶层
            # results[].{index, relevance_score|score}。/v1 与否由 base_url 决定。
            post = self._session.post if self._session is not None else requests.post
            resp = post(
                f"{self.base_url}/rerank", headers=headers,
                json={"model": self.model, "query": query, "documents": documents},
                timeout=timeout)
            resp.raise_for_status()
            return [{"index": r["index"],
                     "relevance_score": r.get("relevance_score", r.get("score", 0.0))}
                    for r in resp.json().get("results", [])]
        # DashScope text-rerank(原生,默认):POST {base}/services/rerank/text-rerank/text-rerank,
        # body {model, input:{query,documents}, parameters};结果在 output.results[].{index,relevance_score}。
        # 注:DashScope 无 OpenAI-compatible /reranks 端点(compatible-mode 下 404),故走原生服务路径。
        post = self._session.post if self._session is not None else requests.post
        resp = post(
            f"{self.base_url}/services/rerank/text-rerank/text-rerank", headers=headers,
            json={"model": self.model,
                  "input": {"query": query, "documents": documents},
                  "parameters": {"return_documents": False, "top_n": len(documents)}},
            timeout=timeout)
        resp.raise_for_status()
        return resp.json()["output"]["results"]

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
