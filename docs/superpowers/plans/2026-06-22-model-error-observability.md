# 模型调用错误可观测性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 当 embedding / rerank / LLM 调用失败时,统一记录(日志+事件)并在前端明确提示(降级不中断),不再伪装成「未命中证据」。

**Architecture:** 仓库是单例(`@lru_cache get_repository`),故用 `contextvars.ContextVar` 做「每次 ask 的错误收集槽」(请求级、无跨请求竞争;多子查询线程池用 `copy_context` 传播)。失败点调用 `_note_model_error(stage, model, exc)`:始终 emit `model_error` 事件(L1),若处于 ask 上下文则追加到 sink(L2)。ask 结束把 sink 写进 `AskResponse.model_errors`,前端据此显示横幅。

**Tech Stack:** FastAPI/pydantic 后端,Next.js/React 前端,pytest。

**用户决策:** 降级+前端明确提示(非硬失败);不做 /health?validate。

---

## Task 1: 后端——错误收集机制 + 失败点接线 + schema

**Files:**
- Modify: `backend/app/models/schemas.py`(加 `ModelError` + `AskResponse.model_errors`)
- Modify: `backend/app/services/sqlite_repository.py`(ContextVar + `_note_model_error` + `_embed_query` + `_retrieve_chunks_multi` 上下文传播 + 三个 ask 接线)
- Modify: `backend/app/services/rerank_client.py`(`rerank(on_error=...)` + L1 日志)
- Test: `backend/tests/test_model_errors.py`(新建)

### Step 1 — schema(`schemas.py`)
在 `AskResponse` 定义之前加:
```python
class ModelError(BaseModel):
    stage: str       # "embed" | "rerank" | "answer" | "rewrite"
    model: str = ""  # 出错的模型名/角色
    message: str     # 简短错误(类型+截断信息)
```
在 `AskResponse` 字段末尾(`kg_required` 后)加:
```python
    model_errors: List[ModelError] = Field(default_factory=list)
```

### Step 2 — ContextVar + 记录助手(`sqlite_repository.py`)
模块顶部 import 区加 `import contextvars`。模块级(类外)加:
```python
# 每次 ask 的模型错误收集槽(请求级;仓库是单例,不能用实例状态)。None=不在 ask 上下文。
_ASK_MODEL_ERRORS: "contextvars.ContextVar[list | None]" = contextvars.ContextVar(
    "ask_model_errors", default=None)
```
`SQLiteRepository` 内加方法:
```python
    def _note_model_error(self, stage: str, model: str, exc: Exception) -> None:
        """记录一次模型调用失败:始终 emit model_error 事件(L1);若在 ask 上下文
        (ContextVar 有 sink)则追加到 sink 供 AskResponse.model_errors 回传前端(L2)。"""
        msg = f"{type(exc).__name__}: {exc}"
        self.event_log.emit({"kind": "model_error", "stage": stage, "model": model or "",
                             "error": msg[:300], "status": "error"})
        sink = _ASK_MODEL_ERRORS.get()
        if sink is not None:
            sink.append({"stage": stage, "model": model or "", "message": msg[:200]})
```

### Step 3 — `_embed_query` 失败记录
当前(确认行号后改):静默 `except` 返回 None。改为:
```python
    def _embed_query(self, query: str) -> Optional[List[float]]:
        if not self.settings.embedder_configured:
            return None
        try:
            return self.embedder.embed_query(query)
        except Exception as exc:
            self._note_model_error("embed", self.settings.embed_model, exc)
            return None
```
（保持返回 None 的降级语义不变,只是加记录。具体 try 体以现有实现为准——把现有的 embed 调用包进 try,except 改为记录+返回 None。)

### Step 4 — `RerankClient.rerank(on_error=...)`(`rerank_client.py`)
顶部加 `import logging` + `logger = logging.getLogger("silicon_notebook.rerank")`。签名加可选 `on_error`:
```python
    def rerank(self, query, documents, on_error=None):
        if not self.configured or not documents:
            return list(range(len(documents)))
        try:
            ...（不变）...
            return order
        except Exception as exc:
            logger.warning("rerank failed, falling back to identity: %s", exc)   # L1
            if on_error is not None:
                on_error(exc)                                                     # L2(由 handler 接 _note_model_error)
            return list(range(len(documents)))
```
（`_rerank_split`/`_rerank_batch` 不变;`on_error` 仅在顶层 except 触发一次。）

### Step 5 — `_retrieve_chunks_multi` 线程上下文传播
多子查询用 ThreadPoolExecutor,worker 默认不继承 ContextVar → embedding 失败不会进 sink。把 worker 包进当前上下文:
```python
        import contextvars as _cv
        _ctx = _cv.copy_context()
        def _one(q):
            try:
                return _ctx.run(self._retrieve_chunks, notebook_id, q)
            except Exception:
                return ([], [], None)
```
（其余不变。）

