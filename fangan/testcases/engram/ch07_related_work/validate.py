#!/usr/bin/env python3
"""Validator for ch00_abstract gold.yaml (schema v0.3.3 invariants)."""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def main():
    src = open(SOURCE_FILE, encoding="utf-8").read()
    viewer = open(os.path.join(HERE, "source.md"), encoding="utf-8").read()
    g = yaml.safe_load(open(os.path.join(HERE, "gold.yaml"), encoding="utf-8"))

    # 1. yaml parses (already did)
    check(g.get("schema_version") == "0.3.3", "schema_version must be '0.3.3'")

    atoms = g["evidence_atoms"]
    atom_ids = {a["id"] for a in atoms}
    se_ids = {e["id"] for e in g["source_elements"]}
    sec_ids = {s["id"] for s in g["section_tree"]}
    objects = g["objects"]
    object_ids = {o["id"] for o in objects}
    chunks = g["semantic_chunks"]
    chunk_by_id = {c["id"]: c for c in chunks}
    packages = g["context_packages"]

    # 2/3. spans
    for a in atoms:
        ss = a["source_span"]
        raw = a["raw_text"]
        check(src[ss["char_start"]:ss["char_end"]] == raw,
              "source_span mismatch for %s" % a["id"])
        vs = a.get("viewer_span")
        if vs:
            check(vs.get("viewer_only") is True, "viewer_span.viewer_only must be true for %s" % a["id"])
            check(viewer[vs["char_start"]:vs["char_end"]] == raw,
                  "viewer_span mismatch for %s" % a["id"])
        # 4. structural refs
        check(a["section_id"] in sec_ids, "%s.section_id not in section_tree" % a["id"])
        check(a["source_element_id"] in se_ids, "%s.source_element_id not in source_elements" % a["id"])
        # 13. no confidence on atoms (sanity)
        check("confidence" not in a, "%s has confidence" % a["id"])

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
        # expected_local_fields subset of payload
        for oid, fields in p.get("expected_local_fields", {}).items():
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
        local = o.get("local_evidence_atom_ids", [])
        supporting = o.get("supporting_context_atom_ids", [])
        for aid in local:
            check(aid in atom_ids, "object %s local atom %s unknown" % (o["id"], aid))
            check(aid in home_chunk_atoms, "object %s local atom %s NOT in home chunk" % (o["id"], aid))
        for aid in supporting:
            check(aid in atom_ids, "object %s supporting atom %s unknown" % (o["id"], aid))
            check(aid not in home_chunk_atoms, "object %s supporting atom %s IS in home chunk" % (o["id"], aid))
        # object in its home_package.expected_objects
        check(o["id"] in pkg_by_id[hp].get("expected_objects", []),
              "object %s not in home_package %s expected_objects" % (o["id"], hp))
        # payload.children must be objects
        for ch in o["payload"].get("children", []) if isinstance(o["payload"], dict) else []:
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

    # GLOBAL no-orphan
    used = set()
    for o in objects:
        used |= set(o.get("local_evidence_atom_ids", []))
        used |= set(o.get("supporting_context_atom_ids", []))
    for r in g["relations"]:
        used |= set(r["evidence_atom_ids"])
    context_only = {a["id"] for a in atoms if a.get("metadata", {}) and a["metadata"].get("context_only")}
    for a in atoms:
        check(a["id"] in used or a["id"] in context_only,
              "ORPHAN atom %s (in no object/relation evidence, not context_only)" % a["id"])

    if errors:
        print("FAIL: %d error(s)" % len(errors))
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("ALL CHECKS PASS")
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d" % (
        len(atoms), len(g["source_elements"]), len(chunks),
        len(packages), len(objects), len(g["relations"])))


if __name__ == "__main__":
    main()
