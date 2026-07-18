"""深度报告引擎:大纲规划 → 每节完整 reasoning 深挖(节间并行) → 逐节撰写
(证据三层 [k]/（推断）/【通识】) → 汇总(执行摘要/参考文献/结尾局限)。

设计对齐 docs/superpowers/specs/2026-07-03-deep-report-mode-design.md。
形态镜像 ReasoningRetriever(Task 25 端口化):引擎只持 ReportEngineDependencies
里的窄端口(reports/retrieval/evidence_context/model_clients/model_errors/
source_query/communities),写库经 reports 端口,不再持 repository facade;
旧 repository 调用点由 from_repository 一次性工厂适配。
线程要点:节间 ThreadPoolExecutor 并行,worker 不继承 ContextVar——每个 submit
用 contextvars.copy_context().run 包裹,保住 per-user 模型解析。
取消注册表:进程全局所有权在 report_execution.REPORT_CANCELLATIONS,本模块的
register_cancel/cancel_report/unregister_cancel 是它的显式委托(冻结调用点)。
"""
from __future__ import annotations
import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List

from app.core.llm import cap_kwargs
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.report_execution import REPORT_CANCELLATIONS

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.repositories.ports import (
        CommunityQueryPort, EvidenceContextPort, ModelClientProvider,
        ModelErrorSink, ReportRepository, ReportSourceQueryPort, RetrievalPort,
    )

_MARKER = re.compile(r"\[(k\d+(?:\s*,\s*k\d+)*)\]")   # 节内 [k_i] 或 [k_i, k_j] 引用标记(全局重编号用)


# --- 取消注册表委托:report_id → threading.Event(活动后台 job 才在册) ---
# 所有权在 report_execution.REPORT_CANCELLATIONS(进程全局唯一实例);这三个
# 函数保持冻结调用点(routes cancel 端点/测试)可用。

def register_cancel(report_id: str) -> threading.Event:
    ev = threading.Event()
    REPORT_CANCELLATIONS.register(report_id, ev)
    return ev


def cancel_report(report_id: str) -> bool:
    return REPORT_CANCELLATIONS.cancel(report_id)


def unregister_cancel(report_id: str) -> None:
    REPORT_CANCELLATIONS.unregister(report_id)


@dataclass(frozen=True)
class ReportEngineDependencies:
    """引擎的全部协作面(窄端口,消费者所有的契约见 app.repositories.ports)。"""
    reports: "ReportRepository"
    retrieval: "RetrievalPort"
    evidence_context: "EvidenceContextPort"
    model_clients: "ModelClientProvider"
    model_errors: "ModelErrorSink"
    source_query: "ReportSourceQueryPort"
    communities: "CommunityQueryPort"
    settings: "Settings"
    event_log: Any
    memory_retriever: Any = None


