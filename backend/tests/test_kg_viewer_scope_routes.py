"""PR-A·A5 through the REAL routes and auth dependency (rulings Q4 / M1).

Two registered users over HTTP (``auth_optional=false``): the notebook owner,
and a member who owns a confirmed Memory.  The three detail endpoints
(object context, concept detail, neighbours) must hide the member's Memory
from the notebook OWNER and from a deployment ADMIN alike — neither role is a
bypass — while the Memory's owner sees all of it.  A mounted library read by
a user who is not a member of that library exposes its visible sources only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_kg_viewer_scope import _ev, _memory, _object_id, _source


@pytest.fixture
def two_users_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_a5.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "api_storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    client = TestClient(create_app())

    def register(username):
        body = client.post(
            "/api/auth/register", json={"username": username, "password": "pw"}
        ).json()
        return body["user"]["id"], {"Authorization": f"Bearer {body['token']}"}

    owner_id, owner = register("a00900301")
    member_id, member = register("a00900302")
    nb = client.post("/api/notebooks", headers=owner, json={"name": "shared"}).json()["id"]
    repo = repository()
    repo.add_member(nb, member_id)
    with repo._write() as db:
        _source(db, nb, "src-s", elements=[("el-s-occ", "VISIBLE occ")])
        _memory(db, nb, "mem-m", member_id)
        _source(db, nb, "src-mm", memory_id="mem-m", elements=[
            ("el-mm-occ", "M-PRIVATE occ"), ("el-mm-secret", "M-PRIVATE secret")])
    repo.store_kg(nb, "src-s", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
    ], [])
    repo.store_kg(nb, "src-mm", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "MSEC"},
         "evidence": [_ev("src-mm", "el-mm-occ")]},
        {"local_id": "x", "object_type": "concept",
         "payload": {"name": "SecretProject", "section_path": "MSEC"},
         "evidence": [_ev("src-mm", "el-mm-secret")]},
        # Owned by the Memory, evidenced only by the visible source (a
        # promoted/merged object): hidden by the owner column alone.
        {"local_id": "p", "object_type": "claim",
         "payload": {"name": "M-PRIVATE promoted claim", "section_path": "MSEC"},
         "evidence": [_ev("src-s", "el-s-occ")]},
    ], [{"source_local_id": "x", "target_local_id": "c", "edge_type": "related_to",
         "evidence": []}])
    repo.rebuild_unified_kg(nb)
    with repo._write() as db:
        ids = SimpleNamespace(
            engram_s=_object_id(db, nb, "Engram", "src-s"),
            engram_m=_object_id(db, nb, "Engram", "src-mm"),
            secret=_object_id(db, nb, "SecretProject", "src-mm"),
            promoted=_object_id(db, nb, "M-PRIVATE promoted claim", "src-mm"),
        )
    cmap = repo.cluster_map(nb)
    ids.engram_c = cmap[ids.engram_s]
    ids.secret_c = cmap[ids.secret]
    return SimpleNamespace(
        client=client, register=register, repo=repo, nb=nb, ids=ids,
        owner_id=owner_id, owner=owner, member_id=member_id, member=member,
    )


def _assert_filtered(env, headers):
    c, nb, ids = env.client, env.nb, env.ids
    for oid in (ids.secret, ids.engram_m, ids.promoted):
        r = c.get(f"/api/notebooks/{nb}/objects/{oid}/context", headers=headers)
        assert r.status_code == 404, (oid, r.text)
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.engram_s}/context", headers=headers)
    assert r.status_code == 200 and "M-PRIVATE" not in r.text, r.text
    r = c.get(f"/api/notebooks/{nb}/concepts/{ids.secret_c}/detail", headers=headers)
    assert r.status_code == 404, r.text
    r = c.get(f"/api/notebooks/{nb}/concepts/{ids.engram_c}/detail", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["id"] for m in body["members"]] == [ids.engram_s]
    assert body["member_total"] == 1
    assert "M-PRIVATE" not in r.text and "MSEC" not in r.text
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.engram_s}/neighbors", headers=headers)
    assert r.status_code == 200
    assert "SecretProject" not in r.text and ids.secret_c not in r.text
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.secret}/neighbors", headers=headers)
    assert r.status_code == 200
    assert r.json()["nodes"] == [] and r.json()["edges"] == []


def test_notebook_owner_gets_no_bypass(two_users_client):
    _assert_filtered(two_users_client, two_users_client.owner)


def test_deployment_admin_gets_no_bypass(two_users_client):
    env = two_users_client
    admin_id, admin = env.register("a00900303")
    with env.repo._write() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (admin_id,))
    env.repo.add_member(env.nb, admin_id)
    assert env.client.get("/api/me", headers=admin).json()["role"] == "admin"
    _assert_filtered(env, admin)


def test_the_memory_owner_sees_everything(two_users_client):
    env = two_users_client
    c, nb, ids, member = env.client, env.nb, env.ids, env.member
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.secret}/context", headers=member)
    assert r.status_code == 200 and "M-PRIVATE secret" in r.text, r.text
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.promoted}/context", headers=member)
    assert r.status_code == 200, r.text
    body = c.get(f"/api/notebooks/{nb}/concepts/{ids.engram_c}/detail", headers=member).json()
    assert {m["id"] for m in body["members"]} == {ids.engram_s, ids.engram_m}
    assert body["member_total"] == 2
    r = c.get(f"/api/notebooks/{nb}/concepts/{ids.secret_c}/detail", headers=member)
    assert r.status_code == 200
    r = c.get(f"/api/notebooks/{nb}/objects/{ids.engram_s}/neighbors", headers=member)
    assert "SecretProject" in r.text


def test_mounted_library_exposes_visible_sources_only_to_a_non_member(two_users_client):
    """Ruling: a mounted library's hidden half (its Knowhow projections, and
    any Memory) is scoped to that library's members.  The owner mounts a
    library of their own into the shared notebook; the member of the shared
    notebook is not a member of the library."""
    env = two_users_client
    c, repo = env.client, env.repo
    lib = c.post("/api/notebooks", headers=env.owner, json={"name": "lib"}).json()["id"]
    with repo._write() as db:
        _source(db, lib, "src-lib", elements=[("el-lib", "LIB visible")])
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,"
            "updated_at) VALUES ('src-kh',?,'kh','knowhow','extracted','parsed','',"
            "'',0,'','','','2026-09-01T00:00:00','2026-09-01T00:00:00')", (lib,),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES ('el-kh','src-kh','paragraph','p',"
            "'KNOWHOW row text','{}','2026-09-01T00:00:00')",
        )
    repo.store_kg(lib, "src-lib", [
        {"local_id": "v", "object_type": "concept",
         "payload": {"name": "LibVisible", "section_path": "1"},
         "evidence": [_ev("src-lib", "el-lib")]}], [])
    repo.store_kg(lib, "src-kh", [
        {"local_id": "k", "object_type": "concept",
         "payload": {"name": "KNOWHOW concept", "section_path": "1"},
         "evidence": [_ev("src-kh", "el-kh")]}], [])
    repo.rebuild_unified_kg(lib)
    mounted = c.put(f"/api/notebooks/{env.nb}/bases", headers=env.owner,
                    json={"base_notebook_ids": [lib]})
    assert mounted.status_code == 200, mounted.text
    with repo._write() as db:
        visible = _object_id(db, lib, "LibVisible", "src-lib")
        knowhow = _object_id(db, lib, "KNOWHOW concept", "src-kh")
    url = f"/api/notebooks/{env.nb}/objects/{{}}/context?source_notebook_id={lib}"
    # Not a member of the library: visible sources only.
    assert c.get(url.format(knowhow), headers=env.member).status_code == 404
    r = c.get(url.format(visible), headers=env.member)
    assert r.status_code == 200 and "LIB visible" in r.text
    # The library's owner is its member: its Knowhow is readable.
    r = c.get(url.format(knowhow), headers=env.owner)
    assert r.status_code == 200 and "KNOWHOW row text" in r.text


def test_routes_apply_the_rule_to_clusters_and_legacy_siblings(two_users_client):
    """codex #806 r1, over HTTP as the notebook owner: a cluster whose only
    member is owned by the visible source but cites only the member's Memory
    (finding 1), a legacy sibling procedure owned by that Memory (finding 2)
    and a legacy evidence item naming the visible source while pointing at a
    Memory element (finding 3)."""
    import json

    env = two_users_client
    c, repo, nb = env.client, env.repo, env.nb
    repo.store_kg(nb, "src-s", [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        {"local_id": "ph", "object_type": "concept",
         "payload": {"name": "M-PRIVATE Phantom", "section_path": "1"},
         "evidence": [_ev("src-mm", "el-mm-secret")]},
    ], [{"source_local_id": "ph", "target_local_id": "hub", "edge_type": "related_to",
         "evidence": []}])
    repo.rebuild_unified_kg(nb)
    with repo._write() as db:
        phantom = _object_id(db, nb, "M-PRIVATE Phantom", "src-s")
    phantom_c = repo.cluster_map(nb)[phantom]

    def legacy(name, source_id, evidence):
        oid = repo._test_insert_object(
            nb, "procedure", {"name": name, "section_path": "L"}, source_id=source_id)
        raw = json.dumps(evidence)
        with repo._connect() as db:
            db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?", (raw, oid))
            repo._runtime.knowledge.replace_object_sources(db, oid, nb, raw)
        return oid

    target = legacy("visible step", "src-s", [_ev("src-s", "el-s-occ")])
    legacy("M-PRIVATE sibling", "src-mm", [_ev("src-s", "el-s-occ")])
    legacy("mislabelled", "src-s", [_ev("src-s", "el-mm-occ")])

    for headers, sees in ((env.owner, False), (env.member, True)):
        r = c.get(f"/api/notebooks/{nb}/objects/{env.ids.engram_s}/neighbors",
                  headers=headers)
        assert r.status_code == 200
        assert (phantom_c in r.text) is sees and ("M-PRIVATE" in r.text) is sees, r.text
        r = c.get(f"/api/notebooks/{nb}/concepts/{phantom_c}/detail", headers=headers)
        assert r.status_code == (200 if sees else 404), r.text
        r = c.get(f"/api/notebooks/{nb}/objects/{target}/context", headers=headers)
        assert r.status_code == 200, r.text
        names = sorted(st["name"] for st in r.json()["steps"])
        if sees:
            assert names == ["M-PRIVATE sibling", "mislabelled", "visible step"]
        else:
            assert names == ["visible step"], r.text
            assert "M-PRIVATE" not in r.text
