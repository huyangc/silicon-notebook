"""Evidence/context composition for Ask answer synthesis.

This module owns the stable ``[k_i]`` assignment and reverse binding contract.
It deliberately does not synthesize model answers and performs no maintenance;
its collaborators are narrow read stores only.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from app.core.config import Settings
from app.models.schemas import AnswerAnchor, Citation
from app.repositories.ports import (
    EvidenceKnowledgeContextPort, NotebookStorePort, SourceStorePort,
)
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge, est_tokens


_MARKER_GROUP_RE = re.compile(r"\[((?:k\d+\s*,\s*)*k\d+)\]")


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
        lines: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        used = 0
        for index, chunk in enumerate(chunks, 1):
            if used >= budget and lines:
                break
            key = f"k{index}"
            line = f"{key}: {chunk.text}"
            lines.append(line)
            used += len(line)
            origin = getattr(chunk, "notebook_id", "") or notebook_id
            evidence_by_id[key] = {
                "object_id": chunk.chunk_id,
                "object_type": "chunk",
                "name": chunk.section_path or chunk.source_title,
                "definition": None,
                "snippet": chunk.text[:300],
                "source_title": chunk.source_title,
                "location_label": chunk.section_path,
                "tier": tiers.get(origin, "personal"),
            }
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
            origin = getattr(hit, "notebook_id", "") or notebook_id
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
            }

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
        for marker_group in _MARKER_GROUP_RE.findall(answer or ""):
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
                    source_title=str(context.get("source_title", "")),
                    location_label=str(context.get("location_label", "")),
                    tier=str(context.get("tier", "personal")),
                    provenance=dict(context.get("provenance") or {}),
                ))
        return anchors

    def citations_from(
        self,
        hits: Sequence[RetrievedKnowledge],
        valid_element_ids: set[str],
        label: str,
    ) -> list[Citation]:
        citations: list[Citation] = []
        for hit in hits:
            tier = getattr(hit, "tier", "personal") or "personal"
            for evidence in hit.evidence:
                if evidence.element_id and evidence.element_id not in valid_element_ids:
                    continue
                citations.append(Citation(
                    label=label,
                    source_id=evidence.source_id,
                    element_id=evidence.element_id,
                    location_label=evidence.location_label,
                    quoted_span=evidence.quoted_span,
                    tier=tier,
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