class ReportEngine:
    def __init__(self, dependencies: ReportEngineDependencies, *,
                 user_id: str, cancel_event: CancelEvent = None):
        self.dependencies = dependencies
        self.settings = dependencies.settings
        self.user_id = user_id            # 发起者身份(审计归属;模型解析走 ContextVar)
        self.cancel_event = cancel_event

    @classmethod
    def from_repository(cls, repository, settings, cancel_event: CancelEvent = None):
        """Frozen-call-site adapter; extracts narrow ports and retains no facade."""
        engine = repository.report_execution.engine_factory(
            user_id=repository.current_user().id,
            cancel_event=cancel_event,
            settings=settings,
        )
        if cls is ReportEngine:
            return engine
        return cls(
            engine.dependencies,
            user_id=engine.user_id,
            cancel_event=engine.cancel_event,
        )

    # --- Stage A ---
    def _plan_outline(self, notebook_id: str, question: str, history: str) -> List[dict]:
        from app.services.prompts import report_outline_prompt, REPORT_OUTLINE_SCHEMA_HINT
        client = self.dependencies.model_clients.reasoning_llm_client
        try:
            raw = client.chat_json(
                [{"role": "user", "content": report_outline_prompt(
                    question, max_sections=self.settings.report_max_sections,
                    history_block=history)}],
                REPORT_OUTLINE_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            sections = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    sections.append({"title": title,
                                     "scope": str(s.get("scope", "")).strip(),
                                     "sub_queries": subs[:4]})
            if sections:
                return sections
        except AskCancelled:
            raise
        except Exception:
            pass
        # 回退骨架:expand_query 的子查询平铺为单节(保证总能出报告)。
        from app.services.query_rewrite import expand_query
        ex = expand_query(self.dependencies.model_clients.rewrite_llm_client,
                          question, history)
        return [{"title": "分析", "scope": question,
                 "sub_queries": [s.query for s in ex.sub_queries][:4] or [question]}]

    # --- Stage A(STORM):Corpus map 0-LLM 语料侦察 ---
    _SCOUT_KG_N = 12
    _SCOUT_CHUNK_N = 8

    def _build_corpus_map(self, notebook_id: str, question: str) -> str:
        """0-LLM 语料侦察:来源标题 + federated KG 命中 + PPR chunk 来源·路径。
        给 STORM 规划接地(治盲规划)。任一子步失败静默降级为空段。"""
        deps = self.dependencies
        parts: List[str] = []
        try:
            rows = deps.source_query.report_source_rows(notebook_id)
            titles = [str(r["title"]).strip() for r in rows if str(r["title"]).strip()]
            if titles:
                parts.append("本 notebook 来源文件:\n" + "\n".join(f"- {t}" for t in titles))
        except Exception:
            pass
        try:
            kg = deps.retrieval.federated_retrieve(notebook_id, question)[: self._SCOUT_KG_N]
            if kg:
                parts.append("检索到的知识条目(name[type][tier]):\n" + "\n".join(
                    f"- {str(h.payload.get('name','')).strip()}"
                    f"[{h.object_type}][{getattr(h,'tier','personal')}]" for h in kg))
        except Exception:
            pass
        try:
            chunks = deps.retrieval.ppr_retrieve(notebook_id, question)[: self._SCOUT_CHUNK_N]
            if chunks:
                parts.append("相关原文所在(来源·章节,不含正文):\n" + "\n".join(
                    f"- {c.source_title} · {c.section_path}" for c in chunks))
        except Exception:
            pass
        try:
            memories = (
                deps.memory_retriever.notebook_memory_hits(
                    self.user_id, notebook_id, question, 8
                )
                if deps.memory_retriever is not None else []
            )
            if memories:
                parts.append("用户已确认 Memory:\n" + "\n".join(
                    f"- {item.title}: {item.text[:240]}" for item in memories
                ))
        except Exception:
            pass
        return ("\n\n".join(parts))[:4000] if parts else "(语料侦察无结果)"

    def _probe_sufficiency(self, notebook_id: str, sections: List[dict]) -> List[dict]:
        """0-LLM 客观信号:每节各 sub_query 跑 federated_retrieve,统计命中并集(base 拆分)。"""
        out = []
        for s in sections:
            seen, base = set(), set()
            for q in (s.get("sub_queries") or []):
                try:
                    for h in self.dependencies.retrieval.federated_retrieve(notebook_id, str(q)):
                        seen.add(h.object_id)
                        if getattr(h, "tier", "") == "base":
                            base.add(h.object_id)
                except Exception:
                    continue
            out.append({"title": s.get("title", ""), "hits": len(seen), "base_hits": len(base)})
        return out

    # --- Stage A 编排:map → STORM → 探针 → Judge → 富大纲 → outline_ready ---
    def plan_outline(self, notebook_id, rid, question, history="") -> None:
        reports = self.dependencies.reports
        try:
            reports.update_report(notebook_id, rid, status="planning", progress="侦察语料中")
            corpus_map = self._build_corpus_map(notebook_id, question)
            raise_if_cancelled(self.cancel_event)
            reports.update_report(notebook_id, rid, progress="多视角规划大纲中")
            sections = self._storm_outline(notebook_id, question, history, corpus_map)
            # 充分性:探针(0 LLM)+ Judge(flash)
            probe = self._probe_sufficiency(notebook_id, sections)
            sections = self._judge_sufficiency(question, sections, probe)
            reports.update_report(notebook_id, rid, outline=sections,
                                  status="outline_ready",
                                  progress=f"大纲就绪({len(sections)} 节),待确认")
        except AskCancelled:
            reports.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            reports.update_report(notebook_id, rid, status="failed",
                                  error=str(exc)[:500], progress="规划失败")

    def _storm_outline(self, notebook_id, question, history, corpus_map) -> List[dict]:
        from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
        try:
            raw = self.dependencies.model_clients.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_storm_outline_prompt(
                    question, corpus_map, max_sections=self.settings.report_max_sections,
                    history_block=history)}],
                REPORT_STORM_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            out = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    out.append({
                        "title": title, "scope": str(s.get("scope", "")).strip(),
                        "sub_queries": subs[:4],
                        "perspectives": [str(p).strip() for p in (s.get("perspectives") or []) if str(p).strip()],
                        "tensions": [str(t).strip() for t in (s.get("tensions") or []) if str(t).strip()]})
            if out:
                return out
        except AskCancelled:
            raise
        except Exception:
            pass
        return self._plan_outline(notebook_id, question, history)   # 回退现行骨架

    def _judge_sufficiency(self, question, sections, probe) -> List[dict]:
        from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
        by_title = {p["title"]: p for p in probe}
        # 缺省:按探针命中给保守判定(Judge 失败也有充分性信号)
        for s in sections:
            h = by_title.get(s["title"], {"hits": 0, "base_hits": 0})
            s.setdefault("sufficiency", "充足" if h["hits"] >= 3 else "薄弱" if h["hits"] else "缺失")
            s.setdefault("gap_note", "")
            s.setdefault("action", "keep" if h["hits"] >= 3 else "supplement" if h["hits"] else "external")
        try:
            block = "\n".join(f"- {p['title']}: hits={p['hits']} base_hits={p['base_hits']}" for p in probe)
            raw = self.dependencies.model_clients.rewrite_llm_client.chat_json(
                [{"role": "user", "content": report_sufficiency_prompt(question, block)}],
                REPORT_SUFFICIENCY_SCHEMA_HINT, cancel_event=self.cancel_event)
            for v in (json.loads(raw).get("verdicts") or []):
                for s in sections:
                    if s["title"] == str(v.get("title", "")).strip():
                        if v.get("sufficiency"): s["sufficiency"] = str(v["sufficiency"])
                        if v.get("gap_note") is not None: s["gap_note"] = str(v.get("gap_note", ""))
                        if v.get("action"): s["action"] = str(v["action"])
        except AskCancelled:
            raise
        except Exception:
            pass
        return sections

    # --- Stage B(单节):完整 reasoning 深挖 ---
    def _deep_dive(self, notebook_id, section, question, depth=None, on_step=None):
        # 经模块属性取 ReasoningRetriever(冻结的测试替换位),端口化构造:
        # 深挖拿到的就是本引擎依赖里的同一批 retrieval/model/communities 端口。
        from app.services.reasoning_retrieval import ReasoningRetriever
        deps = self.dependencies
        sec_question = (f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
                        f"[本节检索方向] " + "; ".join(section["sub_queries"]))
        # 与 ask 走同一套流程:不传 top_n → run 按本节方面数自适应证据预算
        # (effective_top_n:floor=retrieval_top_n,横向对比节因兄弟子查询多而扩容)。
        return ReasoningRetriever(
            retrieval=deps.retrieval,
            model_clients=deps.model_clients,
            communities=deps.communities,
            settings=self.settings,
            cancel_event=self.cancel_event,
        ).run(notebook_id, sec_question, on_step=on_step, max_steps=depth)

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result) -> dict:
        from app.services.prompts import report_section_prompt, REPORT_SECTION_SCHEMA_HINT
        deps = self.dependencies
        chunk_block, chunk_map = deps.evidence_context.chunk_context(
            result.chunks, notebook_id=notebook_id,
            budget_chars=self.settings.report_section_chunk_budget)
        kg_block, kg_map = deps.evidence_context.knowledge_context(
            notebook_id, result.top_hits, id_offset=len(chunk_map))
        # 现场事实:chunk_context/knowledge_context 空输入返回 "(none)" 哨兵
        # (非空串),先归一再拼接,避免把哨兵当真实证据块。
        chunk_block = "" if chunk_block == "(none)" else chunk_block
        kg_block = "" if kg_block == "(none)" else kg_block
        context_block = (f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
                         if chunk_block else kg_block) or "(no evidence retrieved)"
        chain_map = {}
        if getattr(result, "chains", None):
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_map = render_follow_chain_context(
                result.chains, id_offset=2000)
            if chain_block and chain_block != "(none)":
                context_block = f"{context_block}\n\n{chain_block}"
        memory_map = {}
        try:
            memories = (
                deps.memory_retriever.notebook_memory_hits(
                    self.user_id, notebook_id,
                    f"{question} {section['title']} {' '.join(section['sub_queries'])}",
                    8,
                )
                if deps.memory_retriever is not None else []
            )
            memory_block, memory_map = (
                deps.memory_retriever.context(memories, id_offset=3000)
                if memories else ("(none)", {})
            )
            if memory_block and memory_block != "(none)":
                context_block = f"{context_block}\n\n[Confirmed Memory]\n{memory_block}"
        except Exception:
            memory_map = {}
        client = deps.model_clients.reasoning_llm_client
        id_map = {**chunk_map, **kg_map, **chain_map, **memory_map}
        # 思考型模型(deepseek-v4-pro)偶发把输出预算耗在 reasoning_content(思维链,被
        # _stream_chat_content 丢弃)上 → content 空 → chat_json 兜底 "{}" → markdown 空
        # (不抛异常)。原先空 markdown 会让本节在 _assemble 里静默消失(无标题/无提示)。
        # 有界重试一次("{}" 不入 LLM 缓存,真·重掷);仍空则标 failed(→渲染「本节生成失败」
        # note,不再静默)+ emit model_error(report_engine 原先零可观测)。章节更长/预算更小
        # (report_section_max_tokens 仅 answer 的一半),故比 ask 更易触发。
        markdown, grounded = "", False
        for _ in range(2):
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": report_section_prompt(
                        section["title"], section["scope"], question, context_block,
                        allow_parametric=self.settings.report_allow_parametric)}],
                    REPORT_SECTION_SCHEMA_HINT, cancel_event=self.cancel_event,
                    **cap_kwargs(client, "report_section_max_tokens"))
                data = json.loads(raw)
                if isinstance(data, dict):
                    markdown = str(data.get("markdown", "")).strip()
                    grounded = bool(data.get("grounded", False))
            except AskCancelled:
                raise
            except Exception:
                markdown, grounded = "", False
            if markdown:
                break
        base = {"title": section["title"], "scope": section["scope"],
                "markdown": markdown, "grounded": grounded,
                "id_map": id_map,      # 节内 k -> ctx;仅供 _assemble 全局重编号,不入库
                "attempted": list(getattr(result, "attempted", []) or [])}
        if not markdown:
            deps.model_errors.note_model_error(
                "report_section",
                self.settings.reasoning_llm_model or self.settings.openai_compat_model,
                RuntimeError(f"report section '{section['title']}' produced empty content after retry "
                             "(reasoning model likely spent output budget on discarded chain-of-thought)"))
            base["failed"] = True
            base["error"] = "答案合成未产出内容(模型可能把输出预算耗在思维链上),已重试"
        return base

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id, rid, outline, question, depth):
        status = [{"title": s["title"], "phase": "排队", "step": 0} for s in outline]
        lock = threading.Lock()
        last = [0.0]

        def persist(force=False):
            now = time.monotonic()
            # 取快照与落库必须同在锁内:放到锁外会让快照顺序 ≠ 落库顺序 —— 先取
            # 快照的线程可能后写,用陈旧快照盖掉别节刚落库的完成态;而
            # _run_sections 之后再没人写 section_status,这份陈旧快照会永久留库
            # (报告已完成,进度视图却停在「规划」/「深挖」)。写被串行化,但
            # 非强制写有 2 秒节流、强制写每节仅 3 次,且都远短于节内 LLM 调用。
            with lock:
                if not force and now - last[0] < 2.0:
                    return
                last[0] = now
                snap = [dict(x) for x in status]
                done = sum(1 for x in snap if x["phase"] in ("完成", "失败"))
                running = sum(1 for x in snap if x["phase"] not in ("排队", "完成", "失败"))
                self.dependencies.reports.update_report(
                    notebook_id, rid, section_status=snap,
                    progress=f"章节 {done}/{len(outline)} 完成 · {running} 进行中")

        _PHASE = {"plan": "规划", "reflect": "深挖", "retrieve": "深挖", "expand": "深挖",
                  "ppr": "深挖", "follow_chain": "深挖", "fallback": "深挖"}

        def _one(i, section):
            raise_if_cancelled(self.cancel_event)
            with lock:
                status[i]["phase"] = "规划"
            persist(force=True)

            def on_step(step, _i=i):
                with lock:
                    ph = _PHASE.get(step.step_type)
                    if ph:
                        status[_i]["phase"] = ph
                    if step.step_type == "reflect":
                        status[_i]["step"] += 1
                persist()

            try:
                result = self._deep_dive(notebook_id, section, question, depth, on_step)
                with lock:
                    status[i]["phase"] = "撰写"
                persist(force=True)
                drafted = self._draft_section(notebook_id, section, question, result)
                with lock:
                    status[i]["phase"] = "完成"
            except AskCancelled:
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                raise
            except Exception as exc:
                drafted = {"title": section["title"], "scope": section["scope"],
                           "markdown": "", "grounded": False, "failed": True,
                           "error": str(exc)[:300], "id_map": {},
                           "attempted": []}
                with lock:
                    status[i]["phase"] = "失败"
            persist(force=True)
            return drafted

        workers = max(1, min(len(outline), int(self.settings.kg_job_concurrency)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _one, i, s)
                       for i, s in enumerate(outline)]
            return [f.result() for f in futures]

    # --- 入口:Stage B/C/D(生成阶段)——读 outline_json → 深挖 → 汇总 → done ---
    def generate(self, notebook_id, rid, question, depth: int = 2) -> None:
        reports = self.dependencies.reports
        try:
            d = reports.get_report(notebook_id, rid)
            outline = d.get("outline") or []
            if not outline:
                reports.update_report(notebook_id, rid, status="failed",
                                      error="no outline to generate", progress="无大纲")
                return
            reports.update_report(notebook_id, rid, status="generating",
                                  progress=f"章节 0/{len(outline)} 完成")
            sections = self._run_sections(notebook_id, rid, outline, question, depth)
            # 中间只写 progress:此刻 sections 仍含 id_map 账目,不落库。
            reports.update_report(notebook_id, rid, progress="汇总中")
            content_md, gaps, references = self._assemble(
                notebook_id, rid, question, outline, sections)
            for s in sections:
                s.pop("id_map", None)          # 账目仅供 assemble,不入库
            reports.update_report(notebook_id, rid, sections=sections,
                                  content_md=content_md, gaps=gaps,
                                  references=references, status="done", progress="完成")
        except AskCancelled:
            reports.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            reports.update_report(notebook_id, rid, status="failed",
                                  error=str(exc)[:500], progress="失败")

    # --- 编排:规划(→outline_ready)+(auto_generate 时)生成,保留一键直出 ---
    def run(self, notebook_id, rid, question, history="", depth: int = 2,
            auto_generate: bool = False) -> None:
        self.plan_outline(notebook_id, rid, question, history)
        if not auto_generate:
            return
        if self.dependencies.reports.get_report(notebook_id, rid).get("status") == "outline_ready":
            self.generate(notebook_id, rid, question, depth)

    # --- Stage D:汇总——执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
    def _assemble(self, notebook_id, rid, question, outline, sections):
        from app.services.prompts import report_summary_prompt
        # 执行摘要(容错:失败则空段,不拖垮报告)。
        summary = ""
        try:
            sections_block = "\n\n".join(
                s["markdown"][:2000] for s in sections if s.get("markdown"))
            raw = self.dependencies.model_clients.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_summary_prompt(question, sections_block)}],
                '{"summary":""}', cancel_event=self.cancel_event)
            summary = str(json.loads(raw).get("summary", "")).strip()
        except AskCancelled:
            raise
        except Exception:
            pass

        # --- 全局引用重编号(普通证据按来源、relation 按边 id 去重) ---
        references: List[dict] = []
        ref_pos: Dict[str, int] = {}       # dedup key -> 全局 1-based

        def _dk(ctx):                       # 普通知识按来源去重;关系证据按边 id 去重
            if str(ctx.get("object_type") or "") == "relation":
                return f"relation:{str(ctx.get('object_id') or '')}"
            return str(ctx.get("source_id") or ctx.get("source_title")
                       or ctx.get("object_id") or "")

        def _label(ctx):
            if str(ctx.get("object_type") or "") == "relation":
                source = str(ctx.get("source_title") or "").strip()
                relation = str(ctx.get("name") or ctx.get("object_id") or "").strip()
                return f"{source} · {relation}" if source and relation else (
                    source or relation or "(unnamed)")
            return (str(ctx.get("source_title") or ctx.get("name")
                        or ctx.get("object_id") or "").strip() or "(unnamed)")

        remapped: Dict[int, str] = {}
        for si, s in enumerate(sections):
            id_map = s.get("id_map") or {}

            def _sub(m, _id_map=id_map):
                # 支持单 key [k1] 与逗号复合 [k1, k3](LLM 常不按 [k1][k3] 而吐逗号):
                # 逐 key 重映射到全局、bracket 内去重;全未知则整段剥除(幻觉/未知 marker)。
                # 复合 marker 是一个证据组：先完整验证，再产生任何全局编号副作用。
                local_keys = [_raw.strip() for _raw in m.group(1).split(",")]
                contexts = [_id_map.get(key) for key in local_keys]
                if any(not ctx for ctx in contexts):
                    return ""

                out_keys: List[str] = []
                for ctx in contexts:
                    dk = _dk(ctx)
                    if dk not in ref_pos:
                        ref_pos[dk] = len(references) + 1
                        references.append({
                            "key": f"k{ref_pos[dk]}",
                            "object_id": str(ctx.get("object_id") or ""),
                            "object_type": str(ctx.get("object_type") or ""),
                            "label": _label(ctx),
                            "name": str(ctx.get("name") or ""),
                            "source_title": str(ctx.get("source_title") or ""),
                            "location_label": str(ctx.get("location_label") or ""),
                            "tier": str(ctx.get("tier") or "personal"),
                            "provenance": dict(ctx.get("provenance") or {}),
                        })
                    _gk = f"k{ref_pos[dk]}"
                    if _gk not in out_keys:
                        out_keys.append(_gk)
                return ("[" + ", ".join(out_keys) + "]") if out_keys else ""

            remapped[si] = _MARKER.sub(_sub, s.get("markdown") or "")

        # --- 覆盖度信号(仅高信号:库内证据不足的章节;供未来「覆盖度」面板)。
        # 报告体例要求:正文不堆砌诊断。故此处只留「哪些节缺库内支撑」这一条
        # 可读性最高、开销为零的信号;结尾附一行「局限」。
        # 已移除:①干涸子查询罗列(暴露英文子查询,内部机制)②跨节概念对连通性
        # 检查(大库 _retrieve_neighbors 开销 + claim 文本被当概念名 → 大面积噪音)。
        weak = [s["title"] for s in sections
                if s.get("markdown") and not s.get("grounded")]
        gaps = [f"「{t}」库内证据不足,内容偏推断/通识" for t in weak]

        # --- 组装 content_md:执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
        parts = [f"# 深度报告:{question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        for si, s in enumerate(sections):
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif remapped.get(si):
                parts += [remapped[si], ""]
        if references:
            parts += ["## 参考文献", ""] + [
                f"- [{r['key']}] {r['label']}"
                + (f" · {r['location_label']}" if r["location_label"] else "")
                for r in references] + [""]
        if weak:
            parts += [f"> **局限**:{'、'.join(weak)} 库内证据有限,相关论述以推断/"
                      "通识为主,建议补充对应语料后重新生成。", ""]
        return "\n".join(parts), gaps, references
