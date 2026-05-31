#!/usr/bin/env python3
"""Validator for ch09_switched_capacitor gold.yaml (schema v0.3.3-textbook invariants).

Checks: dual-span verbatim; structural refs; chunk coverage; package == chunk
atoms; expected_objects subset of objects; local in / supporting out of home
chunk; object in home expected_objects; relation endpoints in objects; no
confidence; numeric over-span audit; formula/condition normalized no gloss-only
note (formula normalized must be formula-only -- enforced via no out-of-span
numbers); GLOBAL no-orphan (object/relation evidence OR context_only:true);
expected_local_fields subset of payload.
"""
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = ("/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md")

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


_NUM = re.compile(r"\d+(?:\.\d+)?")
# MinerU spaces digits inside math ('9 . 9 0 1', '1 0 ^ {- 5}', '0. 6 2 8 3').
# Collapse whitespace that sits between digits or around a decimal point so the
# spaced source form '9 . 9 0 1' matches the normalized ascii '9.901'.
_DIGIT_SPACE = re.compile(r"(?<=[\d.])\s+(?=[\d.])")
_SPACE_DOT = re.compile(r"\s*\.\s*")


def _collapse_digits(text):
    text = _SPACE_DOT.sub(".", text)
    text = _DIGIT_SPACE.sub("", text)
    return text


def numbers(text):
    return set(_NUM.findall(text))


def raw_numbers(text):
    # numbers present in raw, accounting for MinerU intra-number spacing.
    return numbers(text) | numbers(_collapse_digits(text))


