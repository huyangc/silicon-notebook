"""深度报告引擎:大纲规划 → 每节完整 reasoning 深挖(节间并行) → 逐节撰写
(证据三层 [k]/（推断）/【通识】) → 汇总(执行摘要/参考文献/结尾局限)。

设计对齐 docs/superpowers/specs/2026-07-03-deep-report-mode-design.md。
形态镜像 ReasoningRetriever:持 (repo, settings, cancel_event),写库经 repo。
线程要点:节间 ThreadPoolExecutor 并行,worker 不继承 ContextVar——每个 submit
用 contextvars.copy_context().run 包裹,保住 per-user 模型解析。
"""
from __future__ import annotations
import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.core.llm import cap_kwargs
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled

_MARKER = re.compile(r"\[(k\d+(?:\s*,\s*k\d+)*)\]")   # 节内 [k_i] 或 [k_i, k_j] 引用标记(全局重编号用)

# --- 取消注册表:report_id → threading.Event(活动后台 job 才在册) ---
_ACTIVE_CANCELS: Dict[str, threading.Event] = {}
_CANCELS_LOCK = threading.Lock()


def register_cancel(report_id: str) -> threading.Event:
    ev = threading.Event()
    with _CANCELS_LOCK:
        _ACTIVE_CANCELS[report_id] = ev
    return ev


def cancel_report(report_id: str) -> bool:
    with _CANCELS_LOCK:
        ev = _ACTIVE_CANCELS.get(report_id)
    if ev is not None:
        ev.set()
        return True
    return False


def unregister_cancel(report_id: str) -> None:
    with _CANCELS_LOCK:
        _ACTIVE_CANCELS.pop(report_id, None)


