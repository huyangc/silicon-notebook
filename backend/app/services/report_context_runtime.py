"""Report-stage ownership of model budgets, projections and durable receipts.

Only evidence passed by the owning renderer is replaceable. Research contracts
remain in the render closure. In particular citation entries are recovered from
renderer-recorded spans, never by finding key-shaped strings in source text.
"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from app.domain.cancellation import AskCancelled, raise_if_cancelled
from app.services.context_spans import recorded_spans, recording_spans
from app.services.report_context import (
    ContextProjection,
    EvidenceUnit,
    ReportContextSession,
)

CONTEXT_RECEIPT_KEY = "context_processing"
CONTEXT_RECOVERY_INPUT_DIVISOR = 2


def record_report_context(function):
    @wraps(function)
    def recorded(*args, **kwargs):
        with recording_spans():
            return function(*args, **kwargs)
    return recorded


def _pin(client, max_tokens=None):
    pin = getattr(client, "pin_context", None)
    return pin(max_tokens=max_tokens) if callable(pin) else client


def _context_error(exc):
    from app.core.model_context import is_context_error
    return is_context_error(exc)


class _ReducedBudget:
    def __init__(self, budget):
        self._budget = budget
        self.max_input_tokens = budget.max_input_tokens // CONTEXT_RECOVERY_INPUT_DIVISOR

    def __getattr__(self, name):
        return getattr(self._budget, name)

    def fits(self, messages, schema_hint):
        return self._budget.input_tokens(messages, schema_hint) <= self.max_input_tokens


class _ReducedClient:
    def __init__(self, client):
        self.context_budget = _ReducedBudget(client.context_budget)


@dataclass
class PreparedReportCall:
    runtime: "ReportContextRuntime"
    client: Any
    stage: str
    schema: str
    render: Callable
    originals: tuple[EvidenceUnit, ...]
    projection: ContextProjection
    max_tokens: int | None
    recovered: bool = False

    @property
    def messages(self):
        return self.render(self.projection.units)

    def chat_json(self, **kwargs):
        from app.core.model_context import request_admission

        raise_if_cancelled(self.runtime.cancel_event)
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        kwargs.setdefault("cancel_event", self.runtime.cancel_event)
        previous_messages = self.messages
        try:
            # A section's bounded empty/JSON retry may call us again after
            # recovery. Every subsequent send still belongs to that recovery.
            admission = (request_admission(lambda: self.runtime.session.admit_attempt(self.stage))
                         if self.recovered else nullcontext())
            with admission:
                return self.client.chat_json(previous_messages, self.schema, **kwargs)
        except Exception as exc:
            if (self.recovered or not _context_error(exc)
                    or getattr(self.client, "context_budget", None) is None):
                if report_context_failure(exc):
                    self.runtime.session.note_failure(self.stage)
                raise
            self.recovered = True
            self.projection = self.runtime.project(
                _ReducedClient(self.client), self.schema, self.render,
                self.originals, self.stage,
            )
            from app.core.model_context import ModelContextExceeded
            if (self.messages == previous_messages or
                    self.client.context_budget.input_tokens(self.messages, self.schema)
                    >= self.client.context_budget.input_tokens(previous_messages, self.schema)):
                self.runtime.session.note_failure(self.stage)
                raise ModelContextExceeded() from exc
            with request_admission(lambda: self.runtime.session.admit_attempt(self.stage)):
                try:
                    return self.client.chat_json(self.messages, self.schema, **kwargs)
                except AskCancelled:
                    raise
                except Exception:
                    self.runtime.session.note_failure(self.stage)
                    raise


class ReportContextRuntime:
    def __init__(self, engine, notebook_id="", report_id="", initial_receipt=None):
        self.engine = engine
        self.notebook_id = notebook_id
        self.report_id = report_id
        self.cancel_event = engine.cancel_event
        self._persist_lock = threading.RLock()
        self.session = ReportContextSession(
            engine.settings, initial_receipt=initial_receipt,
            on_change=self._changed, cancel_event=self.cancel_event,
        )

    def _changed(self, _receipt):
        # Callbacks can originate from concurrent section workers. Read the
        # newest receipt under one publication lock so stale snapshots cannot
        # replace newer admissions; never hold a connection across model I/O.
        if not self.report_id:
            return
        with self._persist_lock:
            receipt = self.session.receipt()
            if not (receipt.get("extra_calls") or receipt.get("compressed") or any(
                item.get("failed") or item.get("omitted_items") or item.get("partial_items")
                or item.get("invalid_items") for item in receipt.get("stages", {}).values()
            )):
                return
            reports = self.engine.dependencies.reports
            row = reports.get_report(self.notebook_id, self.report_id)
            understanding = dict((row or {}).get("understanding") or {})
            understanding[CONTEXT_RECEIPT_KEY] = receipt
            reports.update_report(self.notebook_id, self.report_id,
                                  understanding=understanding,
                                  **({"progress": "正在分批整理报告材料"}
                                     if receipt.get("extra_calls") and (row or {}).get("status")
                                     in {"planning", "generating"} else {}))

    def flush(self):
        self._changed(None)

    def compressor(self, max_tokens):
        return _pin(self.engine.dependencies.model_clients.chat("report_context_compress"),
                    max_tokens)

    def reflect_chat_json(self, messages, schema_hint, *, client, candidates_summary, **kwargs):
        return _ReportReflectClient(self.engine, client).reflection_chat_json(
            messages, schema_hint, candidates_summary=candidates_summary, **kwargs,
        )

    def project(self, client, schema, render, units, stage):
        try:
            return self.session.compress_to_fit(
                client, schema, render, units,
                compressor_factory=self.compressor, stage=stage,
            )
        except AskCancelled:
            raise
        except Exception:
            self.session.note_failure(stage)
            raise

    def prepare(self, workload, schema, render, units=(), *, stage, max_tokens=None):
        client = _pin(self.engine.dependencies.model_clients.chat(workload), max_tokens)
        originals = tuple(units)
        projection = self.project(client, schema, render, originals, stage)
        send_cap = max_tokens if (getattr(client, "context_budget", None) is not None
                                  or getattr(client, "settings", None) is not None) else None
        return PreparedReportCall(self, client, stage, schema, render,
                                  originals, projection, send_cap)


def report_context_runtime(engine, notebook_id="", report_id="", *, reset=False):
    runtime = getattr(engine, "_context_runtime", None)
    if runtime is not None and not reset and (not report_id or runtime.report_id == report_id):
        return runtime
    initial = None
    if report_id:
        row = engine.dependencies.reports.get_report(notebook_id, report_id)
        initial = dict((row or {}).get("understanding") or {}).get(CONTEXT_RECEIPT_KEY)
    runtime = ReportContextRuntime(engine, notebook_id, report_id, initial)
    engine._context_runtime = runtime
    return runtime


def receipt_for(engine):
    runtime = getattr(engine, "_context_runtime", None)
    return runtime.session.receipt() if runtime is not None else None


def intent_context_limited(engine, sections, intent_id):
    editor_limited = getattr(report_context_runtime(engine), "editor_limited_sections", set())
    return any(intent_id in (section.get("intent_ids") or []) and (
        index in editor_limited or (section.get("context_processing") or {}).get("coverage_limited")
    ) for index, section in enumerate(sections))


def preserve_context_error(error):
    """Optional model stages may degrade, but must not hide capacity errors."""
    if report_context_failure(error):
        raise error


def report_context_failure(error):
    return error is not None and (_context_error(error)
        or str(getattr(error, "code", "")).startswith("report_context_"))


def planning_context_call(engine, workload, schema, render, blocks, *, stage):
    originals = tuple(EvidenceUnit(
        f"{stage}:{name}:{index}", line, group=name, kind="planning_material",
    ) for name, text in blocks.items() for index, line in enumerate(text.splitlines(keepends=True))
        if line.strip())

    def messages(selected):
        if tuple(selected) == originals:
            projected = blocks
        else:
            projected = {name: "\n".join(_unit_text(unit) for unit in selected
                                        if unit.group == name) for name in blocks}
        return [{"role": "user", "content": render(projected)}]

    return report_context_runtime(engine).prepare(
        workload, schema, messages, originals, stage=stage,
    ).chat_json()


class ReportIntentClient:
    def __init__(self, engine):
        self.engine = engine
        self.client = engine.dependencies.model_clients.chat("report_outline")

    def __getattr__(self, name):
        return getattr(self.client, name)

    def chat_json(self, messages, schema_hint, **kwargs):
        return report_context_runtime(self.engine).prepare(
            "report_outline", schema_hint, lambda _units: messages,
            stage="planning_intent", max_tokens=kwargs.get("max_tokens"),
        ).chat_json(**kwargs)


class _ReportReflectClient:
    def __init__(self, engine, client):
        self.engine, self.client = engine, client

    def __getattr__(self, name):
        return getattr(self.client, name)

    def reflection_chat_json(self, messages, schema_hint, *, candidates_summary, **kwargs):
        # The caller provides the exact dynamic suffix it just rendered. Never
        # search a question/source for a delimiter which its author could forge.
        originals = tuple(EvidenceUnit(f"reflect:{index}", line, kind="retrieval_preview")
                          for index, line in enumerate(candidates_summary.splitlines(keepends=True))
                          if line.strip())
        content = messages[-1]["content"]
        if candidates_summary and not content.endswith(candidates_summary):
            raise ValueError("invalid reflection context ownership")
        prefix = content[:-len(candidates_summary)] if candidates_summary else content

        def render(selected):
            if tuple(selected) == originals:
                return messages
            return [*messages[:-1], {**messages[-1], "content": prefix + "\n".join(
                _unit_text(unit) for unit in selected)}]

        runtime = report_context_runtime(self.engine)
        pinned = _pin(self.client, kwargs.get("max_tokens"))
        projection = runtime.project(pinned, schema_hint, render, originals, "retrieve")
        prepared = PreparedReportCall(runtime, pinned, "retrieve", schema_hint,
                                     render, originals, projection, kwargs.get("max_tokens"))
        return prepared.chat_json(**kwargs)


def _unit_text(unit, aliases=None):
    if not unit.level or unit.raw_text:
        return unit.text
    aliases = aliases or {}
    return json.dumps({
        "finding": unit.text,
        "evidence_keys": [aliases.get(key, key) for key in unit.original_handles()],
        "original_excerpts": [
            {"key": aliases.get(key, key), "quote": quote}
            for key, quote in unit.quotes
        ],
        "conditions": unit.conditions, "counterevidence": unit.counterevidence,
        "inference": unit.inference, "general": unit.general,
    }, ensure_ascii=False)


def _evidence_group(context, sub_outline):
    identities = {str(context.get(key) or "") for key in ("object_id", "element_id", "chunk_id")}
    for index, topic in enumerate(sub_outline):
        keys = topic.get("evidence_keys", ()) if isinstance(topic, dict) else getattr(topic, "evidence_keys", ())
        if identities.intersection(str(key) for key in keys):
            return f"topic-{index}"
    return str(context.get("source_id") or context.get("object_id") or "unassigned")


def prepare_section_call(engine, *, notebook_id, report_id, section_index,
                         section, question, blocks, context_block, id_map,
                         frame_block, synthesis, sub_outline, structure_builder):
    from app.services.prompts import REPORT_SECTION_SCHEMA_HINT, report_section_prompt
    from app.services.report_synthesis import localize_blueprint_evidence

    runtime = report_context_runtime(engine)
    aliases = {}
    units = []
    semantic_blocks = []
    for block_index, block in enumerate(blocks):
        spans = recorded_spans(block) or ()
        keys = {span.key for span in spans if span.key in id_map}
        semantic = ("\n".join(block[span.start:span.end] for span in spans
                               if not span.key and not span.glue) if spans else block).strip()
        if semantic:
            if keys:
                # Renderer-owned graph edges and inference qualifications are
                # meaningful context. Retain them verbatim with their entire
                # dependency set; never attach inferred text to a source key.
                semantic_blocks.append((keys, semantic))
            else:
                units.append(EvidenceUnit(f"section-{section_index}:context-{block_index}",
                                          semantic, group=f"context-{block_index}", kind="context"))
        for span in spans:
            if not span.key or span.key not in id_map:
                continue
            handle = f"section-{section_index}:{span.key}"
            aliases[handle] = span.key
            context = id_map[span.key]
            units.append(EvidenceUnit(
                handle, block[span.start:span.end], group=_evidence_group(context, sub_outline),
                kind=str(context.get("object_type") or "evidence"),
                provenance=tuple((key, str(context.get(key) or "")) for key in
                                 ("source_id", "object_id", "element_id", "location_label"))
                    + (("rendered_sha256", hashlib.sha256(
                        block[span.start:span.end].encode("utf-8")).hexdigest()),),
            ))
    originals = tuple(units)

    def render(selected):
        unchanged = selected == originals
        selected_keys = set(id_map) if unchanged else {
            aliases[handle] for unit in selected for handle in unit.original_handles()
            if handle in aliases
        }
        selected_map = {key: value for key, value in id_map.items() if key in selected_keys}
        localized = localize_blueprint_evidence(synthesis, selected_map)
        evidence = context_block if unchanged else "\n".join([
            *(_unit_text(unit, aliases) for unit in selected),
            *(text for dependencies, text in semantic_blocks if dependencies <= selected_keys),
        ]) or "(no evidence fits this model's context budget)"
        render.evidence_context = evidence
        directions = list(section.get("intent_questions") or [])
        fixed_question = question + ("\n本节必答方向：\n" + "\n".join(directions) if directions else "")
        return [{"role": "user", "content": report_section_prompt(
            section["title"], section["scope"], fixed_question, evidence,
            allow_parametric=engine.settings.report_allow_parametric,
            discovered_structure=structure_builder(sub_outline, selected_map),
            assumptions="；".join(str(item) for item in
                ((section.get("intent_contract") or {}).get("assumptions") or [])
                if str(item).strip()),
            report_frame=frame_block,
            synthesis_commitment=json.dumps(localized, ensure_ascii=False) if localized else "",
        )}]

    cap = int(getattr(engine.settings, "report_section_max_tokens", 65536))
    return runtime.prepare("report_section", REPORT_SECTION_SCHEMA_HINT,
                           render, originals, stage="draft", max_tokens=cap), aliases


def projected_section_map(prepared, aliases, original_map):
    if prepared.projection.units == prepared.originals:
        return original_map
    keys = {aliases[handle] for handle in prepared.projection.source_handles if handle in aliases}
    return {key: value for key, value in original_map.items() if key in keys}


def section_context_receipt(prepared, synthesis=None):
    projection = prepared.projection
    inherited_level = int(((synthesis or {}).get("section") or {}).get("_context_level", 0))
    return {"max_level": max(projection.max_level, inherited_level), "compressed": projection.compressed,
            "omitted_items": len(projection.omitted_handles),
            "partial_items": len(projection.partial_handles),
            "coverage_limited": bool(projection.omitted_handles or projection.partial_handles)}


def inherit_blueprint_context_levels(engine, blueprint):
    """Extend input lineage with the normalized output's cross-section dependencies."""
    runtime = report_context_runtime(engine)
    levels = getattr(runtime, "blueprint_evidence_levels", {})
    section_levels = getattr(runtime, "blueprint_section_levels", {})
    # Shared definitions retain their text even when localization removes their
    # citation keys. The unbound central answer is also sent to every writer,
    # so it conservatively inherits every admitted input's depth.
    shared_level = max((levels.get(key, 0)
                        for definition in blueprint.get("shared_definitions", [])
                        for key in definition.get("evidence_keys", [])), default=0)
    if blueprint.get("central_answer"):
        shared_level = max(shared_level, max(levels.values(), default=0))
    claims = {claim["id"]: claim for claim in blueprint.get("claims", [])}
    for section in blueprint.get("sections", []):
        dependencies = (
            key for claim_id in section.get("claim_ids", [])
            for field in ("evidence_keys", "counterevidence_keys")
            for key in claims[claim_id].get(field, [])
        )
        # The section's thesis/handoff have no evidence bindings; preserve its
        # input-section lineage as well as any explicitly transferred evidence.
        level = max(shared_level, section_levels.get(section["section_id"], 0),
                    max((levels.get(key, 0) for key in dependencies), default=0))
        if level:
            section["_context_level"] = level