def main():
    src = open(SOURCE_FILE, encoding="utf-8").read()
    viewer = open(os.path.join(HERE, "source.md"), encoding="utf-8").read()
    g = yaml.safe_load(open(os.path.join(HERE, "gold.yaml"), encoding="utf-8"))

    check(g.get("schema_version") == "0.3.3-textbook", "schema_version must be '0.3.3-textbook'")

    atoms = g["evidence_atoms"]
    atom_ids = {a["id"] for a in atoms}
    se_ids = {e["id"] for e in g["source_elements"]}
    sec_ids = {s["id"] for s in g["section_tree"]}
    objects = g["objects"]
    object_ids = {o["id"] for o in objects}
    chunks = g["semantic_chunks"]
    chunk_by_id = {c["id"]: c for c in chunks}
    packages = g["context_packages"]

    # 2/3. spans + structural refs + numeric audit
    for a in atoms:
        ss = a["source_span"]
        raw = a["raw_text"]
        check(src[ss["char_start"]:ss["char_end"]] == raw, "source_span mismatch for %s" % a["id"])
        vs = a.get("viewer_span")
        if vs:
            check(vs.get("viewer_only") is True, "viewer_span.viewer_only must be true for %s" % a["id"])
            check(viewer[vs["char_start"]:vs["char_end"]] == raw, "viewer_span mismatch for %s" % a["id"])
        check(a["section_id"] in sec_ids, "%s.section_id not in section_tree" % a["id"])
        check(a["source_element_id"] in se_ids, "%s.source_element_id not in source_elements" % a["id"])
        check("confidence" not in a, "%s has confidence" % a["id"])
        # formula/condition normalized must be formula-only (no trailing gloss).
        if a["atom_type"] in ("formula_atom", "condition_atom"):
            check("   (" not in a["normalized_text"],
                  "%s formula normalized carries a gloss (should be formula-only)" % a["id"])
        # numeric audit: non-formula normalized numbers must be in raw span.
        if a["atom_type"] != "formula_atom":
            raw_nums = raw_numbers(raw)
            for n in numbers(a["normalized_text"]):
                check(n in raw_nums,
                      "NUMERIC over-span: %s normalized has %r absent from raw span" % (a["id"], n))

    # 5. chunk coverage
    atoms_in_chunk = set()
    for c in chunks:
        for aid in c["atom_ids"]:
            atoms_in_chunk.add(aid)
            check(aid in atom_ids, "chunk %s references unknown atom %s" % (c["id"], aid))
        for k in ("central_atom_ids", "gold_must_cover_atoms"):
            for aid in c.get(k, []):
                check(aid in c["atom_ids"], "chunk %s.%s atom %s not in atom_ids" % (c["id"], k, aid))
    for a in atoms:
        check(a["id"] in atoms_in_chunk, "atom %s not in any chunk" % a["id"])

    # 6. packages
    pkg_by_id = {}
    for p in packages:
        pkg_by_id[p["id"]] = p
        chunk = chunk_by_id[p["chunk_id"]]
        pkg_atom_ids = [x["atom_id"] for x in p["atoms"]]
        check(pkg_atom_ids == chunk["atom_ids"],
              "package %s atoms != chunk %s atom_ids" % (p["id"], p["chunk_id"]))
        for oid in p.get("expected_objects", []):
            check(oid in object_ids, "package %s expected_object %s not an object" % (p["id"], oid))
        for oid, fields in (p.get("expected_local_fields", {}) or {}).items():
            obj = next((o for o in objects if o["id"] == oid), None)
            check(obj is not None, "expected_local_fields object %s missing" % oid)
            if obj:
                for f in fields:
                    check(f in obj["payload"], "expected_local_fields %s field %s not in payload" % (oid, f))

    # 7. object evidence
    for o in objects:
        check("confidence" not in o, "object %s has confidence" % o["id"])
        check(o.get("gold_label") is True, "object %s missing gold_label" % o["id"])
        check(o.get("difficulty") in ("easy", "medium", "hard"), "object %s bad difficulty" % o["id"])
        check(o.get("evidence_strength") in ("direct", "indirect", "cross_reference"),
              "object %s bad evidence_strength" % o["id"])
        hp = o["home_package"]
        check(hp in pkg_by_id, "object %s home_package %s missing" % (o["id"], hp))
        home_chunk_atoms = set(chunk_by_id[pkg_by_id[hp]["chunk_id"]]["atom_ids"]) if hp in pkg_by_id else set()
        for aid in o.get("local_evidence_atom_ids", []):
            check(aid in atom_ids, "object %s local atom %s unknown" % (o["id"], aid))
            check(aid in home_chunk_atoms, "object %s local atom %s NOT in home chunk" % (o["id"], aid))
        for aid in o.get("supporting_context_atom_ids", []):
            check(aid in atom_ids, "object %s supporting atom %s unknown" % (o["id"], aid))
            check(aid not in home_chunk_atoms, "object %s supporting atom %s IS in home chunk" % (o["id"], aid))
        if hp in pkg_by_id:
            check(o["id"] in pkg_by_id[hp].get("expected_objects", []),
                  "object %s not in home_package %s expected_objects" % (o["id"], hp))
        for ch in (o["payload"].get("children", []) if isinstance(o["payload"], dict) else []):
            check(ch in object_ids, "object %s child %s not an object" % (o["id"], ch))

    # 8. relations
    for r in g["relations"]:
        check("confidence" not in r, "relation %s has confidence" % r["id"])
        check(r.get("gold_label") is True, "relation %s missing gold_label" % r["id"])
        check(r.get("difficulty") in ("easy", "medium", "hard"), "relation %s bad difficulty" % r["id"])
        check(r.get("evidence_strength") in ("direct", "indirect", "cross_reference"),
              "relation %s bad evidence_strength" % r["id"])
        check(r["source_object_id"] in object_ids, "relation %s bad source" % r["id"])
        check(r["target_object_id"] in object_ids, "relation %s bad target" % r["id"])
        for aid in r["evidence_atom_ids"]:
            check(aid in atom_ids, "relation %s atom %s unknown" % (r["id"], aid))

    # 9. mentions
    for m in g["mentions"]:
        check(m["atom_id"] in atom_ids, "mention %s atom %s unknown" % (m["id"], m["atom_id"]))

    # 10. do_not_extract refs
    for d in g["do_not_extract"]:
        for key in ("ref", "atom_id"):
            if key in d:
                check(d[key] in atom_ids, "do_not_extract %s=%s not an atom" % (key, d[key]))

    # GLOBAL no-orphan: object/relation evidence OR context_only:true
    used = set()
    for o in objects:
        used |= set(o.get("local_evidence_atom_ids", []))
        used |= set(o.get("supporting_context_atom_ids", []))
    for r in g["relations"]:
        used |= set(r["evidence_atom_ids"])
    context_only = {a["id"] for a in atoms if a.get("context_only")}
    for a in atoms:
        check(a["id"] in used or a["id"] in context_only,
              "ORPHAN atom %s (in no object/relation evidence, not context_only)" % a["id"])

    if errors:
        print("FAIL: %d error(s)" % len(errors))
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("ALL CHECKS PASS")
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d do_not_extract=%d" % (
        len(atoms), len(g["source_elements"]), len(chunks),
        len(packages), len(objects), len(g["relations"]), len(g["mentions"]), len(g["do_not_extract"])))


if __name__ == "__main__":
    main()
