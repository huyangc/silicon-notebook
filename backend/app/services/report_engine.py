"""深度报告引擎:大纲规划 → 每节完整 reasoning 深挖(节间并行) → 逐节撰写
(证据三层 [k]/（推断）/【通识】) → 汇总(执行摘要/参考/知识缺口/分析计划)。

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
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.core.llm import cap_kwargs
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled

_GAP_PAIR_CAP = 40          # 跨节概念连通性检查的最大 pair 数(成本护栏)
_TOP_CONCEPTS_PER_SECTION = 3
_MARKER = re.compile(r"\[k(\d+)\]")   # 节内 [k_i] 引用标记(全局重编号用)

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

    # --- Stage B(单节):完整 reasoning 深挖 ---
    def _deep_dive(self, notebook_id: str, section: dict, question: str):
        from app.services.reasoning_retrieval import ReasoningRetriever
        sec_question = (f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
                        f"[本节检索方向] " + "; ".join(section["sub_queries"]))
        return ReasoningRetriever(self.repo, self.settings, self.cancel_event).run(
            notebook_id, sec_question, top_n=self.settings.report_section_top_n)

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result) -> dict:
        from app.services.prompts import report_section_prompt, REPORT_SECTION_SCHEMA_HINT
        chunk_block, chunk_map = self.repo._chunk_answer_context(
            result.chunks, budget_chars=self.settings.report_section_chunk_budget)
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
                "attempted": list(getattr(result, "attempted", []) or []),
                "top_concepts": [
                    {"object_id": h.object_id,
                     "name": str(h.payload.get("name", "")).strip() or h.object_id}
                    for h in result.top_hits[:_TOP_CONCEPTS_PER_SECTION]]}

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id: str, rid: str, outline: List[dict],
                      question: str) -> List[dict]:
        done_count = {"n": 0}

        def _one(section: dict) -> dict:
            raise_if_cancelled(self.cancel_event)
            try:
                result = self._deep_dive(notebook_id, section, question)
                drafted = self._draft_section(notebook_id, section, question, result)
            except AskCancelled:
                raise
            except Exception as exc:
                drafted = {"title": section["title"], "scope": section["scope"],
                           "markdown": "", "grounded": False, "failed": True,
                           "error": str(exc)[:300], "id_map": {},
                           "attempted": [], "top_concepts": []}
            done_count["n"] += 1
            self.repo.update_report(notebook_id, rid,
                                    progress=f"章节深挖 {done_count['n']}/{len(outline)}")
            return drafted

        workers = max(1, int(self.settings.kg_job_concurrency))
        with ThreadPoolExecutor(max_workers=min(workers, len(outline))) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _one, s)
                       for s in outline]
            return [f.result() for f in futures]     # 保大纲序

    # --- 入口 ---
    def run(self, notebook_id: str, rid: str, question: str, history: str = "") -> None:
        try:
            self.repo.update_report(notebook_id, rid, status="running", progress="大纲规划中")
            outline = self._plan_outline(notebook_id, question, history)
            self.repo.update_report(notebook_id, rid, outline=outline,
                                    progress=f"大纲就绪({len(outline)} 节),章节深挖 0/{len(outline)}")
            sections = self._run_sections(notebook_id, rid, outline, question)
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

    # --- Stage D:汇总——执行摘要 + 全局引用 + 知识缺口 + 分析计划 ---
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
                ctx = _id_map.get(f"k{m.group(1)}")
                if not ctx:
                    return ""               # 剥除幻觉/未知 marker
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
                return f"[k{ref_pos[dk]}]"

            remapped[si] = _MARKER.sub(_sub, s.get("markdown") or "")

        # --- 知识缺口(逻辑不变,concept 连通性仍用 top_concepts) ---
        gaps: List[str] = []
        # 缺口一:零命中/干涸子查询(each 节 attempted 里 new==0)。
        for s in sections:
            for a in s.get("attempted", []):
                if a.get("new") == 0:
                    gaps.append(f"「{s['title']}」节:子查询 “{a['query']}” 在库内未检得新证据")
        # 缺口二:跨节高相关概念对在 KG 中无边(结构性缺口)。
        pairs_checked = 0
        concepts = [(s["title"], c) for s in sections for c in s.get("top_concepts", [])]
        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                if concepts[i][0] == concepts[j][0]:
                    continue                     # 只查跨节
                if pairs_checked >= _GAP_PAIR_CAP:
                    break
                pairs_checked += 1
                a, b = concepts[i][1], concepts[j][1]
                try:
                    neigh = self.repo._retrieve_neighbors(notebook_id, a["object_id"],
                                                          None, "both")
                except Exception:
                    continue
                if not any(h.object_id == b["object_id"] for h in neigh):
                    gaps.append(f"图谱缺口:「{a['name']}」与「{b['name']}」尚无关联边")
        # 缺口三:整节无 [k] 支撑。
        for s in sections:
            if s.get("markdown") and not s.get("grounded"):
                gaps.append(f"「{s['title']}」节无库内引用支撑(全部为推断/通识,建议补充语料)")
        gaps = list(dict.fromkeys(gaps))[:30]

        # --- 组装 content_md(用重编号后的节 markdown) ---
        plan_lines = [
            f"- {s['title']}: " + "; ".join(o.get("sub_queries", []))
            for s, o in zip(sections, outline)]
        parts = [f"# 深度报告:{question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        for si, s in enumerate(sections):
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif remapped.get(si):
                parts += [remapped[si], ""]
        if gaps:
            parts += ["## 知识缺口", ""] + [f"- {g}" for g in gaps] + [""]
        if references:
            parts += ["## 参考文献", ""] + [
                f"- [{r['key']}] {r['label']}"
                + (f" · {r['location_label']}" if r["location_label"] else "")
                for r in references] + [""]
        parts += ["## 分析计划", ""] + plan_lines
        return "\n".join(parts), gaps, references