class ReportEngine:
    def __init__(self, repo, settings, cancel_event: CancelEvent = None):
        self.repo = repo
        self.settings = settings
        self.cancel_event = cancel_event

    # --- Stage A ---
    def _plan_outline(self, notebook_id: str, question: str, history: str) -> List[dict]:
        from app.services.prompts import report_outline_prompt, REPORT_OUTLINE_SCHEMA_HINT
        client = self.repo.reasoning_llm_client
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
        ex = expand_query(self.repo.rewrite_llm_client, question, history)
        return [{"title": "分析", "scope": question,
                 "sub_queries": [s.query for s in ex.sub_queries][:4] or [question]}]

    # --- Stage A(STORM):Corpus map 0-LLM 语料侦察 ---
    _SCOUT_KG_N = 12
    _SCOUT_CHUNK_N = 8

    def _build_corpus_map(self, notebook_id: str, question: str) -> str:
        """0-LLM 语料侦察:来源标题 + federated KG 命中 + PPR chunk 来源·路径。
        给 STORM 规划接地(治盲规划)。任一子步失败静默降级为空段。"""
        parts: List[str] = []
        try:
            with self.repo._connect() as db:
                rows = db.execute(
                    "SELECT title FROM sources WHERE notebook_id=? ORDER BY created_at LIMIT 20",
                    (notebook_id,)).fetchall()
            titles = [str(r["title"]).strip() for r in rows if str(r["title"]).strip()]
            if titles:
                parts.append("本 notebook 来源文件:\n" + "\n".join(f"- {t}" for t in titles))
        except Exception:
            pass
        try:
            kg = self.repo.federated_retrieve(notebook_id, question)[: self._SCOUT_KG_N]
            if kg:
                parts.append("检索到的知识条目(name[type][tier]):\n" + "\n".join(
                    f"- {str(h.payload.get('name','')).strip()}"
                    f"[{h.object_type}][{getattr(h,'tier','personal')}]" for h in kg))
        except Exception:
            pass
        try:
            chunks = self.repo._ppr_retrieve(notebook_id, question)[: self._SCOUT_CHUNK_N]
            if chunks:
                parts.append("相关原文所在(来源·章节,不含正文):\n" + "\n".join(
                    f"- {c.source_title} · {c.section_path}" for c in chunks))
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
                    for h in self.repo.federated_retrieve(notebook_id, str(q)):
                        seen.add(h.object_id)
                        if getattr(h, "tier", "") == "base":
                            base.add(h.object_id)
                except Exception:
                    continue
            out.append({"title": s.get("title", ""), "hits": len(seen), "base_hits": len(base)})
        return out

    # --- Stage A 编排:map → STORM → 探针 → Judge → 富大纲 → outline_ready ---
    def plan_outline(self, notebook_id, rid, question, history="") -> None:
        try:
            self.repo.update_report(notebook_id, rid, status="planning", progress="侦察语料中")
            corpus_map = self._build_corpus_map(notebook_id, question)
            raise_if_cancelled(self.cancel_event)
            self.repo.update_report(notebook_id, rid, progress="多视角规划大纲中")
            sections = self._storm_outline(notebook_id, question, history, corpus_map)
            # 充分性:探针(0 LLM)+ Judge(flash)
            probe = self._probe_sufficiency(notebook_id, sections)
            sections = self._judge_sufficiency(question, sections, probe)
            self.repo.update_report(notebook_id, rid, outline=sections,
                                    status="outline_ready",
                                    progress=f"大纲就绪({len(sections)} 节),待确认")
        except AskCancelled:
            self.repo.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            self.repo.update_report(notebook_id, rid, status="failed",
                                    error=str(exc)[:500], progress="规划失败")

    def _storm_outline(self, notebook_id, question, history, corpus_map) -> List[dict]:
        from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
        try:
            raw = self.repo.reasoning_llm_client.chat_json(
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
            raw = self.repo.rewrite_llm_client.chat_json(
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
        from app.services.reasoning_retrieval import ReasoningRetriever
        sec_question = (f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
                        f"[本节检索方向] " + "; ".join(section["sub_queries"]))
        # 与 ask 走同一套流程:不传 top_n → run 按本节方面数自适应证据预算
        # (effective_top_n:floor=retrieval_top_n,横向对比节因兄弟子查询多而扩容)。
        return ReasoningRetriever(self.repo, self.settings, self.cancel_event).run(
            notebook_id, sec_question, on_step=on_step, max_steps=depth)

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result) -> dict:
        from app.services.prompts import report_section_prompt, REPORT_SECTION_SCHEMA_HINT
        chunk_block, chunk_map = self.repo._chunk_answer_context(
            result.chunks, budget_chars=self.settings.report_section_chunk_budget,
            notebook_id=notebook_id)
        kg_block, kg_map = self.repo._answer_context(
            notebook_id, result.top_hits, id_offset=len(chunk_map))
        # 现场事实:_chunk_answer_context/_answer_context 空输入返回 "(none)" 哨兵
        # (非空串),先归一再拼接,避免把哨兵当真实证据块。
        chunk_block = "" if chunk_block == "(none)" else chunk_block
        kg_block = "" if kg_block == "(none)" else kg_block
        context_block = (f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
                         if chunk_block else kg_block) or "(no evidence retrieved)"
        client = self.repo.reasoning_llm_client
        raw = client.chat_json(
            [{"role": "user", "content": report_section_prompt(
                section["title"], section["scope"], question, context_block,
                allow_parametric=self.settings.report_allow_parametric)}],
            REPORT_SECTION_SCHEMA_HINT, cancel_event=self.cancel_event,
            **cap_kwargs(client, "report_section_max_tokens"))
        data = json.loads(raw)
        id_map = {**chunk_map, **kg_map}
        return {"title": section["title"], "scope": section["scope"],
                "markdown": str(data.get("markdown", "")).strip(),
                "grounded": bool(data.get("grounded", False)),
                "id_map": id_map,      # 节内 k -> ctx;仅供 _assemble 全局重编号,不入库
                "attempted": list(getattr(result, "attempted", []) or [])}

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id, rid, outline, question, depth):
        status = [{"title": s["title"], "phase": "排队", "step": 0} for s in outline]
        lock = threading.Lock()
        last = [0.0]

        def persist(force=False):
            now = time.monotonic()
            with lock:
                if not force and now - last[0] < 2.0:
                    return
                last[0] = now
                snap = [dict(x) for x in status]
            done = sum(1 for x in snap if x["phase"] in ("完成", "失败"))
            running = sum(1 for x in snap if x["phase"] not in ("排队", "完成", "失败"))
            self.repo.update_report(
                notebook_id, rid, section_status=snap,
                progress=f"章节 {done}/{len(outline)} 完成 · {running} 进行中")

        _PHASE = {"plan": "规划", "reflect": "深挖", "retrieve": "深挖", "expand": "深挖",
                  "ppr": "深挖", "fallback": "深挖"}

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
        try:
            d = self.repo.get_report(notebook_id, rid)
            outline = d.get("outline") or []
            if not outline:
                self.repo.update_report(notebook_id, rid, status="failed",
                                        error="no outline to generate", progress="无大纲")
                return
            self.repo.update_report(notebook_id, rid, status="generating",
                                    progress=f"章节 0/{len(outline)} 完成")
            sections = self._run_sections(notebook_id, rid, outline, question, depth)
            # 中间只写 progress:此刻 sections 仍含 id_map 账目,不落库。
            self.repo.update_report(notebook_id, rid, progress="汇总中")
            content_md, gaps, references = self._assemble(
                notebook_id, rid, question, outline, sections)
            for s in sections:
                s.pop("id_map", None)          # 账目仅供 assemble,不入库
            self.repo.update_report(notebook_id, rid, sections=sections,
                                    content_md=content_md, gaps=gaps,
                                    references=references, status="done", progress="完成")
        except AskCancelled:
            self.repo.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            self.repo.update_report(notebook_id, rid, status="failed",
                                    error=str(exc)[:500], progress="失败")

    # --- 编排:规划(→outline_ready)+(auto_generate 时)生成,保留一键直出 ---
    def run(self, notebook_id, rid, question, history="", depth: int = 2,
            auto_generate: bool = False) -> None:
        self.plan_outline(notebook_id, rid, question, history)
        if not auto_generate:
            return
        if self.repo.get_report(notebook_id, rid).get("status") == "outline_ready":
            self.generate(notebook_id, rid, question, depth)

    # --- Stage D:汇总——执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
    def _assemble(self, notebook_id, rid, question, outline, sections):
        from app.services.prompts import report_summary_prompt
        # 执行摘要(容错:失败则空段,不拖垮报告)。
        summary = ""
        try:
            sections_block = "\n\n".join(
                s["markdown"][:2000] for s in sections if s.get("markdown"))
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_summary_prompt(question, sections_block)}],
                '{"summary":""}', cancel_event=self.cancel_event)
            summary = str(json.loads(raw).get("summary", "")).strip()
        except AskCancelled:
            raise
        except Exception:
            pass

        # --- 全局引用重编号(按来源去重):节内 [k_i] → 全局 [k{N}] ---
        references: List[dict] = []
        ref_pos: Dict[str, int] = {}       # dedup key -> 全局 1-based

        def _dk(ctx):                       # 去重键:source_id > source_title > object_id
            return str(ctx.get("source_id") or ctx.get("source_title")
                       or ctx.get("object_id") or "")

        def _label(ctx):
            return (str(ctx.get("source_title") or ctx.get("name")
                        or ctx.get("object_id") or "").strip() or "(unnamed)")

        remapped: Dict[int, str] = {}
        for si, s in enumerate(sections):
            id_map = s.get("id_map") or {}

            def _sub(m, _id_map=id_map):
                # 支持单 key [k1] 与逗号复合 [k1, k3](LLM 常不按 [k1][k3] 而吐逗号):
                # 逐 key 重映射到全局、bracket 内去重;全未知则整段剥除(幻觉/未知 marker)。
                out_keys: List[str] = []
                for _raw in m.group(1).split(","):
                    ctx = _id_map.get(_raw.strip())
                    if not ctx:
                        continue
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
