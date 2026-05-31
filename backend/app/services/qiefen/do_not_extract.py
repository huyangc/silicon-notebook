"""Deterministic negative control: surfaces that must NOT become knowledge
objects (URLs, author-year citations, figure/table cross-references)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.services.qiefen.models import EvidenceAtom

_URL = re.compile(r"https?://\S+")
_CITATION = re.compile(r"\([A-Z][A-Za-z]+ et al\.?,?\s*\d{4}\)")
_FIGREF = re.compile(r"\b(?:see |in )?(Figure|Table)\s+\d+\b")


def detect_negatives(atoms: List[EvidenceAtom]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    citation_examples: List[str] = []
    for a in atoms:
        for url in _URL.findall(a.raw_text):
            entries.append({"text": url.rstrip(".,);"), "atom_id": a.id,
                            "reason": "Resource URL, not a knowledge object.",
                            "kind": "out_of_slice_reference"})
        for cit in _CITATION.findall(a.raw_text):
            if cit not in citation_examples:
                citation_examples.append(cit)
    if citation_examples:
        entries.append({"pattern": "inline_author_year_citation",
                        "examples": citation_examples,
                        "reason": "inline author-year citations are not knowledge objects.",
                        "kind": "citation_policy"})
    return entries
