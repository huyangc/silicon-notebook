"""One shared-model synthesis over peer notebook original-source evidence."""
from __future__ import annotations

import json

from app.core.llm import cap_kwargs
from app.domain.citation_origin import foreign_notebook_id
from app.models.ask import Citation
from app.services.cancellation import raise_if_cancelled
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys
from app.services.prompts import ANSWER_SCHEMA_HINT, answer_prompt
from app.services.retrieval_run import current_retrieval_run


class GlobalAskSynthesis:
    def __init__(self, *, settings, model_clients, parse_anchors, style_block=None,
                 answer_with_retry=None):
        self.settings = settings
        self.model_clients = model_clients
        self.parse_anchors = parse_anchors
        self.style_block = style_block
        self.answer_with_retry = answer_with_retry

    def _context(self, chunks, notebook_names):
        blocks, id_map = [], {}
        used = 0
        represented = {chunk.notebook_id for chunk in chunks if chunk.element_ids}
        share = self.settings.chunk_answer_budget_chars // max(1, len(represented))
        selected = set()

        def serialize(chunk, key):
            return json.dumps({
                "key": key,
                "notebook": notebook_names.get(chunk.notebook_id, "笔记本"),
                "source": chunk.source_title,
                "location": chunk.section_path,
                "text": chunk.text,
            }, ensure_ascii=False)

        def admit(chunk, allowance):
            nonlocal used
            key = f"k{len(blocks) + 1}"
            block = serialize(chunk, key)
            cost = len(block) + 1
            if cost > allowance or used + cost > self.settings.chunk_answer_budget_chars:
                return False
            used += len(block) + 1
            blocks.append(block)
            id_map[key] = {
                "object_id": chunk.chunk_id, "object_type": "chunk",
                "name": chunk.source_title, "source_title": chunk.source_title,
                "snippet": chunk.text, "source_id": chunk.source_id,
                "element_id": next(iter(chunk.element_ids), ""),
                "location_label": chunk.section_path, "notebook_id": chunk.notebook_id,
                "tier": "personal", "relevance": chunk.relevance,
            }
            selected.add(chunk.chunk_id)
            return True

        candidates = [chunk for chunk in chunks if chunk.element_ids]
        best = {}
        for chunk in candidates:
            best.setdefault(chunk.notebook_id, chunk)
        best_cost = sum(len(serialize(chunk, f"k{index}")) + 1
                        for index, chunk in enumerate(best.values(), start=1))
        if best_cost <= self.settings.chunk_answer_budget_chars:
            # Never let a weaker, shorter tail preempt a reserved best passage
            # when all libraries' best passages fit together.
            for chunk in best.values():
                admit(chunk, self.settings.chunk_answer_budget_chars - used)
        else:
            represented = set()
            for chunk in candidates:
                if chunk.notebook_id not in represented and admit(chunk, share):
                    represented.add(chunk.notebook_id)
        for chunk in candidates:
            if chunk.chunk_id not in selected:
                admit(chunk, self.settings.chunk_answer_budget_chars - used)
        return "\n".join(blocks), id_map

    def __call__(self, question, chunks, notebook_names, history, cancel_event):
        context, id_map = self._context(chunks, notebook_names)
        if not id_map:
            return (
                "当前检索没有找到足以支撑回答的原文。请补充文章标题、关键词或原文中的术语后重试。",
                False, [], [],
            )
        raise_if_cancelled(cancel_event)
        client = self.model_clients.chat("ask_answer")
        run = current_retrieval_run()
        style = self.style_block(run.actor_id) if self.style_block and run and run.actor_id else ""
        prompt = answer_prompt(question, context, history, style_block=style, peer_notebooks=True)
        def bound_marker(match):
            keys = marker_keys(match.group(0))
            if not keys or any(key not in id_map for key in keys):
                raise ValueError("unbound global citation")
            return "[" + ",".join(keys) + "]"

        def generate():
            raise_if_cancelled(cancel_event)
            raw = client.chat_json(
                [{"role": "user", "content": prompt}], ANSWER_SCHEMA_HINT,
                cancel_event=cancel_event, **cap_kwargs(client, "answer_max_tokens"),
            )
            raise_if_cancelled(cancel_event)
            data = json.loads(raw)
            answer = data.get("answer") if isinstance(data, dict) else None
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty global answer")
            answer = LOOSE_MARKER_RE.sub(bound_marker, answer.strip())
            if not answer.strip():
                raise ValueError("empty global answer after citation binding")
            anchors = self.parse_anchors(answer, id_map)
            return answer, data.get("grounded") is True and bool(anchors), anchors

        if self.answer_with_retry:
            answer, grounded, anchors, ok = self.answer_with_retry(generate, "全局问答")
            if not ok:
                return "模型暂未生成有效回答，请稍后重试。", False, [], []
        else:
            answer, grounded, anchors = generate()
        citations = [
            Citation(
                label=anchor.source_title or anchor.label,
                source_id=anchor.source_id, element_id=anchor.element_id,
                location_label=anchor.location_label, quoted_span=anchor.snippet or "",
                # Global Ask has no active notebook; every origin keeps its badge.
                source_file_name=anchor.source_file_name,
                notebook_id=foreign_notebook_id(anchor.notebook_id, ""),
            )
            for anchor in anchors
        ]
        return answer, grounded, anchors, citations
