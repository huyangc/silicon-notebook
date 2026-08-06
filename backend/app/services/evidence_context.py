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
from app.services.citation_markers import MARKER_RE, marker_keys
from app.services.source_display import source_display_title
from app.services.source_scope import notebook_in_scope


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


def _is_document_row(item: object) -> bool:
    """Is this enumerated row a whole DOCUMENT rather than something inside one?

    Structural rather than an ``isinstance`` on the executor's ``SourceItem``:
    this module sits under the enumeration executor in the layering and imports
    nothing from it (same reason ``collection_item_citations`` reads every field
    through ``getattr``).  The shape is unambiguous — a document row carries a
    ``source_id`` and neither an ``element_id`` nor an ``object_id``, which is
    true of no other row the enumerator produces.
    """
    return bool(
        getattr(item, "source_id", "")
        and not getattr(item, "element_id", "")
        and not getattr(item, "object_id", "")
    )


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

        The naming rule itself is ``source_display.source_display_title`` — the
        same one the enumeration list and the retrieval element cards apply, so
        one source cannot be called two things in one answer.  A source with no
        name at all is left out of the map rather than mapped to ``""``: callers
        fall back to their own label, which is not the same as showing a blank.
        """
        return {
            source_id: value["title"]
            for source_id, value in self.citation_source_info(source_ids).items()
            if value["title"]
        }

    def citation_source_info(
        self,
        source_ids: Iterable[str],
        *,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Resolve citation display titles and original upload names together.

        Both values come from the same bounded metadata read.  Keeping them in
        one lookup is important for Ask/report hot paths: adding filename
        provenance must not double the number of source queries.  ``file_name``
        is the persisted upload name; MinerU's temporary/output Markdown names
        are never stored in this column.
        """
        ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        if not ids:
            return {}
        rows = metadata if metadata is not None else self.source_metadata(ids)
        result: dict[str, dict[str, str]] = {}
        for source_id in ids:
            row = rows.get(source_id) or {}
            title = source_display_title(row)
            file_name = str(row.get("file_name") or "").strip()
            if title or file_name:
                result[source_id] = {
                    "title": title,
                    "file_name": file_name,
                    # Owning notebook, already in the same bounded read.  Tier
                    # cannot stand in for "came from a mounted library": a
                    # mounted notebook the user owns keeps tier="personal".
                    "notebook_id": str(row.get("notebook_id") or ""),
                }
        return result

    def collection_item_citations(
        self,
        items: Sequence[object],
        *,
        active_notebook_id: str,
    ) -> dict[str, Citation]:
        """Resolve one bounded original-source citation per enumerated row.

        The enumeration executor restricts items to the active notebook's
        participant set, and this boundary is rechecked here before any global
        element hydration.  The hydrated source's real notebook must also
        match the item's origin.  The browser receives only a compact locator
        and excerpt and never needs direct membership in a mounted base.

        KG rows expose at most ``MAX_EVIDENCE_REFS`` element ids; hydrate all
        requested ids in one bounded store read, then select the first live
        occurrence in the executor's stable order.  Element rows already carry
        their exact source projection and therefore remain citable even if the
        optional hydration read misses.
        """
        participants = set(
            self.notebooks.participant_notebook_ids(active_notebook_id)
        )
        rows = [
            item for item in items
            if str(getattr(item, "notebook_id", "") or active_notebook_id)
            in participants
        ]
        evidence_ids = list(dict.fromkeys(
            str(element_id)
            for item in rows
            for element_id in (
                ([getattr(item, "element_id", "")]
                 if getattr(item, "element_id", "") else [])
                + list(getattr(item, "evidence_element_ids", ()) or ())
            )
            if element_id
        ))
        hydrated = self.sources.evidence_elements(evidence_ids)

        source_ids: list[str] = []
        for item in rows:
            direct_source_id = str(getattr(item, "source_id", "") or "")
            if direct_source_id:
                source_ids.append(direct_source_id)
            for element_id in (getattr(item, "evidence_element_ids", ()) or ()):
                row = hydrated.get(str(element_id)) or {}
                source_id = str(row.get("source_id") or "")
                if source_id:
                    source_ids.append(source_id)
        source_metadata = self.source_metadata(source_ids)
        source_info = self.citation_source_info(
            source_ids, metadata=source_metadata
        )

        citations: dict[str, Citation] = {}
        for item in rows:
            # Same ordering as ``collection_enumeration_answer.enumerated_item_id``
            # — the module that looks these keys back up.  Not imported from
            # there: this module is the lower layer (it knows nothing about the
            # enumeration answer glue), so the two are kept identical by
            # ``test_enumerated_item_key_agrees_across_the_two_layers`` instead of
            # by a dependency that would point the wrong way.  ``source_id`` is
            # ordered LAST so an element row — which also carries one — keys by
            # its element and can never collide with a document row for the same
            # file.
            item_id = str(
                getattr(item, "element_id", "")
                or getattr(item, "object_id", "")
                or getattr(item, "source_id", "")
                or ""
            )
            if not item_id:
                continue
            origin = str(getattr(item, "notebook_id", "") or "")
            expected_notebook_id = origin or active_notebook_id
            if expected_notebook_id not in participants:
                continue
            if _is_document_row(item):
                # A DOCUMENT row's original source is the document itself: there
                # is no sub-location to resolve, so this branch needs neither the
                # element hydration above nor an ``element_id``.  Leaving that
                # field empty is load-bearing rather than lazy — the grounding
                # classifier counts a row as exact evidence only when it carries
                # BOTH ids, and a whole document is not an exact original-text
                # locator.  The notebook boundary is still enforced: the row only
                # got here because its origin is in the participant set, and the
                # cross-library convention (``notebook_id`` empty for the active
                # notebook) is the same one every other citation follows.
                citations[item_id] = Citation(
                    label=str(getattr(item, "source_title", "") or item_id),
                    source_id=item_id,
                    element_id="",
                    location_label=str(getattr(item, "doc_type_label", "") or ""),
                    quoted_span=str(getattr(item, "summary", "") or "")[:300],
                    source_file_name=(source_info.get(item_id) or {}).get(
                        "file_name", ""
                    ),
                    tier=str(getattr(item, "tier", "personal") or "personal"),
                    notebook_id=(origin if origin != active_notebook_id else ""),
                    knowhow=None,
                )
                continue
            direct_element_id = str(getattr(item, "element_id", "") or "")
            evidence_id = direct_element_id
            evidence_row: Mapping[str, Any] = {}
            if direct_element_id:
                evidence_row = hydrated.get(direct_element_id) or {}
                source_id = str(getattr(item, "source_id", "") or "")
                if str(
                    (source_metadata.get(source_id) or {}).get("notebook_id") or ""
                ) != expected_notebook_id:
                    continue
                location = str(getattr(item, "location_label", "") or "")
                quoted = str(getattr(item, "text", "") or "")
                source_title = str(getattr(item, "source_title", "") or "")
            else:
                source_id = ""
                location = ""
                quoted = ""
                source_title = ""
                for candidate_id in (
                    getattr(item, "evidence_element_ids", ()) or ()
                ):
                    candidate = hydrated.get(str(candidate_id))
                    if not candidate:
                        continue
                    candidate_source_id = str(candidate.get("source_id") or "")
                    if str(
                        (source_metadata.get(candidate_source_id) or {}).get("notebook_id") or ""
                    ) != expected_notebook_id:
                        continue
                    evidence_id = str(candidate_id)
                    evidence_row = candidate
                    source_id = candidate_source_id
                    location = str(candidate.get("location_label") or "")
                    quoted = str(candidate.get("text") or "")
                    break
            if not source_id or not evidence_id:
                continue
            item_source_info = source_info.get(source_id) or {}
            source_title = item_source_info.get("title", source_title)
            citations[item_id] = Citation(
                label=f"{source_title} · {location}".strip(" ·"),
                source_id=source_id,
                element_id=evidence_id,
                location_label=location,
                quoted_span=quoted[:300],
                source_file_name=item_source_info.get("file_name", ""),
                tier=str(getattr(item, "tier", "personal") or "personal"),
                notebook_id=(origin if origin != active_notebook_id else ""),
                knowhow=_knowhow_ref(evidence_row) if evidence_row else None,
            )
        return citations

    def chunk_context(
        self,
        chunks: Sequence[RetrievedChunk],
        *,
        notebook_id: str,
        id_offset: int = 0,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        """``id_offset`` 镜像 ``element_context``/``knowledge_context``:默认 0 保持
        chunk 段恒为 ``k1..kN``(所有既有调用方),按节合成给每节整体加一段偏移,
        让各节的 key 号段互不相交(见 ``outline_synthesis``)。"""
        budget = (
            self.settings.chunk_answer_budget_chars
            if budget_chars is None
            else max(0, int(budget_chars))
        )
        tiers = self.tier_map(
            list({getattr(chunk, "notebook_id", "") or notebook_id for chunk in chunks})
        )
        citation_source_info = self.citation_source_info(
            chunk.source_id for chunk in chunks
        )
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
            separator = 1 if lines else 0
            remaining = budget - used - separator
            if remaining <= 0:
                break
            key = f"k{index + id_offset}"
            source_info = citation_source_info.get(chunk.source_id) or {}
            source_title = source_info.get("title", chunk.source_title)
            prefix = f"{key}: "
            if remaining <= len(prefix):
                break
            text = chunk.text[:remaining - len(prefix)]
            if len(text) < len(chunk.text) and len(text) > 1:
                text = text[:-1] + "…"
            line = prefix + text
            lines.append(line)
            used += separator + len(line)
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
                **(
                    {"source_file_name": source_info["file_name"]}
                    if source_info.get("file_name") else {}
                ),
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
        citation_source_info = self.citation_source_info(
            element.source_id for element in elements
        )
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        used = 0
        for index, element in enumerate(elements, 1):
            key = f"k{id_offset + index}"
            location = element.location_label or element.element_type
            source_info = citation_source_info.get(element.source_id) or {}
            source_title = source_info.get("title", element.source_title)
            prefix = (
                f"{key}: [source-element][{tier}] {source_title} · "
                f"{location} — "
            )
            separator = 1 if lines else 0
            remaining = budget - used - separator - len(prefix)
            if remaining <= 0:
                break
            text = element.text[:remaining]
            if len(text) < len(element.text) and remaining > 1:
                text = text[:-1] + "…"
            line = prefix + text
            lines.append(line)
            used += separator + len(line)
            evidence_by_id[key] = {
                "object_id": element.element_id,
                "object_type": "element",
                "name": location or source_title,
                "definition": element.text,
                "snippet": element.text[:300],
                "source_id": element.source_id,
                "element_id": element.element_id,
                "source_title": source_title,
                **(
                    {"source_file_name": source_info["file_name"]}
                    if source_info.get("file_name") else {}
                ),
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
        budget_chars: int | None = None,
        priority_object_ids: Sequence[str] = (),
        priority_budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        """``priority_object_ids`` 先于其余命中装配,并可另有一份更紧的子预算。

        动机(报告 PR-5):大纲绑定的对象被追加在候选尾部,按输入序渲染时在大候选池
        下恒被预算截掉。子预算让它们先拿一份额度,其余 ranked 命中吃剩下的。

        **这必须在一次调用里做完**,不能由调用方拆成两次:关系行(`relations:`)是对
        本次 `evidence_by_id` **内部**的边求的,拆两次就会丢掉所有跨两半的关系,还会
        渲染出两行 `relations:` 并各自记一次预算;cluster 去重同理。缺省(空 priority)
        时这个函数与接入前逐字相同。
        """
        budget = (
            self.settings.answer_context_budget_chars
            if budget_chars is None else max(0, int(budget_chars))
        )
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        seen_clusters: set[str] = set()
        # 刻意不按当前 base_scope 收窄 participants(仍是本 notebook 的全部参与库,
        # 含被取消勾选的):下面每个参与库都要各查一次 cluster_map(),大库可达数百万
        # 行,收窄能省掉被排除库那份查询/内存。之所以不做,是它只是成本问题、不是
        # 内容泄漏问题——没有任何 canonical 簇的成员跨库,所以 in-scope 对象的 id
        # 不可能出现在被排除库的 cluster_map 里,``_canonical()`` 对它的查找必然
        # miss;而 ``in_network_relations``(见本函数下方)取回的、两端解析到被排除
        # 库对象的关系行,会在 ``object_to_key`` 查不到 key 时被丢弃。收窄它需要改动
        # canonical 折叠/去重语义,超出本次修复范围。
        participants = self.notebooks.participant_notebook_ids(notebook_id)
        # 逐 hit 在各参与库的缓存 map 里做有界查找,绝不把它们合并进新 dict:
        # 合并是 O(全库) 的整表拷贝(scale 下 cluster map 可达 5M 条),而本函数
        # 每次答案合成都会被调、按节合成还要按节数放大。逆序查找保持与原
        # dict.update 逐库覆盖完全相同的「后挂载库优先」语义。
        participant_maps = [
            self.knowledge.cluster_map(participant) for participant in participants
        ]

        def _canonical(object_id: str) -> str:
            for participant_map in reversed(participant_maps):
                canonical = participant_map.get(object_id)
                if canonical is not None:
                    return canonical
            return object_id

        used = 0
        next_id = 0

        def _admit(group: Sequence[RetrievedKnowledge], ceiling: int) -> None:
            """把 ``group`` 装进 ``ceiling`` 以内(公用 lines/used/next_id/去重集合)。

            `break` 只结束**本组**:优先组用完自己的子预算后,其余命中仍要继续吃总
            预算的剩余部分。
            """
            nonlocal used, next_id
            for hit in group:
                cluster_id = _canonical(hit.object_id)
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
                if not notebook_in_scope(origin):
                    # 参考库勾选闸,落在**装配点**而不是下面的 node_context 读上。
                    # 这里是 KG 命中变成「答案 prompt 里的一行 + 一个活的 k{n}
                    # 锚点」的那一步:node_context 只提供 definition/snippet,把它
                    # 清空仍会让对象名照常渲染、锚点照常可引用。取消勾选的参考库
                    # 必须一个字都不进 prompt,所以整条命中在这里跳过。
                    #
                    # 库维度专用(notebook_in_scope 而非 source_scope_restricted):
                    # 只取消参考库时后者恒为 False(R1)。本地维度不在此过滤——命中
                    # 已在候选边界按来源过滤过。
                    continue
                try:
                    context = self.knowledge.node_context(origin, hit.object_id)
                except KeyError:
                    continue
                separator = 1 if lines else 0
                remaining_total = ceiling - used - separator
                if remaining_total <= 0:
                    break
                next_id += 1
                key = f"k{next_id + id_offset}"
                name = str(hit.payload.get("name", "")).strip()
                occurrences = context.get("occurrences") or []
                snippet = occurrences[0].get("element_text") if occurrences else ""
                definition = context.get("definition") or snippet
                tier = getattr(hit, "tier", "personal")
                prefix = f"{key}: [{hit.object_type}][{tier}] {name}"
                if remaining_total <= len(prefix):
                    break
                definition_cap = max(0, min(300, remaining_total - len(prefix)))
                extra = (
                    f" — def: {definition[:definition_cap]}"
                    if definition and definition_cap
                    else ""
                )
                if context.get("steps") and definition_cap:
                    extra += "; steps: " + " -> ".join(
                        step.get("name", "") for step in context["steps"][:8]
                    )
                line = (prefix + extra)[:remaining_total]
                lines.append(line)
                used += separator + len(line)
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

        priority = {str(object_id) for object_id in (priority_object_ids or ()) if object_id}
        if priority:
            first = [hit for hit in hits if str(hit.object_id) in priority]
            rest = [hit for hit in hits if str(hit.object_id) not in priority]
            _admit(first, budget if priority_budget_chars is None
                   else max(0, min(budget, int(priority_budget_chars))))
            _admit(rest, budget)
        else:
            _admit(hits, budget)

        citation_source_info = self.citation_source_info(
            value.get("source_id", "") for value in evidence_by_id.values()
        )
        for value in evidence_by_id.values():
            source_id = str(value.get("source_id") or "")
            source_info = citation_source_info.get(source_id) or {}
            if source_info.get("title"):
                value["source_title"] = source_info["title"]
            if source_info.get("file_name"):
                value["source_file_name"] = source_info["file_name"]

        object_to_key = {value["object_id"]: key for key, value in evidence_by_id.items()}
        if len(object_to_key) >= 2:
            # Same deliberate non-narrowing as ``participants`` above: this query
            # spans every participant (including a checkbox-excluded library),
            # not just the ones ``_admit`` actually let evidence in from. It
            # cannot leak that library's content -- a relation row only survives
            # below when BOTH endpoints resolve to a key in ``object_to_key``,
            # and an excluded library's objects were never admitted into
            # ``evidence_by_id`` in the first place -- so this is pure query cost
            # for excluded libraries, never a content leak.
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
                relation_prefix = "\nrelations: " if lines else "relations: "
                remaining = budget - used
                rendered: list[str] = []
                relation_used = len(relation_prefix)
                for source_key, edge_type, target_key, _nb, _source, _target, support in ranked[:30]:
                    suffix = f" (×{support}源)" if support >= 2 else ""
                    item = f"{source_key} -[{edge_type}]-> {target_key}{suffix}"
                    separator = 2 if rendered else 0
                    if relation_used + separator + len(item) > remaining:
                        break
                    rendered.append(item)
                    relation_used += separator + len(item)
                if rendered:
                    lines.append("relations: " + "; ".join(rendered))
        return ("\n".join(lines) if lines else "(none)"), evidence_by_id

    def parse_anchors(
        self,
        answer: str,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[AnswerAnchor]:
        anchors: list[AnswerAnchor] = []
        seen: set[str] = set()
        marker_groups = MARKER_RE.findall(answer or "")
        cited_keys = list(dict.fromkeys(
            key
            for marker_group in marker_groups
            for key in marker_keys(marker_group)
            if key in evidence_by_id
        ))
        citation_source_info = self.citation_source_info(
            str(evidence_by_id[key].get("source_id") or "") for key in cited_keys
        )
        for marker_group in marker_groups:
            keys = marker_keys(marker_group)
            if not keys or any(key not in evidence_by_id for key in keys):
                continue
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                context = evidence_by_id[key]
                name = str(context.get("name", ""))
                source_id = str(context.get("source_id") or "")
                source_info = citation_source_info.get(source_id) or {}
                anchors.append(AnswerAnchor(
                    key=key,
                    object_id=str(context["object_id"]),
                    object_type=str(context["object_type"]),
                    label=name[:40] or key,
                    name=name,
                    definition=context.get("definition"),
                    snippet=context.get("snippet"),
                    source_title=source_info.get(
                        "title",
                        str(context.get("source_title", "")),
                    ),
                    source_file_name=source_info.get(
                        "file_name", str(context.get("source_file_name") or "")
                    ),
                    location_label=str(context.get("location_label", "")),
                    source_id=source_id,
                    element_id=str(context.get("element_id", "")),
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
        citation_source_info = self.citation_source_info(
            evidence.source_id for _tier, _nb, evidence in filtered
        )

        citations: list[Citation] = []
        for tier, hit_notebook_id, evidence in filtered:
            source_info = citation_source_info.get(evidence.source_id) or {}
            source_title = source_info.get("title", "")
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
                source_file_name=source_info.get("file_name", ""),
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
