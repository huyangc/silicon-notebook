"""qiefen orchestrator: source_text -> QiefenDocument.

S1-S5 + do_not_extract are deterministic. When an LLM `client` is supplied and
configured, S6-S8 (objects/relations/mentions) run; otherwise those stay empty
and the output is the deterministic P0 document.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from app.services.qiefen import (
    atom_selector, atomizer, chunker, do_not_extract, mentions, objects,
    packager, relations,
)
from app.services.qiefen.models import EvidenceAtom, QiefenDocument, SourceMeta
from app.services.qiefen.profiles import extraction_targets
from app.services.qiefen.section_tree import build_section_tree
from app.services.qiefen.source_elements import parse_elements

# Safety cap so an unexpectedly section-rich chapter cannot explode LLM calls.
MAX_PACKAGES_PER_CHAPTER = 60
# Cap objects fed to the single per-chapter relation call.
MAX_RELATION_OBJECTS = 80
# Concurrent per-package object LLM calls (DeepSeek allows concurrency). The
# per-package calls are independent, so a thread pool turns ~N sequential calls
# into ~N/workers waves. Override with QIEFEN_LLM_WORKERS.
LLM_MAX_WORKERS = int(os.environ.get("QIEFEN_LLM_WORKERS", "8"))
# Experiment: LLM atom selection BEFORE chunking (textbook only). Off by default.
_ATOM_SELECT = os.environ.get("QIEFEN_ATOM_SELECT", "0") != "0"

_HIGH_VALUE_ATOM_TYPES = {
    "formula_atom", "table_header_atom", "table_row_atom", "table_caption_atom",
    "scaling_law_result_atom", "result_sentence", "method_sentence",
    "mechanism_sentence", "definition_atom", "concept_definition_atom",
    "design_principle_atom", "example_problem_atom", "problem_statement_atom",
}


def default_client() -> Any:
    """Build an OpenAICompatibleClient from settings (.env). Returns it even if
    unconfigured; the pipeline checks `.configured` before using it."""
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    return OpenAICompatibleClient(Settings())


def run(source_text: str, source_file: str, profile: str,
        line_range: Optional[List[int]] = None, source_id: str = "",
        title: str = "", scope: str = "", client: Any = None) -> QiefenDocument:
    elements = parse_elements(source_text, source_file, line_range)
    sections = build_section_tree(elements)

    # Map each element to its enclosing section (last heading at/above it).
    section_of: dict[str, str] = {}
    section_paths = {s.id: s.path for s in sections}
    cur_section = sections[0].id if sections else "SEC-0"
    # Non-heading elements inherit the last heading at/above their line.
    sec_by_line = sorted(
        [(e.line_start, s.id) for e, s in _pair_headings(elements, sections)],
    )
    for el in elements:
        sid = _section_for_line(el.line_start, sec_by_line)
        section_of[el.id] = sid or cur_section

    atoms: List[EvidenceAtom] = []
    for sid in _ordered_sections(elements, section_of, sections):
        sec_elements = [e for e in elements if section_of[e.id] == sid
                        and e.type != "heading"]
        atoms.extend(atomizer.atomize(source_text, sec_elements, sid, profile))

    # Optional general LLM atom selection BEFORE chunking, so every downstream
    # stage (chunks/packages/objects) is built on the curated atom set — no
    # alignment perturbation (unlike post-hoc curation). Profile-aware, applied
    # to both paper and textbook.
    if (_ATOM_SELECT and atoms
            and client is not None and getattr(client, "configured", False)):
        try:
            client.client()  # pre-warm before the selector's thread pool
        except Exception:
            pass
        keep = atom_selector.select_core_atoms(client, atoms, profile, LLM_MAX_WORKERS)
        if keep:
            atoms = [a for a in atoms if a.id in keep]

    chunks = chunker.build_chunks(atoms, profile, section_paths)
    atoms_by_id = {a.id: a for a in atoms}
    packages = packager.build_packages(chunks, atoms_by_id, title, profile)
    for ch in chunks:
        ch.extraction_targets = extraction_targets(profile)
    for pkg in packages:
        pkg.extraction_targets = extraction_targets(profile)
    dne = do_not_extract.detect_negatives(atoms)

    objs: List = []
    rels: List = []
    mens: List = []
    canon: List = []
    if client is not None and getattr(client, "configured", False):
        objs, rels, mens, canon = _run_llm_stages(
            client, packages, atoms_by_id, profile)

    return QiefenDocument(
        source_meta=SourceMeta(source_id=source_id, profile=profile, title=title,
                               source_file=source_file,
                               source_line_range=line_range or [],
                               scope=scope,
                               extraction_targets=extraction_targets(profile)),
        section_tree=sections, evidence_atoms=atoms, semantic_chunks=chunks,
        context_packages=packages, objects=objs, relations=rels,
        mentions=mens, canonicalization=canon, do_not_extract=dne,
    )


def _package_value(pkg) -> int:
    return sum(1 for a in (pkg.atoms or [])
              if a.get("atom_type") in _HIGH_VALUE_ATOM_TYPES)


def _run_llm_stages(client, packages, atoms_by_id, profile):
    atom_text = {aid: a.raw_text for aid, a in atoms_by_id.items()}
    # Prioritize the most content-bearing packages, then cap.
    selected = sorted(packages, key=_package_value, reverse=True)[:MAX_PACKAGES_PER_CHAPTER]
    selected_ids = {p.id for p in selected}

    # Pre-warm the lazily-built OpenAI client so concurrent threads don't race
    # to initialize it.
    try:
        client.client()
    except Exception:
        pass

    # Object extraction per package is independent -> run concurrently.
    def _extract(pkg):
        return pkg.id, objects.extract_objects(client, pkg, profile, atom_text)

    objs_by_pkg = {}
    if selected:
        workers = max(1, min(LLM_MAX_WORKERS, len(selected)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for pid, pkg_objs in pool.map(_extract, selected):
                objs_by_pkg[pid] = pkg_objs
    # Rebuild in deterministic (selected) order regardless of completion order.
    all_objects = []
    for pkg in selected:
        all_objects.extend(objs_by_pkg.get(pkg.id, []))

    # Backfill each package's expected_objects (only the packages we extracted).
    for pkg in packages:
        if pkg.id in selected_ids:
            pkg.expected_objects = [o.id for o in objs_by_pkg.get(pkg.id, [])]

    rels = relations.extract_relations(
        client, all_objects[:MAX_RELATION_OBJECTS], profile, atom_text)
    mens = mentions.derive_mentions(all_objects)
    canon = mentions.build_canonicalization(mens)
    return all_objects, rels, mens, canon


def _pair_headings(elements, sections):
    headings = [e for e in elements if e.type == "heading"]
    return list(zip(headings, sections))


def _section_for_line(line, sec_by_line):
    chosen = None
    for hline, sid in sec_by_line:
        if hline <= line:
            chosen = sid
        else:
            break
    return chosen


def _ordered_sections(elements, section_of, sections):
    seen = []
    for e in elements:
        sid = section_of[e.id]
        if sid not in seen:
            seen.append(sid)
    return seen