def record_section_baseline(engine, result, *, notebook_id, section, question,
                            chunks, elements, id_map, context_block, budget_chars):
    from app.services.retrieval_baseline import build_retrieval_baseline_manifest

    prior = getattr(result, "baseline_manifest", None)
    result.baseline_manifest = build_retrieval_baseline_manifest(
        notebook_id=notebook_id, query=f"{question}\n{section.get('title') or ''}",
        mode="report", settings=engine.settings,
        candidate_knowledge=prior.candidate_knowledge if prior is not None else result.top_hits,
        candidate_chunks=prior.candidate_chunks if prior is not None else chunks,
        candidate_elements=prior.candidate_elements if prior is not None else elements,
        selected_knowledge=result.top_hits,
        selected_chunks=getattr(result, "baseline_chunks", None) or chunks,
        selected_elements=elements, final_context_block=context_block,
        final_id_map=id_map, final_ordered_handles=tuple(id_map),
        final_budget_chars=budget_chars, prior_manifest=prior,
        baseline_step_usage=len(getattr(result, "trace", None) or []),
    )


def report_blueprint_call(engine, question, intent_contract, report_frame, evidence_sections):
    from app.services.prompts import REPORT_SYNTHESIS_SCHEMA_HINT, report_synthesis_prompt

    units = tuple(EvidenceUnit(
        f"{section['section_id']}:{card['evidence_id']}",
        json.dumps(card, ensure_ascii=False), group=section["section_id"],
    ) for section in evidence_sections for card in section["evidence"])
    by_handle = {unit.handle: (unit.group, json.loads(unit.text)) for unit in units}

    def render(selected):
        if tuple(selected) == units:
            payload = evidence_sections
        else:
            payload = [{**section, "evidence": []} for section in evidence_sections]
            sections = {section["section_id"]: section for section in payload}
            admitted = set()
            for unit in selected:
                for handle in unit.original_handles():
                    group, original = by_handle[handle]
                    card = dict(original)
                    if unit.level:
                        card["body"] = "\n".join(quote for key, quote in unit.quotes if key == handle)
                        sections[group].setdefault("findings", []).append(_unit_text(
                            unit, {key: value[1]["evidence_id"] for key, value in by_handle.items()}))
                    if handle not in admitted:
                        sections[group]["evidence"].append(card)
                        admitted.add(handle)
        return [{"role": "user", "content": report_synthesis_prompt(
            question, json.dumps(intent_contract, ensure_ascii=False),
            json.dumps(report_frame or {}, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
            facet_ids=[row["id"] for row in (report_frame or {}).get("facets") or []
                       if isinstance(row, dict) and row.get("id")],
        )}]

    prepared = report_context_runtime(engine).prepare(
        "report_summary", REPORT_SYNTHESIS_SCHEMA_HINT, render, units,
        stage="synthesis", max_tokens=engine.settings.report_synthesis_max_tokens,
    )
    raw = prepared.chat_json()
    legal = {by_handle[handle][1]["evidence_id"] for handle in prepared.projection.source_handles}
    levels = {}
    section_levels = {}
    for unit in prepared.projection.units:
        for handle in unit.original_handles():
            section_id, card = by_handle[handle]
            evidence_id = card["evidence_id"]
            levels[evidence_id] = max(levels.get(evidence_id, 0), unit.level)
            section_levels[section_id] = max(section_levels.get(section_id, 0), unit.level)
    prepared.runtime.blueprint_evidence_levels = levels
    prepared.runtime.blueprint_section_levels = section_levels
    return raw, legal


def report_editor_call(engine, question, sections, frame, blueprint, intent_block):
    from app.services.prompts import REPORT_SUMMARY_SCHEMA_HINT, report_summary_prompt
    from app.services.report_synthesis import fair_editor_context

    runtime = report_context_runtime(engine)
    runtime.editor_limited_sections = set()
    coverage = {}
    full = fair_editor_context(sections, frame=frame, blueprint=blueprint, coverage=coverage)
    units = tuple(EvidenceUnit(
        f"editor-section-{index}", json.dumps({
            "title": section.get("title"), "markdown": section.get("markdown"),
            "claims": section.get("claims"), "citation_audit": section.get("citation_audit"),
        }, ensure_ascii=False), group=f"section-{index}", kind="section",
        level=int((section.get("context_processing") or {}).get("max_level", 0)), raw_text=True,
        # A chapter quote may select any claim. Inherit every known qualifier
        # conservatively so compression cannot promote an inference to fact.
        conditions=tuple(dict.fromkeys(condition for claim in section.get("claims") or []
                                       for condition in claim.get("conditions") or [])),
        counterevidence=tuple(dict.fromkeys(item for claim in section.get("claims") or []
                                            for item in claim.get("counterevidence") or [])),
        inference=any(claim.get("type") == "inference" for claim in section.get("claims") or []),
        general=any(claim.get("type") == "general" for claim in section.get("claims") or []),
    ) for index, section in enumerate(sections))

    def render(selected):
        legacy_complete = tuple(selected) == units and not coverage
        block = full if legacy_complete else "\n".join(
            _unit_text(unit) for unit in selected
        )
        # Frame and blueprint remain fixed audit contracts under compression.
        if not legacy_complete:
            block = json.dumps({"frame": frame, "blueprint": blueprint}, ensure_ascii=False) + "\n" + block
        return [{"role": "user", "content": report_summary_prompt(
            question, block, intent_block=intent_block,
        )}]

    prepared = runtime.prepare("report_summary", REPORT_SUMMARY_SCHEMA_HINT, render,
                               units, stage="final_editor",
                               max_tokens=engine.settings.report_summary_max_tokens)
    try:
        return prepared.chat_json()
    finally:
        limited = set(prepared.projection.omitted_handles) | set(prepared.projection.partial_handles)
        runtime.editor_limited_sections = {
            index for index, unit in enumerate(units) if unit.handle in limited
        }


def context_disclosure(engine):
    receipt = receipt_for(engine) or {}
    stages = receipt.get("stages") or {}
    omitted = sum(int(stage.get("omitted_items", 0)) for stage in stages.values())
    partial = sum(int(stage.get("partial_items", 0)) for stage in stages.values())
    invalid = sum(int(stage.get("invalid_items", 0)) for stage in stages.values())
    failed = [stage for stage, value in stages.items() if value.get("failed")]
    if not (receipt.get("compressed") or receipt.get("extra_calls") or omitted or partial or invalid or failed):
        return []
    text = (f"材料处理：已按模型容量分批整理，额外调用 {receipt.get('extra_calls', 0)} 次，"
            f"最多经过 {receipt.get('max_level', 0)} 层压缩；引证仍指向原始资料。")
    if omitted or partial or invalid:
        text += (f"各处理阶段累计有 {omitted} 项材料未纳入、{partial} 项仅使用部分原文、"
                 f"{invalid} 项整理结果未通过校验；结果不代表完整覆盖。")
    if failed:
        labels = {"planning_intent": "问题理解", "planning_outline": "大纲规划",
                  "planning_sufficiency": "资料充分性检查", "retrieve": "资料检索",
                  "draft": "章节撰写", "synthesis": "全篇综合", "final_editor": "摘要与审校"}
        text += "未完成的材料处理阶段：" + "、".join(labels[name] for name in failed) + "。"
    return ["> " + text, ""]
