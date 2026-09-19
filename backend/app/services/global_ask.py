"""Global source Ask ownership, authority, and detached execution."""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import replace
from datetime import datetime, timezone
import json
import threading
import time
import time
from uuid import uuid4

from app.models.global_ask import (
    GlobalAskAnswer, GlobalAskJob, GlobalAskRequest, GlobalConversationDetail,
    GlobalNotebookScope, GLOBAL_ASK_PAGE_MAX, GLOBAL_ASK_PAGE_SIZE,
    GlobalAskSkippedNotebook,
)
from app.models.sources import SourceElement
from app.services.cancellation import AskCancelled, raise_if_cancelled
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from app.services.global_evidence import peer_evidence
from app.repositories.read_budget import read_budget, ReadBudgetExceeded
from app.services.global_retrieval import GlobalRetrievalSkipped, GlobalRetrievalResult


class GlobalAskError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat()


class GlobalAskService:
    def __init__(self, *, store, notebooks, can_read, sources, retrieve, synthesize, settings,
                 rewrite_query=None, can_read_many=None, prepare_query=None):
        self.store = store
        self.notebooks = notebooks
        self.can_read = can_read
        self.can_read_many = can_read_many
        self.sources = sources
        self.retrieve = retrieve
        self.synthesize = synthesize
        self.settings = settings
        self.rewrite_query = rewrite_query
        self.prepare_query = prepare_query
        self._lock = threading.RLock()
        self._events = {}
        self._workers = {}
        self._pending = 0
        self._closed = False

    def _check(self, ids, user_id, allowed_notebook_ids=None, authority_check=None):
        ids = set(ids)
        allowed = None if allowed_notebook_ids is None else set(allowed_notebook_ids)
        if authority_check is not None:
            try:
                current = authority_check()
            except Exception as exc:
                raise GlobalAskError(403, "访问权限已变化，请重新连接后重试。") from exc
            if current is not None:
                allowed = set(current) if allowed is None else allowed.intersection(current)
        if allowed is not None and not ids.issubset(allowed):
            raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")
        readable = (set(self.can_read_many(sorted(ids), user_id)) if self.can_read_many else
                    {notebook_id for notebook_id in ids if self.can_read(notebook_id, user_id)})
        if not ids.issubset(readable):
            raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")

    def _conversation(self, conversation_id, user_id):
        row = self.store.conversation(conversation_id, user_id)
        if row is None:
            raise GlobalAskError(404, "对话不存在，请刷新列表。")
        return row

    def _save_if_open(self, job, user_id, *, progress=False):
        """No detached worker may reopen persistence after runtime shutdown."""
        with self._lock:
            if self._closed:
                return False
            return self.store.save_progress(job, user_id) if progress else self.store.save(job, user_id)

    def _history(self, conversation, ids, user_id, allowed, authority_check):
        history = []
        if conversation and self.settings.global_ask_history_turns:
            turns = self.store.completed_history(conversation.id, user_id, self.settings.global_ask_history_turns)
            admitted = [turn for turn in turns if set(turn["notebook_ids"]).issubset(ids)]
            if admitted:
                self._check({notebook_id for turn in admitted for notebook_id in turn["notebook_ids"]},
                            user_id, allowed, authority_check)
            for turn in reversed(admitted):
                history.append("User: " + turn["question"])
                history.append("Assistant (conversation context, not evidence): " + turn["answer"])
        return "\n".join(history)

    def start(self, payload: GlobalAskRequest, *, user_id, allowed_notebook_ids=None,
              submitted_via="web", authority_check=None):
        request_json = payload.model_dump_json()
        previous = self.store.request_job(user_id, payload.client_request_id)
        if previous is not None:
            job, old_request = previous
            if old_request != request_json:
                raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。")
            self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids, authority_check)
            return job
        with self._lock:
            if self._closed:
                raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
            if len(self._workers) + self._pending >= self.settings.global_ask_max_concurrent:
                raise GlobalAskError(429, "全局问答正在处理其他任务，请稍后重试。")
            self._pending += 1
        try:
            conversation = self._conversation(payload.conversation_id, user_id) if payload.conversation_id else None
            scope = payload.notebook_scope or (conversation.notebook_scope if conversation else GlobalNotebookScope())
            rows = self.notebooks(user_id)
            names = rows if isinstance(rows, dict) else {row.id: row.name for row in rows}
            allowed = None if allowed_notebook_ids is None else frozenset(allowed_notebook_ids)
            ids = list(scope.notebook_ids) if scope.mode == "include" else [
                key for key in names if allowed is None or key in allowed
            ]
            if not ids:
                raise GlobalAskError(422, "没有可访问的笔记本，请先创建笔记本并添加资料。")
            if len(ids) > self.settings.global_ask_max_notebooks:
                raise GlobalAskError(422, f"本次范围超过 {self.settings.global_ask_max_notebooks} 个笔记本，请缩小范围后重试。")
            self._check(ids, user_id, allowed, authority_check)
            # Freeze every source ceiling before starting any retrieval or detached work.
            source_rows = self.sources.visible_source_ids_by_notebook(ids)
            source_ceiling = {notebook_id: frozenset(source_rows[notebook_id]) for notebook_id in ids}
            self._check(ids, user_id, allowed, authority_check)
            history = self._history(conversation, ids, user_id, allowed, authority_check)
            job = GlobalAskJob(
                job_id="gask-" + uuid4().hex,
                conversation_id=conversation.id if conversation else "gconv-" + uuid4().hex,
                status="running", question=payload.question, created_at=_now(),
                notebook_scope=scope, resolved_notebook_ids=ids,
            )
            event = threading.Event()
            context = copy_context()
            worker = threading.Thread(
                target=lambda: context.run(
                    self._run, job.model_copy(deep=True), user_id, names, history,
                    event, allowed, authority_check, source_ceiling,
                ),
                daemon=True, name="global-ask",
            )
            created = False
            try:
                with self._lock:
                    if self._closed:
                        raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
                    self.store.create(
                        job, user_id, payload.client_request_id, request_json, submitted_via,
                        new_conversation=conversation is None,
                    )
                    created = True
                    self._events[job.job_id] = event
                    self._workers[job.job_id] = worker
                    worker.start()
            except Exception as exc:
                if created:
                    with self._lock:
                        self._events.pop(job.job_id, None)
                        self._workers.pop(job.job_id, None)
                    job.status = "failed"
                    job.error = "任务未能启动，请重新提交问题。"
                    self.store.save(job, user_id)
                    raise
                previous = self.store.request_job(user_id, payload.client_request_id)
                if previous is not None:
                    if previous[1] != request_json:
                        raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。") from exc
                    self._check(previous[0].resolved_notebook_ids, user_id, allowed, authority_check)
                    return previous[0]
                if conversation and self.store.running_job_ids(conversation.id, user_id):
                    raise GlobalAskError(409, "这段对话仍在回答，请等待完成或停止后重试。") from exc
                raise
            return job
        finally:
            with self._lock:
                self._pending -= 1

    def _collect(self, job, user_id, event, allowed, authority_check, source_ceiling, query):
        pools = []
        evidence = {}
        deadline = time.monotonic() + self.settings.global_ask_retrieval_timeout_seconds
        for notebook_id in job.resolved_notebook_ids:
            raise_if_cancelled(event)
            if time.monotonic() >= deadline:
                job.skipped_notebooks.append(GlobalAskSkippedNotebook(
                    notebook_id=notebook_id, reason="总检索时限已到，请缩小范围后重试。",
                ))
                if not self._save_if_open(job, user_id, progress=True):
                    raise AskCancelled()
                continue
            self._check([notebook_id], user_id, allowed, authority_check)
            source_ids = source_ceiling[notebook_id]
            scope = {
                "mode": "include", "source_ids": source_ids, "owner_id": user_id,
                "hidden_source_ids": [], "narrowed": True,
            }
            notebook_deadline = min(deadline, time.monotonic() + self.settings.global_ask_notebook_timeout_seconds)
            try:
                with read_budget(notebook_deadline, event), source_scope_context(
                    notebook_id, scope, {"mode": "include", "notebook_ids": []},
                ):
                    result = self.retrieve(notebook_id, query)
                if time.monotonic() >= notebook_deadline:
                    raise ReadBudgetExceeded()
            except (GlobalRetrievalSkipped, ReadBudgetExceeded) as exc:
                reason = "检索超时，请缩小范围后重试。" if isinstance(exc, ReadBudgetExceeded) or exc.reason == "timeout" else "检索暂不可用，请检查资料索引后重试。"
                job.skipped_notebooks.append(GlobalAskSkippedNotebook(notebook_id=notebook_id, reason=reason))
                if not self._save_if_open(job, user_id, progress=True):
                    raise AskCancelled()
                continue
            if isinstance(result, GlobalRetrievalResult):
                hits = result.chunks
                evidence.update(result.evidence_fingerprints)
                if result.degraded:
                    job.degraded_notebook_ids.append(notebook_id)
            else:
                hits, _, _ = result
                # Compatibility for injected retrieval adapters. The production
                # global retriever supplies fingerprints from its SQL snapshot.
                evidence.update(self.sources.evidence_fingerprints([
                    element_id for hit in hits for element_id in hit.element_ids
                ]))
            pool = []
            for hit in hits:
                if hit.source_id not in source_ids:
                    continue
                pool.append(replace(hit, notebook_id=notebook_id))
            pools.append(pool)
            job.searched_notebook_ids.append(notebook_id)
            if not self._save_if_open(job, user_id, progress=True):
                raise AskCancelled()
        return peer_evidence(pools, self.settings.global_ask_candidate_limit), evidence

    def _run(self, job, user_id, names, history, event, allowed, authority_check, source_ceiling):
        try:
            with retrieval_run(
                run_kind="ask_global", actor_id=user_id, correlation_id=job.job_id,
                fanout_limit=1, cancel_event=event,
            ):
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                query = self.rewrite_query(history, job.question, event) if self.rewrite_query else job.question
                if self.prepare_query:
                    self.prepare_query(query)
                chunks, evidence = self._collect(job, user_id, event, allowed, authority_check, source_ceiling, query)
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                answer, grounded, anchors, citations = self.synthesize(job.question, chunks, names, history, event)
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                invalid = self._validate_citations(citations, evidence, source_ceiling, chunks)
                if invalid:
                    # Rebuild the answer from surviving evidence, never just erase a
                    # marker while retaining the claim that depended on changed text.
                    chunks = [chunk for chunk in chunks if chunk.chunk_id not in invalid]
                    if chunks:
                        raise_if_cancelled(event)
                        self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                        answer, grounded, anchors, citations = self.synthesize(job.question, chunks, names, history, event)
                        invalid = self._validate_citations(citations, evidence, source_ceiling, chunks)
                    if invalid or not chunks:
                        answer = "引用原文在回答期间发生了变化，暂时无法提供可靠结论，请重新提问。"
                        grounded, anchors, citations = False, [], []
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                job.cited_notebook_ids = list(dict.fromkeys(c.notebook_id for c in citations))
                job.response = GlobalAskAnswer(
                    answer_id="ganswer-" + uuid4().hex, question=job.question, answer=answer,
                    grounded=grounded, anchors=anchors, citations=citations, created_at=_now(),
                    notebook_scope=job.notebook_scope, resolved_notebook_ids=job.resolved_notebook_ids,
                    searched_notebook_ids=job.searched_notebook_ids, cited_notebook_ids=job.cited_notebook_ids,
                    skipped_notebooks=job.skipped_notebooks,
                    degraded_notebook_ids=job.degraded_notebook_ids,
                )
                job.status = "done"
                self._save_if_open(job, user_id)
        except AskCancelled:
            job.status, job.response = "cancelled", None
            self._save_if_open(job, user_id)
        except Exception as exc:
            job.status, job.response = "failed", None
            job.error = exc.message if isinstance(exc, GlobalAskError) else "回答未完成，请检查模型服务和资料状态后重试。"
            self._save_if_open(job, user_id)
        finally:
            with self._lock:
                self._events.pop(job.job_id, None)
                self._workers.pop(job.job_id, None)

    def _validate_citations(self, citations, evidence, source_ceiling, chunks):
        supporting = []
        for citation in citations:
            matches = [chunk for chunk in chunks if (
                chunk.notebook_id == citation.notebook_id
                and chunk.source_id == citation.source_id
                and citation.element_id in chunk.element_ids
            )]
            if not matches:
                return {chunk.chunk_id for chunk in chunks}
            supporting.extend(matches)
        current = self.sources.evidence_fingerprints([
            element_id for chunk in supporting for element_id in chunk.element_ids
        ])
        visible = {}
        invalid = set()
        for chunk in supporting:
            notebook_id = chunk.notebook_id
            if notebook_id not in visible:
                visible[notebook_id] = set(self.sources.all_visible_source_ids(notebook_id))
            if (chunk.source_id not in source_ceiling.get(notebook_id, ())
                    or chunk.source_id not in visible[notebook_id]
                    or not chunk.element_ids
                    or any(evidence.get(element_id) is None
                           or current.get(element_id) != evidence[element_id]
                           or current[element_id][0] != chunk.source_id
                           for element_id in chunk.element_ids)):
                invalid.add(chunk.chunk_id)
        return invalid

    def get_job(self, job_id, *, user_id, allowed_notebook_ids=None):
        job = self.store.job(job_id, user_id)
        if job is None:
            raise GlobalAskError(404, "问答任务不存在，请刷新对话。")
        self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids)
        return job

    def cancel(self, job_id, *, user_id, allowed_notebook_ids=None):
        job = self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        if job.status == "running":
            with self._lock:
                event = self._events.get(job_id)
                if event is not None:
                    event.set()
            job.status, job.response = "cancelled", None
            if not self.store.save(job, user_id):
                return self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        return job

    @staticmethod
    def _validate_page(limit, offset):
        if not 1 <= limit <= GLOBAL_ASK_PAGE_MAX or offset < 0:
            raise GlobalAskError(422, "分页参数无效，请刷新后重试。")

    def list_conversations(self, *, user_id, limit=GLOBAL_ASK_PAGE_SIZE, offset=0):
        self._validate_page(limit, offset)
        return self.store.list_conversations(user_id, limit, offset)

    def conversation(self, conversation_id, *, user_id, allowed_notebook_ids=None,
                     limit=GLOBAL_ASK_PAGE_SIZE, offset=0):
        self._validate_page(limit, offset)
        conversation = self._conversation(conversation_id, user_id)
        jobs = self.store.jobs(conversation_id, user_id, limit + 1, offset)
        has_more = len(jobs) > limit
        turns = jobs[:limit]
        self._check({notebook_id for job in turns for notebook_id in job.resolved_notebook_ids},
                    user_id, allowed_notebook_ids)
        return GlobalConversationDetail(
            **conversation.model_dump(), turns=list(reversed(turns)), has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )

    def rename_conversation(self, conversation_id, title, *, user_id):
        self._conversation(conversation_id, user_id)
        self.store.rename(conversation_id, user_id, title)
        return self._conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id, *, user_id):
        self._conversation(conversation_id, user_id)
        with self._lock:
            for job_id in self.store.running_job_ids(conversation_id, user_id):
                event = self._events.get(job_id)
                if event is not None:
                    event.set()
            self.store.delete(conversation_id, user_id)

    def cited_element(self, job_id, element_id, *, user_id, allowed_notebook_ids=None):
        job = self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        references = job.response.citations if job.response else []
        citation = next((item for item in references if item.element_id == element_id), None)
        if citation is None:
            raise GlobalAskError(404, "引用不存在，请重新打开答案。")
        self._check([citation.notebook_id], user_id, allowed_notebook_ids)
        try:
            source = self.sources.get_source(citation.source_id)
        except KeyError as exc:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。") from exc
        if source.notebook_id != citation.notebook_id or source.type in {"memory", "knowhow"}:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。")
        row = self.sources.evidence_elements([element_id]).get(element_id)
        if row is None or row["source_id"] != citation.source_id:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。")
        self._check([citation.notebook_id], user_id, allowed_notebook_ids)
        data = dict(row)
        if isinstance(data.get("metadata"), str):
            data["metadata"] = json.loads(data["metadata"])
        return SourceElement.model_validate(data)

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._events.values():
                event.set()
            workers = list(self._workers.values())
        deadline = time.monotonic() + self.settings.global_ask_shutdown_timeout_seconds
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
