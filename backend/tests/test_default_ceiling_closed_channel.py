"""With the Memory channel closed, no non-partitioned channel surfaces the
asker's own Memory (real SQLite store).

Third E1-1 review, P2-3: with ``memory_access_context(False)`` the default
ceiling withholds the asker's own Memory sources.  Without a ceiling of their
own, the 1-hop walk rendered a Memory-derived node name and relation chain
into ``kg_block`` -- the answer prompt -- behind a live anchor, and PPR /
exact lookup put the Memory passage into their raw candidates; PR-E1 switched
those four channels off for such a run.  PR-E2 gives each its ceiling (the
walk judges the current notebook's nodes, PPR filters before its cut, exact
lookup pushes the ceiling into its probe), so the channels stay on and this
pins, on a real store, that nothing of the asker's Memory leaks -- with the
open channel as the control that shows the fixture does reach the
Memory-derived rows.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import default_ceiling_context, memory_access_context
from tests.model_testkit import bind_all_embedding_clients
from tests.test_default_source_ceiling import real_readers

NOW = "2026-09-30T00:00:00"
QUERY = "ZEBRAQUARTZ Mixture-of-Experts MoE"
EXACT_QUERY = "zebra_quartz_cmd 命令是怎样的"
MARKERS = ("ZEBRAQUARTZ", "SECRETMEMO")


def _evidence(source_id: str, element_id: str, quote: str) -> str:
    return json.dumps([{
        "source_id": source_id, "source_title": "", "element_id": element_id,
        "element_type": "paragraph", "location_label": "p1",
        "quoted_span": quote, "confidence": 1.0,
    }])


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Bob's notebook: one ordinary source (concept e1) and his own confirmed
    Memory projected as ``src-mem`` with two Memory-derived concepts (e2 in
    e1's cluster, e3 "ZEBRAQUARTZ plan"), a Memory-derived relation e1 -> e3,
    a Memory passage, and a Memory section named ``zebra_quartz_cmd``."""
    from app.services.sqlite_repository import (
        SQLiteRepository, reset_request_user, set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'closed.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    bob = repo.create_user("b00654321", "password-12")
    # Reset in teardown: the request user is a context variable, and leaking
    # it makes every later test in this worker write rows as a user its own
    # database does not have (FOREIGN KEY failures far away from here).
    token = set_request_user(bob)
    nb = repo.create_notebook(NotebookCreate(name="kb")).id
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed',?,?,?,?)",
            ("mem-1", nb, bob.id, "m", "SECRETMEMO", NOW, NOW),
        )
        for sid, title, kind, memory_id in (
            ("src-doc", "DeepSeek paper", "md", None),
            ("src-mem", "Bob memory", "memory", "mem-1"),
        ):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, nb, title, kind, "ready", memory_id, NOW, NOW),
            )
        for cid, sid, text, section, element in (
            ("cA", "src-doc",
             "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
             "Arch", "elA"),
            ("cM", "src-mem",
             "SECRETMEMO: ZEBRAQUARTZ is Bob's private Mixture-of-Experts (MoE) plan.",
             "Plan", "elM"),
            ("cX", "src-mem", "zebra_quartz_cmd SECRETMEMO arguments: --private",
             "zebra_quartz_cmd", "elX"),
        ):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, nb, sid, text, section, json.dumps([element]), NOW),
            )
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (cid, nb, text),
            )
        for oid, sid, element, name in (
            ("e1", "src-doc", "elA", "Mixture-of-Experts (MoE)"),
            ("e2", "src-mem", "elM", "Mixture-of-Experts (MoE)"),
            ("e3", "src-mem", "elM", "ZEBRAQUARTZ plan"),
        ):
            quote = "SECRETMEMO ZEBRAQUARTZ" if sid == "src-mem" else "MoE"
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb, "concept", "approved", "", json.dumps({"name": name}),
                 _evidence(sid, element, quote), sid, NOW, NOW),
            )
            # The reverse index, as the store writes it: a new notebook's is
            # certified, so a reader trusting it sees an object without these
            # rows as sourceless.
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (?,?,?)", (oid, sid, nb),
            )
        for oid in ("e1", "e2"):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"cl-{oid}", nb, "K-moe", oid, "Mixture-of-Experts (MoE)",
                 "concept", NOW),
            )
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("rM", nb, "src-mem", "e1", "e3", "kind_of",
             _evidence("src-mem", "elM", "SECRETMEMO ZEBRAQUARTZ"), NOW),
        )
    try:
        yield repo, nb, bob.id
    finally:
        reset_request_user(token)


def _leaks(value) -> list[str]:
    text = repr(value)
    return [marker for marker in MARKERS if marker in text]


def test_open_channel_control_reaches_the_memory_derived_rows(seeded):
    """Control: with the channel open the same fixture does put the asker's own
    Memory-derived node into ``kg_block``, and PPR and exact lookup do reach
    the Memory passages -- so the closed-channel assertions below are not
    vacuous."""
    repo, nb, bob = seeded
    service = repo.retrieval
    with default_ceiling_context(nb, bob, real_readers(repo)):
        assert service.candidates._unsafe_source_scope_restricted(nb) is False
        _chunks, block, id_map, _hits, ppr_count = service.mixed_chunk_candidates(
            nb, QUERY, QUERY, [QUERY],
        )
        assert "ZEBRAQUARTZ" in block
        assert ppr_count > 0
        assert _leaks(service.candidates._exact_lookup_chunks(nb, EXACT_QUERY))


def test_closed_channel_keeps_the_channels_open_and_leaks_nothing(seeded):
    """With the Memory channel closed the four channels stay ON (a withheld
    source is not drift) and none of them surfaces the asker's own Memory:
    the walk judges the current notebook's nodes by the ceiling, PPR applies
    it before its cut, exact lookup pushes it into its probe, and the
    relation channel's result boundary drops the Memory-derived relation."""
    repo, nb, bob = seeded
    service = repo.retrieval
    candidates = service.candidates
    with memory_access_context(False), default_ceiling_context(
        nb, bob, real_readers(repo),
    ):
        assert candidates._unsafe_source_scope_restricted(nb) is False
        chunks, block, id_map, hits, ppr_count = service.mixed_chunk_candidates(
            nb, QUERY, QUERY, [QUERY],
        )
        raw = candidates._mix_retrieve(nb, QUERY, QUERY, [QUERY])
        # The walk ran (the paper's node is rendered) ...
        assert "Mixture-of-Experts" in block
        # ... but the answer prompt's graph block holds no Memory-derived node
        # name, no relation chain through it, no live anchor for it.
        assert "ZEBRAQUARTZ" not in block and "kind_of" not in block
        assert not {"e2", "e3"} & {
            str((entry or {}).get("object_id") or "") for entry in id_map.values()
        }
        assert not _leaks(raw[1])
        # PPR runs and gives its slots to in-ceiling passages only.
        assert raw[4] > 0 and ppr_count > 0
        assert not _leaks([chunk.text for chunk in raw[0]])
        # Exact lookup runs with the ceiling in its probe: the Memory section
        # named ``zebra_quartz_cmd`` is never found.
        assert not _leaks(candidates._exact_lookup_chunks(nb, EXACT_QUERY))
        assert "rM" not in {
            relation.relation_id
            for relation in candidates.federated_retrieve_relations(nb, QUERY)
        }
        assert not _leaks([chunk.text for chunk in chunks])
        assert not {"e2", "e3"} & {hit.object_id for hit in hits}
