"""Evidence/context composition for Ask answer synthesis.

This module owns the stable ``[k_i]`` assignment and reverse binding contract.
It deliberately does not synthesize model answers and performs no maintenance;
its collaborators are narrow read stores only.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

from app.core.config import Settings
from app.models.ask import AnswerAnchor, Citation, CitationKnowhowRef
from app.repositories.ports import (
    EvidenceKnowledgeContextPort, NotebookStorePort, SourceStorePort,
)
from app.services.retrieval import (
    RetrievedChunk, RetrievedElement, RetrievedKnowledge, est_tokens,
)


_MARKER_GROUP_RE = re.compile(r"\[((?:k\d+\s*,\s*)*k\d+)\]")


def _knowhow_ref(element_row: Mapping[str, Any] | None) -> CitationKnowhowRef | None:
    """Task 12（引用跳转）: 从 `evidence_elements()` 返回的一行里取出
    `metadata.knowhow.{table_id,row_id}`，命中才建 ref，其余（非 knowhow 元素/
    元素已不存在/metadata 解析失败）一律安全返回 None——从不抛异常。"""
    if not element_row:
        return None
    try:
        metadata = json.loads(element_row.get("metadata") or "{}")
    except (TypeError, ValueError):
        return None
    knowhow = metadata.get("knowhow") if isinstance(metadata, dict) else None
    if not isinstance(knowhow, dict):
        return None
    table_id, row_id = knowhow.get("table_id"), knowhow.get("row_id")
    if not table_id or not row_id:
        return None
    return CitationKnowhowRef(table_id=table_id, row_id=row_id)


def _knowhow_ref_from_payload(payload: Mapping[str, Any]) -> CitationKnowhowRef | None:
    """Task 12b（引用跳转扩面，锚点侧）: 从知识对象（KO）的 payload 里取
    `table_id`/`rows`（`KnowhowProjector._ko_object_row` 写入的 §④ payload
    shape，见 projection.py 同名 docstring）算出锚点的 knowhow 定位。

    合并行规则（controller 决策，T12b brief）: 只有 `len(rows) == 1` 时才有
    唯一无歧义的行——被多行共提合并的同一个 KO（例如被 10 行同时引用的"该
    工具"）没有单一行可跳，此时诚实地留 None（前端不出「在表格中查看」按钮），
    不猜第一行。非 knowhow KO（payload 里没有 rows/table_id）同样安全返回
    None——从不抛异常，镜像 `_knowhow_ref` 的防御风格。"""
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    table_id, row_id = payload.get("table_id"), rows[0]
    if not table_id or not row_id:
        return None
    return CitationKnowhowRef(table_id=str(table_id), row_id=str(row_id))


class EvidenceContextService:
    def __init__(
        self,
        *,
        notebooks: NotebookStorePort,
        sources: SourceStorePort,
        knowledge: EvidenceKnowledgeContextPort,
        settings: Settings,
    ) -> None:
        self.notebooks = notebooks
        self.sources = sources
        self.knowledge = knowledge
        self.settings = settings

    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]:
        return self.notebooks.tier_map(notebook_ids)

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Return bounded source classification for retrieval-only consumers."""
        return self.sources.source_metadata(source_ids)

    def citation_titles(self, source_ids: Iterable[str]) -> dict[str, str]:
        """Resolve user-facing citation titles in one bounded source lookup.

        A grounded paper title is more meaningful than its upload/file name.  It
        is authoritative for citation display only when the metadata row marks
        the source as a paper and the parsed title is nonblank.  Every other
        source retains its ordinary source title (then file name as fallback).
        """
        ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        if not ids:
            return {}
        metadata = self.source_metadata(ids)
        titles: dict[str, str] = {}
        for source_id in ids:
            row = metadata.get(source_id) or {}
            paper_title = str(row.get("paper_title") or "").strip()
            ordinary_title = str(row.get("title") or row.get("file_name") or "").strip()
            title = paper_title if bool(row.get("is_paper")) and paper_title else ordinary_title
            if title:
                titles[source_id] = title
        return titles

    def chunk_context(
        self,
        chunks: Sequence[RetrievedChunk],
        *,
        notebook_id: str,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        budget = (
            self.settings.chunk_answer_budget_chars
            if budget_chars is None
            else budget_chars
        )
        tiers = self.tier_map(
            list({getattr(chunk, "notebook_id", "") or notebook_id for chunk in chunks})
        )
        citation_titles = self.citation_titles(chunk.source_id for chunk in chunks)
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        # Task 12b 评审修复（grounded 主路径可达性）：chunk 锚点也要带 knowhow。
        # 前端 buildAnswerReferences 是 anchor 优先的全有全无——答案里只要有
        # 一个 [k] 命中，引用列表就整体走 anchor 分支，citation.knowhow 永远
        # 被遮蔽；而 chunk 锚点（所有模式 grounded 答案的主体）此前从不带
        # knowhow → grounded 的 chunk 模式答案（answer_prompt 要求每句有据
        # 都标 [k]）里「在表格中查看」按钮永远不出现。这里沿用共享的
        # knowhow_refs_for 按 element_id 批量解一次（每次组装恰好一次 store
        # 读取，PK 上的有界 IN 查询，条数 ≤ 本次入选 chunk 数——运行效率是
        # 一等约束）。只对单 element 的 chunk 生效：knowhow 投影的格子 chunk
        # 恒为单 element（projection.py 每格一元素），多 element 的普通文档
        # chunk 天然不可能是 knowhow 格子，防御性跳过。
        single_element_keys: dict[str, str] = {}
        used = 0
        for index, chunk in enumerate(chunks, 1):
            if used >= budget and lines:
                break
            key = f"k{index}"
            source_title = citation_titles.get(chunk.source_id, chunk.source_title)
            line = f"{key}: {chunk.text}"
            lines.append(line)
            used += len(line)
            # raw_origin: chunk.notebook_id 的原始值;origin 另外回退本次 ask 的
            # notebook_id 供 tier 查表用(同库 chunk 也要查得到 tier)。徽章库名
            # 映射(Task 14)要的是"真正跨库才非空"的 raw_origin——但联邦/PPR 检索
            # (federated_retrieve/_ppr_retrieve)对本库命中同样会把 notebook_id
            # 打成 active 自己的 id,并非只在跨库命中时才打标(codex r2 review 修
            # 正此前的错误假设),故显式比较 notebook_id 归零,镜像
            # follow_chain.py 的 `hop.notebook_id != active_notebook_id` 处理,
            # 否则前端会把"本库自己"解出一个多余的"来自「当前笔记本」"徽章。
            raw_origin = getattr(chunk, "notebook_id", "") or ""
            origin = raw_origin or notebook_id
            if raw_origin == notebook_id:
                raw_origin = ""
            element_ids = getattr(chunk, "element_ids", None) or []
            evidence_by_id[key] = {
                "object_id": chunk.chunk_id,
                "object_type": "chunk",
                "name": chunk.section_path or source_title,
                "definition": None,
                "snippet": chunk.text[:300],
                "source_title": source_title,
                "location_label": chunk.section_path,
                "tier": tiers.get(origin, "personal"),
                "notebook_id": raw_origin,
                "source_id": chunk.source_id,
                "element_id": element_ids[0] if len(element_ids) == 1 else "",
                "relevance": float(getattr(chunk, "relevance", 0.0) or 0.0),
                "knowhow": None,
            }
            if len(element_ids) == 1:
                single_element_keys[key] = element_ids[0]
        if single_element_keys:
            refs = self.knowhow_refs_for(single_element_keys.values())
            for key, element_id in single_element_keys.items():
                evidence_by_id[key]["knowhow"] = refs.get(element_id)
        return ("\n".join(lines) if lines else "(none)"), evidence_by_id

    def element_context(
        self,
        elements: Sequence[RetrievedElement],
        *,
        notebook_id: str,
        id_offset: int = 4000,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        """Render retrieved ``SourceElement`` rows as first-class citable evidence.

        Elements are already the parser's smallest citation unit.  Keeping their
        ids in the reverse map prevents the report/Ask layers from collapsing a
        precise passage back to a source-level reference.
        """
        budget = (
            self.settings.answer_context_budget_chars
            if budget_chars is None else max(0, int(budget_chars))
        )
        tier = self.tier_map([notebook_id]).get(notebook_id, "personal")
        citation_titles = self.citation_titles(element.source_id for element in elements)
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        used = 0
        for index, element in enumerate(elements, 1):
            key = f"k{id_offset + index}"
            location = element.location_label or element.element_type
            source_title = citation_titles.get(element.source_id, element.source_title)
            prefix = (
                f"{key}: [source-element][{tier}] {source_title} · "
                f"{location} — "
            )
            remaining = budget - used - len(prefix)
            if remaining <= 0:
                break
            text = element.text[:remaining]
            if len(text) < len(element.text) and remaining > 1:
                text = text[:-1] + "…"
            line = prefix + text
            lines.append(line)
            used += len(line)
            evidence_by_id[key] = {
                "object_id": element.element_id,
                "object_type": "element",
                "name": location or source_title,
                "definition": element.text,
                "snippet": element.text[:300],
                "source_id": element.source_id,
                "element_id": element.element_id,
                "source_title": source_title,
                "location_label": location,
                "tier": tier,
                "notebook_id": "",
                "relevance": float(element.score or 0.0),
                "knowhow": None,
            }
        if evidence_by_id:
            refs = self.knowhow_refs_for(
                value["element_id"] for value in evidence_by_id.values()
            )
            for value in evidence_by_id.values():
                value["knowhow"] = refs.get(value["element_id"])
        return ("\n".join(lines) if lines else "(none)"), evidence_by_id

    def knowledge_context(
        self,
        notebook_id: str,
        hits: Sequence[RetrievedKnowledge],
        *,
        id_offset: int = 0,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        budget = self.settings.answer_context_budget_chars
        min_items = self.settings.answer_context_min_items
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        seen_clusters: set[str] = set()
        participants = self.notebooks.participant_notebook_ids(notebook_id)
        cluster_map: dict[str, str] = {}
        for participant in participants:
            cluster_map.update(self.knowledge.cluster_map(participant))

        used = 0
        next_id = 0
        for hit in hits:
            cluster_id = cluster_map.get(hit.object_id, hit.object_id)
            if cluster_id in seen_clusters:
                continue
            seen_clusters.add(cluster_id)
            # raw_origin: hit.notebook_id 的原始值,供 Task 14 的引用徽章库名映射
            # 用;origin 另外回退本次 ask 的 notebook_id,供 node_context 查询用
            # (同库命中也要查得到详情)。徽章要的是"真正跨库才非空"的
            # raw_origin——但 federated_retrieve 对本库命中同样会把 notebook_id
            # 打成 active 自己的 id(并非只在跨库命中时才打标,codex r2 review 修
            # 正此前的错误假设),故显式比较 notebook_id 归零,镜像
            # follow_chain.py 的 `hop.notebook_id != active_notebook_id` 处理。
            raw_origin = getattr(hit, "notebook_id", "") or ""
            origin = raw_origin or notebook_id
            if raw_origin == notebook_id:
                raw_origin = ""
            try:
                context = self.knowledge.node_context(origin, hit.object_id)
            except KeyError:
                continue
            if used >= budget and len(lines) >= min_items:
                break
            next_id += 1
            key = f"k{next_id + id_offset}"
            name = str(hit.payload.get("name", "")).strip()
            occurrences = context.get("occurrences") or []
            snippet = occurrences[0].get("element_text") if occurrences else ""
            definition = context.get("definition") or snippet
            remaining = max(0, budget - used)
            definition_cap = max(0, min(300, remaining))
            extra = (
                f" — def: {definition[:definition_cap]}"
                if definition and definition_cap
                else ""
            )
            if context.get("steps") and definition_cap:
                extra += "; steps: " + " -> ".join(
                    step.get("name", "") for step in context["steps"][:8]
                )
            tier = getattr(hit, "tier", "personal")
            line = f"{key}: [{hit.object_type}][{tier}] {name}{extra}"
            lines.append(line)
            used += len(line)
            evidence_by_id[key] = {
                "object_id": hit.object_id,
                "object_type": hit.object_type,
                "name": name,
                "definition": definition,
                "snippet": snippet,
                "source_title": occurrences[0].get("source_title", "") if occurrences else "",
                "location_label": occurrences[0].get("section_path", "") if occurrences else "",
                "tier": tier,
                "notebook_id": raw_origin,
                "source_id": str(occurrences[0].get("source_id", "")) if occurrences else "",
                "element_id": str(occurrences[0].get("element_id", "")) if occurrences else "",
                "relevance": float(getattr(hit, "relevance", 0.0) or 0.0),
                # Task 12b（引用跳转扩面，锚点侧）: KO payload 已在内存里（同
                # 上面 hit.payload.get("name","") 的零额外查询惯例），命中单行
                # knowhow 格子才有值，其余（非 knowhow KO/合并多行）为 None。
                "knowhow": _knowhow_ref_from_payload(hit.payload),
            }

        preferred_titles = self.citation_titles(
            value.get("source_id", "") for value in evidence_by_id.values()
        )
        for value in evidence_by_id.values():
            source_id = str(value.get("source_id") or "")
            if source_id in preferred_titles:
                value["source_title"] = preferred_titles[source_id]

        object_to_key = {value["object_id"]: key for key, value in evidence_by_id.items()}
        if len(object_to_key) >= 2:
            relation_rows = self.knowledge.in_network_relations(
                participants, list(object_to_key)
            )
            seen_relations: set[tuple[str, str, str]] = set()
            ranked: list[tuple[str, str, str, str, str, str, int]] = []
            for row in relation_rows:
                source_key = object_to_key.get(row["source_object_id"])
                target_key = object_to_key.get(row["target_object_id"])
                identity = (source_key or "", row["edge_type"], target_key or "")
                if not source_key or not target_key or source_key == target_key or identity in seen_relations:
                    continue
                seen_relations.add(identity)
                support = self.knowledge.relation_support_count(
                    row["notebook_id"], row["source_object_id"], row["edge_type"],
                    row["target_object_id"],
                )
                ranked.append((*identity, row["notebook_id"], row["source_object_id"],
                               row["target_object_id"], support))
            ranked.sort(key=lambda row: row[-1], reverse=True)
            if ranked:
                rendered = []
                for source_key, edge_type, target_key, _nb, _source, _target, support in ranked[:30]:
                    suffix = f" (×{support}源)" if support >= 2 else ""
                    rendered.append(f"{source_key} -[{edge_type}]-> {target_key}{suffix}")
                lines.append("relations: " + "; ".join(rendered))
        return ("\n".join(lines) if lines else "(none)"), evidence_by_id

    def parse_anchors(
        self,
        answer: str,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[AnswerAnchor]:
        anchors: list[AnswerAnchor] = []
        seen: set[str] = set()
        marker_groups = _MARKER_GROUP_RE.findall(answer or "")
        cited_keys = list(dict.fromkeys(
            key
            for marker_group in marker_groups
            for key in (part.strip() for part in marker_group.split(","))
            if key in evidence_by_id
        ))
        preferred_titles = self.citation_titles(
            str(evidence_by_id[key].get("source_id") or "") for key in cited_keys
        )
        for marker_group in marker_groups:
            keys = [part.strip() for part in marker_group.split(",")]
            if not keys or any(key not in evidence_by_id for key in keys):
                continue
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                context = evidence_by_id[key]
                name = str(context.get("name", ""))
                anchors.append(AnswerAnchor(
                    key=key,
                    object_id=str(context["object_id"]),
                    object_type=str(context["object_type"]),
                    label=name[:40] or key,
                    name=name,
                    definition=context.get("definition"),
                    snippet=context.get("snippet"),
                    source_title=preferred_titles.get(
                        str(context.get("source_id") or ""),
                        str(context.get("source_title", "")),
                    ),
                    location_label=str(context.get("location_label", "")),
                    tier=str(context.get("tier", "personal")),
                    # Task 14: 只有 chunk_context/knowledge_context 填了才非空
                    # (render_subgraph_context 的纯 graph-BFS 节点暂未填,`.get`
                    # 安全回退空串,徽章优雅退回泛化 tier 文案,不崩不猜)。
                    notebook_id=str(context.get("notebook_id", "")),
                    provenance=dict(context.get("provenance") or {}),
                    # Task 12b: 只有 knowledge_context 建的 evidence_by_id 才带
                    # "knowhow" 键；chunk_context/记忆上下文没有这个键，`.get`
                    # 安全回退 None（既有 exclude_if 惯例下从 JSON 整体缺席）。
                    knowhow=context.get("knowhow"),
                ))
        return anchors

    def knowhow_refs_for(self, element_ids: Iterable[str]) -> dict[str, CitationKnowhowRef]:
        """Task 12b（引用跳转扩面）: `citations_from` 与 ask_service.py 四个
        chunk/graph 内联 `Citation(...)` 构造点共用的批量 knowhow 定位查询——
        不管调用方一次要建多少条引用，只发生一次 `evidence_elements()` 读取
        (运行效率是一等约束，见仓库 memory)。入参可以带空串/重复 id（各内联
        调用点直接从 `c.element_ids[0] if c.element_ids else ""` 取值，不必
        先自行过滤），本方法自己去重 + 丢弃假值。返回 {element_id: ref} 映射，
        只收命中的——调用方对未命中的 element_id 用 `.get(eid)` 拿回 None，
        与既有 `Citation.knowhow`/`exclude_if` 惯例一致。"""
        ids = list(dict.fromkeys(eid for eid in element_ids if eid))
        if not ids:
            return {}
        elements = self.sources.evidence_elements(ids)
        refs: dict[str, CitationKnowhowRef] = {}
        for eid in ids:
            ref = _knowhow_ref(elements.get(eid))
            if ref is not None:
                refs[eid] = ref
        return refs

    def citations_from(
        self,
        hits: Sequence[RetrievedKnowledge],
        valid_element_ids: set[str],
        label: str,
        *,
        notebook_id: str,
    ) -> list[Citation]:
        filtered: list[tuple[str, str, Any]] = []
        for hit in hits:
            tier = getattr(hit, "tier", "personal") or "personal"
            # Task 14 codex r4 fix: hit.notebook_id 的原始值同样会被联邦检索
            # (_federated_retrieve_impl)对 active 库自己的命中打上 active 自己
            # 的 id——resolve_participants/participant_tiers 首项恒为 active
            # 本身,`for nid in notebook_ids: h.notebook_id = nid` 对每一本都无
            # 条件执行,并不是只有跨库命中才打标(chunk_context/knowledge_context/
            # render_follow_chain_context 都踩过、也都已按同一模式修过这个错误
            # 假设——本函数此前的注释也这样误判过,codex r3 review 当时把它判
            # 定为"不可达"是错的:citations_from 的产出会在答案合成失败、或模型
            # 没吐出任何有效 [k] 锚点时,被前端 buildAnswerReferences 当"回退
            # 列表"直接展示——见其 `if (references.length > 0) return
            # references;` 之后的 citations 兜底分支。必须显式与调用方
            # notebook_id 比较,相等则归零,镜像既有三处处理。
            hit_notebook_id = getattr(hit, "notebook_id", "") or ""
            if hit_notebook_id == notebook_id:
                hit_notebook_id = ""
            for evidence in hit.evidence:
                if evidence.element_id and evidence.element_id not in valid_element_ids:
                    continue
                filtered.append((tier, hit_notebook_id, evidence))

        # Task 12（引用跳转）: 批量按 element_id 查一次 knowhow 定位标签，不管
        # 本次要建多少条引用——绝不逐条引用各查一次(运行效率是一等约束)。
        knowhow_refs = self.knowhow_refs_for(
            evidence.element_id for _tier, _nb, evidence in filtered
        )
        preferred_titles = self.citation_titles(
            evidence.source_id for _tier, _nb, evidence in filtered
        )

        citations: list[Citation] = []
        for tier, hit_notebook_id, evidence in filtered:
            source_title = preferred_titles.get(evidence.source_id, "")
            citation_label = (
                f"{source_title} · {evidence.location_label}".strip(" ·")
                if source_title else label
            )
            citations.append(Citation(
                label=citation_label,
                source_id=evidence.source_id,
                element_id=evidence.element_id,
                location_label=evidence.location_label,
                quoted_span=evidence.quoted_span,
                tier=tier,
                notebook_id=hit_notebook_id,
                knowhow=knowhow_refs.get(evidence.element_id),
            ))
        return citations

    @staticmethod
    def truncate_kg_block(block: str, max_tokens: int) -> str:
        if est_tokens(block) <= max_tokens:
            return block
        lines, used = [], 0
        for line in block.splitlines():
            tokens = est_tokens(line)
            if used + tokens > max_tokens and lines:
                break
            lines.append(line)
            used += tokens
        return "\n".join(lines)
