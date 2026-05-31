"""Deterministic P0 orchestrator: source_text -> QiefenDocument (S1..S5 + DNE)."""
from __future__ import annotations

from typing import List, Optional

from app.services.qiefen import atomizer, chunker, do_not_extract, packager
from app.services.qiefen.models import EvidenceAtom, QiefenDocument, SourceMeta
from app.services.qiefen.profiles import extraction_targets
from app.services.qiefen.section_tree import build_section_tree
from app.services.qiefen.source_elements import parse_elements


def run(source_text: str, source_file: str, profile: str,
        line_range: Optional[List[int]] = None, source_id: str = "",
        title: str = "", scope: str = "") -> QiefenDocument:
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

    chunks = chunker.build_chunks(atoms, profile, section_paths)
    atoms_by_id = {a.id: a for a in atoms}
    packages = packager.build_packages(chunks, atoms_by_id, title, profile)
    for ch in chunks:
        ch.extraction_targets = extraction_targets(profile)
    for pkg in packages:
        pkg.extraction_targets = extraction_targets(profile)
    dne = do_not_extract.detect_negatives(atoms)

    return QiefenDocument(
        source_meta=SourceMeta(source_id=source_id, profile=profile, title=title,
                               source_file=source_file,
                               source_line_range=line_range or [],
                               scope=scope,
                               extraction_targets=extraction_targets(profile)),
        section_tree=sections, evidence_atoms=atoms, semantic_chunks=chunks,
        context_packages=packages, do_not_extract=dne,
    )


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
