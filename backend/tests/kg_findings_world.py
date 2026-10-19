"""A small synthetic KG on which every findings rule fires at least once
(design docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md
§四/§五), seeded with plain SQL so the SAME world runs on SQLite and
PostgreSQL (``tests/test_kg_findings_publish.py`` / ``tests/postgres/
test_kg_findings_publish_pg.py`` compare the two backends' published findings
row for row).

The world, by rule:

* core-0..core-3 — the main body: 12 concepts each from a shared pool, windows
  shifted by 8 (pairwise Jaccard 0.2: no duplicate), chained by relations into
  one giant component;
* off — an off-topic source: 12 concepts nobody else has, in their own island
  → F1;
* dup-a / dup-b — the same 12 concepts each (6 from core-0's window, 6 of
  their own) → F2 (J = 1.0), and neither is off-topic (they share with core-0);
* broken — 6 concepts whose names are single-character runs plus 6 core ones
  → F3 (fragmented names);
* "Llama 3.1" (core-0) / "Llama 31" (core-1): one squash key, two v2 spellings
  → F4. The "Llama 3.1" canonical's NAME comes from a knowhow member
  (its spelling "Llama-3.1" is never allowed into a row or the payload: names
  shown are the visible members' own);
  A second knowhow member ("Llama three-one") gives that canonical a v1 seed
  no visible name has: decisions keyed by it must still apply;
* "Mistral base" (core-2) sits in a canonical NAMED by a knowhow object
  ("Mistral 7.1"), which squash-collides with "Mistral 71" (core-3) on canonical
  names only: the visible names do not reproduce it, so no F4 row may appear
  for it (``groups_unreproducible``) — never under the hidden spelling;
* "Q-Former" (core-2) / "Qformer" (core-3): one v2 seed, two v1 seeds → S1;
* claims of core-0 with no relation → F5 (isolated objects).

Canonical ids are ``"K-" + concept_seed_v1(name)``, the production shape, so
stored merge decisions match by seed exactly as they do in production.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from app.domain.kg_merge_seed import concept_seed_v1
from app.models.schemas import NotebookCreate

T0 = "2026-10-19T08:00:00.123456+00:00"
# The knowhow object's spelling. It must share the group's squash key (the
# engine judges F4 on canonical names), so it is a plausible name — the test is
# that THIS spelling never reaches a row or the payload.
KNOWHOW_SECRET = "Llama-3.1"
# A second hidden member of the same canonical whose v1 seed is unlike any
# visible name's: a stored decision keyed by IT must still count (seeds of
# hidden members decide, design §4.4).
KNOWHOW_HIDDEN_SEED_NAME = "Llama three-one"
# A canonical named ONLY by a knowhow object: its visible member ("Mistral
# base") does not share the squash key, so the F4 group it would form with
# "Mistral 71" is not reproducible from visible names and must be dropped —
# never shown under the hidden spelling.
KNOWHOW_ONLY_NAME = "Mistral 7.1"
_CANONICAL_OVERRIDE = {"Mistral base": KNOWHOW_ONLY_NAME}
SECRETS = (KNOWHOW_SECRET, KNOWHOW_HIDDEN_SEED_NAME, KNOWHOW_ONLY_NAME)


def canonical_of(name: str) -> str:
    return "K-" + concept_seed_v1(name)


@dataclass
class World:
    notebook_id: str
    sources: List[str] = field(default_factory=list)
    # canonical id -> canonical name (concept_clusters.canonical_name)
    canonical_names: Dict[str, str] = field(default_factory=dict)


def _writer(db, postgres: bool):
    p = "%s" if postgres else "?"
    js = "%s::jsonb" if postgres else "?"

    def ins(table: str, columns: str, values: tuple, json_columns=()) -> None:
        names = [c.strip() for c in columns.split(",")]
        marks = ",".join(js if n in json_columns else p for n in names)
        db.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", values)

    return ins


def _concept_names() -> Dict[str, List[str]]:
    pool = [f"Core concept {i:02d}" for i in range(36)]
    names = {f"src-core-{i}": pool[8 * i: 8 * i + 12] for i in range(4)}
    names["src-core-0"] = names["src-core-0"] + ["Llama 3.1"]
    names["src-core-1"] = names["src-core-1"] + ["Llama 31"]
    names["src-core-2"] = names["src-core-2"] + ["Q-Former", "Mistral base"]
    names["src-core-3"] = names["src-core-3"] + ["Qformer", "Mistral 71"]
    names["src-off"] = [f"Unrelated topic {i:02d}" for i in range(12)]
    shared_dup = pool[0:6] + [f"Duplicate concept {i:02d}" for i in range(6)]
    names["src-dup-a"] = list(shared_dup)
    names["src-dup-b"] = list(shared_dup)
    names["src-broken"] = pool[12:18] + [
        " ".join(word) for word in ("llama", "mamba", "qwenx", "gemma", "phiab", "mistr")
    ]
    return names


def seed_world(repo, *, postgres: bool, community_min_size: int = 2) -> World:
    """Seed the world into a fresh notebook; returns its ids. The KG state
    carries kg_mutation_seq 5 / cluster_mutation_seq 2 (the published cluster
    generation is the birth generation 0)."""
    repo.settings.community_min_size = community_min_size
    notebook_id = repo.create_notebook(NotebookCreate(name="findings-world")).id
    world = World(notebook_id)
    names = _concept_names()
    p = "%s" if postgres else "?"
    with repo._runtime.database.write() as db:
        ins = _writer(db, postgres)
        for index, source_id in enumerate([*names, "src-kh"]):
            source_type = "knowhow" if source_id == "src-kh" else "markdown"
            ins("sources", "id,notebook_id,title,source_type,status,parse_status,"
                "chunked_at,created_at,updated_at",
                (source_id, notebook_id, f"Title of {source_id}", source_type,
                 "extracted", "parsed", T0,
                 f"2026-10-19T08:{index:02d}:00.000000+00:00", T0))
            world.sources.append(source_id)
        objects: Dict[Tuple[str, str], str] = {}
        serial = 0

        def obj(source_id: str, name: str, object_type: str = "concept") -> str:
            nonlocal serial
            serial += 1
            object_id = f"ko-{serial:04d}"
            ins("knowledge_objects", "id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at",
                (object_id, notebook_id, object_type, "approved",
                 json.dumps({"name": name}), "[]", source_id, T0, T0),
                json_columns=("payload", "evidence"))
            return object_id

        def cluster(object_id: str, canonical_id: str, canonical_name: str) -> None:
            ins("concept_clusters", "id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,created_at,generation",
                (f"cc-{object_id}", notebook_id, canonical_id, object_id,
                 canonical_name, "concept", T0, 0))

        for source_id, concept_names in names.items():
            for name in concept_names:
                object_id = obj(source_id, name)
                objects[(source_id, name)] = object_id
                canonical_name = _CANONICAL_OVERRIDE.get(name, name)
                canonical_id = canonical_of(canonical_name)
                if name == "Llama 3.1":
                    canonical_name = KNOWHOW_SECRET
                world.canonical_names[canonical_id] = canonical_name
                cluster(object_id, canonical_id, canonical_name)
        # the knowhow members: the one that names the "Llama 3.1" canonical, one
        # more of it with its own v1 seed, and the one naming "Mistral base"'s
        for name, canonical_id, canonical_name in (
            (KNOWHOW_SECRET, canonical_of("Llama 3.1"), KNOWHOW_SECRET),
            (KNOWHOW_HIDDEN_SEED_NAME, canonical_of("Llama 3.1"), KNOWHOW_SECRET),
            (KNOWHOW_ONLY_NAME, canonical_of(KNOWHOW_ONLY_NAME), KNOWHOW_ONLY_NAME),
        ):
            cluster(obj("src-kh", name), canonical_id, canonical_name)
        # isolated claims (F5): no relation, no cluster row
        for k in range(3):
            obj("src-core-0", f"Lonely claim {k}", object_type="claim")

        serial_rel = 0

        def rel(source_id: str, left: str, right: str) -> None:
            nonlocal serial_rel
            serial_rel += 1
            ins("knowledge_relations", "id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at",
                (f"kr-{serial_rel:04d}", notebook_id, source_id,
                 objects[(source_id, left)], objects[(source_id, right)],
                 "supports", "[]", T0),
                json_columns=("evidence",))

        for source_id, concept_names in names.items():
            # a chain inside every source; shared concepts join the core chains
            for left, right in zip(concept_names, concept_names[1:]):
                rel(source_id, left, right)
        db.execute(
            f"UPDATE unified_kg_state SET kg_mutation_seq=5, cluster_mutation_seq=2 "
            f"WHERE notebook_id={p}",
            (notebook_id,),
        )
    return world
