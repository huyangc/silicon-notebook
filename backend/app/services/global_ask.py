"""Global source Ask ownership, authority, and detached execution."""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import replace
from datetime import datetime, timezone
import heapq
import json
import threading
from uuid import uuid4

from app.models.global_ask import (
    GlobalAskAnswer, GlobalAskJob, GlobalAskRequest, GlobalConversationDetail,
    GlobalNotebookScope, GLOBAL_ASK_PAGE_MAX, GLOBAL_ASK_PAGE_SIZE,
)
from app.models.sources import SourceElement
from app.services.cancellation import AskCancelled, raise_if_cancelled
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context


class GlobalAskError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat()


class GlobalAskService:
    def __init__(self, *, store, notebooks, can_read, sources, retrieve, synthesize, settings,
                 rewrite_query=None):
        self.store = store
        self.notebooks = notebooks
        self.can_read = can_read
        self.sources = sources
        self.retrieve = retrieve
        self.synthesize = synthesize
        self.settings = settings
        self.rewrite_query = rewrite_query
        self._lock = threading.RLock()
        self._events = {}
        self._workers = {}

    def _check(self, ids, user_id, allowed_notebook_ids=None, authority_check=None):
        allowed = None if allowed_notebook_ids is None else set(allowed_notebook_ids)
        if authority_check is not None:
            try:
                current = authority_check()
            except Exception as exc:
                raise GlobalAskError(403, "访问权限已变化，请重新连接后重试。") from exc
            if current is not None:
                allowed = set(current) if allowed is None else allowed.intersection(current)
        for notebook_id in ids:
            if (allowed is not None and notebook_id not in allowed) or not self.can_read(notebook_id, user_id):
                raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")

    def _conversation(self, conversation_id, user_id):
        row = self.store.conversation(conversation_id, user_id)
        if row is None:
            raise GlobalAskError(404, "对话不存在，请刷新列表。")
        return row

    def _history(self, conversation, ids, user_id, allowed, authority_check):
        history = []
        if conversation and self.settings.global_ask_history_turns:
            jobs = self.store.jobs(conversation.id, user_id, self.settings.global_ask_history_turns, 0)
            for previous in reversed(jobs):
                if set(previous.resolved_notebook_ids).issubset(ids):
                    self._check(previous.resolved_notebook_ids, user_id, allowed, authority_check)
                    history.append("User: " + previous.question)
        return "\n".join(history)

    def start(self, payload: GlobalAskRequest, *, user_id, allowed_notebook_ids=None,
              submitted_via="web", authority_check=None):
        request_json = payload.model_dump_json()
        with self._lock:
            previous = self.store.request_job(user_id, payload.client_request_id)
            if previous is not None:
                job, old_request = previous
                if old_request != request_json:
                    raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。")
                self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids, authority_check)
                return job
            conversation = self._conversation(payload.conversation_id, user_id) if payload.conversation_id else None
            scope = payload.notebook_scope or (conversation.notebook_scope if conversation else GlobalNotebookScope())
            names = {row.id: row.name for row in self.notebooks(user_id)}
            allowed = None if allowed_notebook_ids is None else frozenset(allowed_notebook_ids)
            ids = list(scope.notebook_ids) if scope.mode == "include" else [
                key for key in names if allowed is None or key in allowed
            ]
            if not ids:
                raise GlobalAskError(422, "没有可访问的笔记本，请先创建笔记本并添加资料。")
            self._check(ids, user_id, allowed, authority_check)
            # Freeze every source ceiling before starting any retrieval or detached work.
            source_ceiling = {
                notebook_id: frozenset(self.sources.all_visible_source_ids(notebook_id))
                for notebook_id in ids
            }
            self._check(ids, user_id, allowed, authority_check)
            history = self._history(conversation, ids, user_id, allowed, authority_check)
            job = GlobalAskJob(
                job_id="gask-" + uuid4().hex,
                conversation_id=conversation.id if conversation else "gconv-" + uuid4().hex,
                status="running", question=payload.question, created_at=_now(),
                notebook_scope=scope, resolved_notebook_ids=ids,
            )
            try:
                self.store.create(
                    job, user_id, payload.client_request_id, request_json, submitted_via,
                    new_conversation=conversation is None,
                )
            except Exception as exc:
                previous = self.store.request_job(user_id, payload.client_request_id)
                if previous is not None and previous[1] == request_json:
                    self._check(previous[0].resolved_notebook_ids, user_id, allowed, authority_check)
                    return previous[0]
                if conversation:
                    active = self.store.jobs(conversation.id, user_id, GLOBAL_ASK_PAGE_SIZE, 0)
                    if any(item.status == "running" for item in active):
                        raise GlobalAskError(409, "这段对话仍在回答，请等待完成或停止后重试。") from exc
                raise
            event = threading.Event()
            context = copy_context()
            worker = threading.Thread(
                target=lambda: context.run(
                    self._run, job.model_copy(deep=True), user_id, names, history,
                    event, allowed, authority_check, source_ceiling,
                ),
                daemon=True, name="global-ask",
            )
            self._events[job.job_id] = event
            self._workers[job.job_id] = worker
            try:
                worker.start()
            except Exception:
                self._events.pop(job.job_id, None)
                self._workers.pop(job.job_id, None)
                job.status = "failed"
                job.error = "任务未能启动，请重新提交问题。"
                self.store.save(job, user_id)
                raise
            return job

    def _collect(self, job, user_id, event, allowed, authority_check, source_ceiling, query):
        candidates = []
        sequence = 0
        for notebook_id in job.resolved_notebook_ids:
            raise_if_cancelled(event)
            self._check([notebook_id], user_id, allowed, authority_check)
            source_ids = source_ceiling[notebook_id]
            scope = {
                "mode": "include", "source_ids": source_ids, "owner_id": user_id,
                "hidden_source_ids": [], "narrowed": True,
            }
            with source_scope_context(notebook_id, scope, {"mode": "include", "notebook_ids": []}):
                hits, _, _ = self.retrieve(notebook_id, query)
            for hit in hits:
                if hit.source_id not in source_ids:
                    continue
                hit = replace(hit, notebook_id=notebook_id)
                sequence += 1
                item = (float(hit.relevance or hit.score), sequence, hit)
                if len(candidates) < self.settings.global_ask_candidate_limit:
                    heapq.heappush(candidates, item)
                elif item[:2] > candidates[0][:2]:
                    heapq.heapreplace(candidates, item)
            job.searched_notebook_ids.append(notebook_id)
            if not self.store.save(job, user_id):
                raise AskCancelled()
        return [item[2] for item in sorted(candidates, reverse=True)]

    def _run(self, job, user_id, names, history, event, allowed, authority_check, source_ceiling):
        try:
            with retrieval_run(
                run_kind="ask_chunk", actor_id=user_id, correlation_id=job.job_id,
                fanout_limit=1, cancel_event=event,
            ):
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                query = self.rewrite_query(history, job.question, event) if self.rewrite_query else job.question
                chunks = self._collect(job, user_id, event, allowed, authority_check, source_ceiling, query)
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                evidence = self.sources.evidence_elements([
                    element_id for chunk in chunks for element_id in chunk.element_ids
                ])
                answer, grounded, anchors, citations = self.synthesize(job.question, chunks, names, history, event)
                raise_if_cancelled(event)
                self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
                self._validate_citations(citations, evidence, source_ceiling)
                job.cited_notebook_ids = list(dict.fromkeys(c.notebook_id for c in citations))
                job.response = GlobalAskAnswer(
                    answer_id="ganswer-" + uuid4().hex, question=job.question, answer=answer,
                    grounded=grounded, anchors=anchors, citations=citations, created_at=_now(),
                    notebook_scope=job.notebook_scope, resolved_notebook_ids=job.resolved_notebook_ids,
                    searched_notebook_ids=job.searched_notebook_ids, cited_notebook_ids=job.cited_notebook_ids,
                )
                job.status = "done"
                self.store.save(job, user_id)
        except AskCancelled:
            job.status, job.response = "cancelled", None
            self.store.save(job, user_id)
        except Exception as exc:
            job.status, job.response = "failed", None
            job.error = exc.message if isinstance(exc, GlobalAskError) else "回答未完成，请检查模型服务和资料状态后重试。"
            self.store.save(job, user_id)
        finally:
            with self._lock:
                self._events.pop(job.job_id, None)
                self._workers.pop(job.job_id, None)

    def _validate_citations(self, citations, evidence, source_ceiling):
        current = self.sources.evidence_elements([item.element_id for item in citations])
        visible = {}
        for citation in citations:
            notebook_id = citation.notebook_id
            if notebook_id not in visible:
                visible[notebook_id] = set(self.sources.all_visible_source_ids(notebook_id))
            previous = evidence.get(citation.element_id)
            row = current.get(citation.element_id)
            if (citation.source_id not in source_ceiling.get(notebook_id, ())
                    or citation.source_id not in visible[notebook_id]
                    or previous is None or row != previous
                    or row["source_id"] != citation.source_id):
                raise GlobalAskError(409, "引用资料在回答期间发生了变化，请重新提问。")

    def get_job(self, job_id, *, user_id, allowed_notebook_ids=None):
        job = self.store.job(job_id, user_id)
        if job is None:
            raise GlobalAskError(404, "问答任务不存在，请刷新对话。")
        self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids)
        return job

    def cancel(self, job_id, *, user_id, allowed_notebook_ids=None):
        with self._lock:
            job = self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
            if job.status == "running":
                event = self._events.get(job_id)
                if event is not None:
                    event.set()
                job.status, job.response = "cancelled", None
                self.store.save(job, user_id)
            return self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)

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
        for job in turns:
            self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids)
        return GlobalConversationDetail(
            **conversation.model_dump(), turns=list(reversed(turns)), has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )

    def rename_conversation(self, conversation_id, title, *, user_id):
        self._conversation(conversation_id, user_id)
        self.store.rename(conversation_id, user_id, title)
        return self._conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id, *, user_id):
        with self._lock:
            self._conversation(conversation_id, user_id)
            for job in self.store.jobs(conversation_id, user_id, GLOBAL_ASK_PAGE_SIZE, 0):
                event = self._events.get(job.job_id)
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
            for event in self._events.values():
                event.set()
            workers = list(self._workers.values())
        for worker in workers:
            worker.join()