### Step 6 — 三个 ask handler 接线
`ask_chunk` / `ask_graph` / `ask_reasoning` 各自:在进入检索/答案前设 sink,`finally` 复位;答案 LLM 的 except 记一次;结束把 sink 写进 response。模式:
```python
        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        try:
            ...（原有检索 + 答案逻辑;失败点经 _note_model_error 自动记录）...
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
        ...
        response.model_errors = [ModelError(**e) for e in _err_sink]   # 在构造/返回 response 处
```
具体:
- **ask_chunk**:答案 except(现 `self.event_log.logger.exception("answer failed...")`)后加 `self._note_model_error("answer", self.settings.openai_compat_model, exc)`(改 `except Exception:` 为 `except Exception as exc:`)。rerank 调用加 `on_error=lambda e: self._note_model_error("rerank", self.settings.rerank_model, e)`。`from app.models.schemas import ... ModelError`。response 构造后/返回前 set `response.model_errors`。
- **ask_graph**:答案 except 后加 `self._note_model_error("answer", self.settings.openai_compat_model, exc)`。同样设/复位 sink + 写 response.model_errors。
- **ask_reasoning**:答案 except 后 `self._note_model_error("answer", (self.settings.reasoning_llm_model or self.settings.openai_compat_model), exc)`。同样。
（reasoning/graph 不调 rerank;embedding 经 `_retrieve_scored`→`_embed_query` 自动记录,无需额外接线。)

### Step 7 — 测试(`test_model_errors.py`)
用 FakeEmbedder/Fake LLM 注入失败,断言:
1. `test_answer_llm_failure_recorded`:fake llm_client.chat_json 抛异常 → `ask_chunk` 返回的 `resp.model_errors` 含 `stage=="answer"`,且 `resp.llm_mode=="deterministic"`(降级不中断)。
2. `test_embed_failure_recorded`:embedder.embed_query 抛异常(embedder_configured=True)→ ask 后 `model_errors` 含 `stage=="embed"`;检索仍返回(关键词降级)。
3. `test_rerank_on_error_called`:RerankClient `_rerank_batch` 抛异常 + 传 `on_error` → on_error 被调用且 `rerank` 返回 identity。
4. `test_note_model_error_emits_event_without_sink`:不在 ask 上下文(sink=None)调用 `_note_model_error` 不抛错(只 emit 事件)。
5. `test_no_errors_empty_list`:正常 ask → `resp.model_errors == []`。

- [ ] 写测试 → 跑红 → 实现 → 跑绿:`cd backend && python -m pytest tests/test_model_errors.py tests/test_mix_answer.py tests/test_rerank_client.py -q`
- [ ] 提交:`git add -A && git commit`(消息见下)

提交信息:
```
feat(obs): 模型调用失败统一记录(model_error 事件)+ 回传 AskResponse.model_errors

embed/rerank/answer-LLM 失败经 _note_model_error 记事件(L1);ask 内经 ContextVar
sink 收集回传前端(L2,降级不中断)。仓库单例故用 contextvars(多子查询线程 copy_context 传播)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 2: 前端——模型错误横幅

**Files:**
- Modify: `frontend/app/page.tsx`(`AskResponse` 类型加 `model_errors`;答案区渲染横幅)
- Modify: 对应样式文件(新增 `.answer-model-error` 类,告警色)

### Step 1 — 类型
`AskResponse` type 加:
```typescript
  model_errors?: { stage: string; model: string; message: string }[];
```

### Step 2 — 渲染横幅
在答案块顶部(evidence-level tag 附近、conclusion 之上),当 `answer.model_errors?.length` 时显示明显告警横幅,区别于「推断/未命中」标签。中文映射 stage:`embed`→「向量模型」、`rerank`→「重排模型」、`answer`→「答案模型」、`rewrite`→「改写模型」。文案如:
```
⚠️ 部分模型调用失败(向量模型、答案模型),本次结果为降级输出,可能不完整或未接地。请检查 API key / 服务可用性。
```
（列出去重后的 stage 中文名;hover/title 可显示首条 message。）样式:醒目告警色(红/橙),与正常 evidence tag 视觉分明。

- [ ] `cd frontend && npx tsc --noEmit`(或项目既有类型检查)通过。
- [ ] 提交。

提交信息:
```
feat(obs): 前端答案区模型失败告警横幅(消费 AskResponse.model_errors)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 3: 终审 + 验证
- [ ] 全量后端 `python -m pytest -q` 全绿;`bash scripts/check.sh` EXIT=0。
- [ ] 前端类型检查通过;preview 验证横幅(注入失败可见)。
- [ ] 终审:确认 `model_errors` 在三种 mode 都接线;ContextVar finally 复位无泄漏;L1 事件在无 ask 上下文也工作。

## Self-Review
- 单例仓库 → ContextVar(非实例状态),多子查询线程 copy_context 传播 ✓
- 降级不中断(返回 None / identity / 空答案兜底照旧),只多了记录 ✓
- L1(事件,始终)与 L2(ContextVar sink,仅 ask)分离;无 ask 上下文 sink=None 安全 ✓
- 不改 grounding/tau/检索语义;model_errors 是纯附加可观测字段 ✓
