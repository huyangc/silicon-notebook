#!/usr/bin/env python3
"""One-off migration: drop `attrs` from generated gold drafts. `attrs` is deferred
until its shape is decided (see fangan_todo.md "KG 重构"). A node now carries only
id/type/name/section_path/evidence/mentions, with `name` holding the node's text.

If an earlier pass moved a Claim's full statement into attrs.statement (and
truncated `name`), restore it into `name` first so no text is lost. No LLM.

Usage: PYTHONPATH=backend python scripts/kg_strip_attrs.py
"""
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services.kg.models import KnowledgeGraph  # noqa: E402
from app.services.kg.emit import to_yaml  # noqa: E402

OUT = REPO / "fangan" / "testcases_kg"


def main():
    n_files = n_nodes = 0
    for f in sorted(OUT.glob("*/ch*/gold_kg.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        stripped = 0
        for nd in data.get("nodes", []):
            attrs = nd.pop("attrs", None)
            if not attrs:
                continue
            stripped += 1
            # restore full Claim text that a prior pass parked in attrs.statement
            if nd.get("type") == "Claim" and attrs.get("statement"):
                nd["name"] = attrs["statement"]
        for ed in data.get("edges", []):
            ed.pop("attrs", None)
        g = KnowledgeGraph(**data)  # model no longer has attrs -> clean re-emit
        f.write_text(to_yaml(g), encoding="utf-8")
        n_files += 1
        n_nodes += stripped
        print(f"{f.parent.relative_to(OUT)}: {stripped} nodes stripped")
    print(f"--- {n_nodes} nodes across {n_files} files")


if __name__ == "__main__":
    main()
