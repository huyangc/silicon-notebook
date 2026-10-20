"""The findings list's read side and its one write (design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §3.4 /
§3.5 / §4.5 / §8.4): the overview's ``findings_summary`` / ``merge_code``, the
``/kg-analysis/findings`` page with its derived statuses, and "标为正常"
(service and routes). SQLite; the PostgreSQL twin of the page is
``tests/postgres/test_kg_findings_publish_pg.py``."""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from app.core.config import Settings
from app.domain.kg_analysis_contracts import ARTIFACT_FINDINGS, LEDGER_CONTRACT_KEY
from app.domain.kg_merge_seed import concept_seed_v1
from app.services.embedding import FakeEmbedder
from app.services.kg_findings_view import (
    HANDLED_MERGE_CONFIRMED,
    HANDLED_SOURCE_DELETED,
    HANDLED_SOURCE_PROCESSING,
    HANDLED_SOURCE_REPROCESSED,
    STATUS_DECIDED_NOT_SAME,
    STATUS_DISMISSED,
    STATUS_HANDLED,
    STATUS_OPEN,
    STATUS_RELINK_SPENT,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.kg_findings_world import SECRETS, seed_world
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


@pytest.fixture
def built(repo):
    world = seed_world(repo, postgres=False)
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    return world.notebook_id


def _service(repo):
    return repo._runtime.kg_analysis


def _page(repo, notebook_id, zone="open", **kwargs):
    return _service(repo).findings_page(notebook_id, zone=zone, **kwargs)


def _status(page) -> dict:
    return {(r["rule_id"], r["subject"]["key"]): (r["status"], r["handled_reason"])
            for r in page.rows}


def _sql(repo, statement, params=()):
    with repo._runtime.database.write() as db:
        db.execute(statement, params)


OPEN_KEYS = [  # display order: severity, then impact, then key
    ("F1", "src-off"), ("F2", "src-dup-a|src-dup-b"), ("F3", "src-broken"),
    ("F5", "notebook"), ("F4", "llama31"),
]


# ------------------------------------------------------------------- page
def test_the_open_zone_lists_every_finding_with_live_sources(repo, built):
    page = _page(repo, built)
    assert page.present is True and page.absence is None
    assert page.total == 5 and page.returned == 5 and page.has_more is False
    assert [(r["rule_id"], r["subject"]["key"]) for r in page.rows] == OPEN_KEYS
    assert {r["status"] for r in page.rows} == {STATUS_OPEN}
    by_rule = {r["rule_id"]: r for r in page.rows}
    assert by_rule["F2"]["live_sources"] == [
        {"source_id": "src-dup-a", "exists": True, "title": "Title of src-dup-a",
         "source_type": "markdown", "processing": False},
        {"source_id": "src-dup-b", "exists": True, "title": "Title of src-dup-b",
         "source_type": "markdown", "processing": False},
    ]
    assert by_rule["F3"]["live_sources"][0]["source_id"] == "src-broken"
    assert [a["kind"] for a in by_rule["F2"]["actions"]] == ["delete_source"]
    assert by_rule["F2"]["actions"][0]["source_id"] == "src-dup-b"    # not the kept copy
    assert by_rule["F4"]["evidence"]["canonical_ids"] == ["K-llama 3 1", "K-llama 31"]
    assert by_rule["F1"]["impact"] == {"objects": 12}
    assert page.built_at["kg_seq"] == by_rule["F1"]["first_seen"]["kg_seq"]
    assert page.units["impact_objects"] == "objects"
    assert set(page.evidence_units) == {"F1", "F2", "F3", "F4", "F5"}


def test_knowhow_named_canonical_never_in_payload(repo, built):
    """Design §8.4: a canonical NAME may come from a knowhow object; neither the
    page, nor the summary, nor any row may carry it — only visible members'
    own names are shown."""
    overview = _service(repo).overview(built)
    pages = [_page(repo, built, zone) for zone in ("open", "passed", "dismissed")]
    blob = json.dumps(
        [overview.findings_summary.__dict__, [p.rows for p in pages]],
        ensure_ascii=False, default=str)
    assert [secret for secret in SECRETS if secret in blob] == []


def test_no_hidden_member_text_is_served_in_any_case(repo, built):
    """codex #941 R3 (P1): F4 stores the v1 seeds of EVERY member — hidden
    Knowhow / Memory ones included — to match stored decisions. Those seeds are
    readable names past the reader-visible boundary: the page projects them
    out, and no hidden spelling or hidden-only seed reaches any served byte,
    whatever its case (the whole overview and every zone's page)."""
    from tests.kg_findings_world import hidden_text_served

    assert hidden_text_served(repo, built) == []
    f4 = next(r for r in _page(repo, built).rows if r["rule_id"] == "F4")
    assert "member_seeds" not in f4["evidence"]
    assert "seeds_truncated" not in f4["evidence"]
    # what the panel needs is still there: the group's ids and visible names
    assert f4["evidence"]["canonical_ids"] == ["K-llama 3 1", "K-llama 31"]
    assert [m["name"] for m in f4["evidence"]["members"]] == ["Llama 3.1", "Llama 31"]


def test_a_dismissed_or_decided_f4_is_projected_the_same_way(repo, built):
    """The projection is per row, whatever the zone: an F4 the user decided
    apart (by a decision keyed by the HIDDEN member's seed — still matched on
    the stored evidence) and one marked normal carry no member seeds either."""
    from tests.kg_findings_world import KNOWHOW_HIDDEN_SEED_NAME, hidden_text_served

    _sql(repo,
         "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
         "score,status,seed_a,seed_b,created_at,updated_at) "
         "VALUES ('mc-h',?,'K-llama 3 1','K-llama 31',0.9,'rejected',?,'llama 31',"
         "'2026-10-19','2026-10-19')",
         (built, concept_seed_v1(KNOWHOW_HIDDEN_SEED_NAME)))
    passed = _page(repo, built, "passed")
    assert _status(passed)[("F4", "llama31")] == (STATUS_DECIDED_NOT_SAME, None)
    assert hidden_text_served(repo, built) == []
    _sql(repo, "DELETE FROM concept_merge_candidates WHERE id='mc-h'")
    assert _service(repo).dismiss_finding(built, "F4", "llama31", actor_user_id="u")
    dismissed = _page(repo, built, "dismissed")
    assert [r["rule_id"] for r in dismissed.rows] == ["F4"]
    assert "member_seeds" not in dismissed.rows[0]["evidence"]
    assert hidden_text_served(repo, built) == []


def test_pagination_is_stable_within_a_zone(repo, built):
    first = _page(repo, built, limit=2, offset=0)
    second = _page(repo, built, limit=2, offset=2)
    third = _page(repo, built, limit=2, offset=4)
    keys = [(r["rule_id"], r["subject"]["key"])
            for p in (first, second, third) for r in p.rows]
    assert keys == OPEN_KEYS
    assert (first.has_more, second.has_more, third.has_more) == (True, True, False)
    assert first.total == second.total == third.total == 5


def test_delete_source_marks_the_finding_handled_not_gone(repo, built):
    """Deleting the off-topic source in the source list (not via the panel) is
    recognised on the next page read; the row stays in the open zone as
    "handled, verify after the update" until a rebuild drops it."""
    _sql(repo, "DELETE FROM sources WHERE id='src-off'")
    page = _page(repo, built)
    assert _status(page)[("F1", "src-off")] == (STATUS_HANDLED, HANDLED_SOURCE_DELETED)
    row = next(r for r in page.rows if r["rule_id"] == "F1")
    assert row["live_sources"] == [{"source_id": "src-off", "exists": False, "title": "",
                                    "source_type": "", "processing": False}]
    # F2: one copy gone is handled too
    _sql(repo, "DELETE FROM sources WHERE id='src-dup-b'")
    assert _status(_page(repo, built))[("F2", "src-dup-a|src-dup-b")] == (
        STATUS_HANDLED, HANDLED_SOURCE_DELETED)


def test_reparse_after_the_report_and_processing_count_as_handled(repo, built):
    _sql(repo, "UPDATE sources SET status='parsing' WHERE id='src-broken'")
    assert _status(_page(repo, built))[("F3", "src-broken")] == (
        STATUS_HANDLED, HANDLED_SOURCE_PROCESSING)
    _sql(repo, "UPDATE sources SET status='extracted', chunked_at='2099-01-01T00:00:00+00:00' "
               "WHERE id='src-broken'")
    assert _status(_page(repo, built))[("F3", "src-broken")] == (
        STATUS_HANDLED, HANDLED_SOURCE_REPROCESSED)
    # renaming moves updated_at only: not a reparse
    _sql(repo, "UPDATE sources SET chunked_at=NULL, title='renamed', "
               "updated_at='2099-01-01T00:00:00+00:00' WHERE id='src-broken'")
    assert _status(_page(repo, built))[("F3", "src-broken")] == (STATUS_OPEN, None)


def _decide(repo, notebook_id, status, left="Llama 3.1", right="Llama 31"):
    _sql(repo,
         "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
         "score,status,seed_a,seed_b,created_at,updated_at) VALUES (?,?,?,?,0.9,?,?,?,?,?)",
         (f"mc-{status}", notebook_id, "K-" + concept_seed_v1(left),
          "K-" + concept_seed_v1(right), status, concept_seed_v1(left),
          concept_seed_v1(right), "2026-10-19", "2026-10-19"))


def test_reject_merge_moves_f4_to_passed(repo, built):
    """Rejecting does not set dirty, so waiting for the next build to drop the
    group would leave it open forever: it moves to "passed" at once (§3.5)."""
    _decide(repo, built, "rejected")
    assert ("F4", "llama31") not in _status(_page(repo, built))
    passed = _page(repo, built, "passed")
    assert _status(passed) == {("F4", "llama31"): (STATUS_DECIDED_NOT_SAME, None)}


def test_a_decision_with_drifted_canonical_ids_still_moves_f4_to_passed(repo, built):
    """PR-2 review: decisions are matched by SEED on the read side too — a
    rejection whose two canonical ids both drifted (the clusters' minimum
    members changed) still says "not the same thing"; so does one keyed by a
    hidden member's seed (stored in the evidence at publication)."""
    _sql(repo,
         "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
         "score,status,seed_a,seed_b,created_at,updated_at) "
         "VALUES ('mc-drift',?,'K-gone-a','K-gone-b',0.9,'rejected',?,?,?,?)",
         (built, concept_seed_v1("Llama three-one"), concept_seed_v1("Llama 31"),
          "2026-10-19", "2026-10-19"))
    assert _status(_page(repo, built, "passed")) == {
        ("F4", "llama31"): (STATUS_DECIDED_NOT_SAME, None)}


def test_a_reparse_finished_during_the_precompute_counts_as_handled(repo, built):
    """"Reparsed after the report" is judged against the moment the scan started
    (``scanned_at``), not the publication: a reparse that finished inside the
    precompute window may not be in the report at all."""
    payload_scanned = _service(repo).findings_page(built).built_at   # sanity: present
    assert payload_scanned is not None
    _sql(repo, "UPDATE kg_analysis_artifacts SET created_at='2099-01-02T00:00:00+00:00' "
               "WHERE notebook_id=? AND kind='findings'", (built,))
    _sql(repo, "UPDATE sources SET chunked_at='2099-01-01T00:00:00+00:00' "
               "WHERE id='src-broken'")
    assert _status(_page(repo, built))[("F3", "src-broken")] == (
        STATUS_HANDLED, HANDLED_SOURCE_REPROCESSED)


def test_confirm_merge_marks_f4_handled(repo, built):
    _decide(repo, built, "confirmed")
    assert _status(_page(repo, built))[("F4", "llama31")] == (
        STATUS_HANDLED, HANDLED_MERGE_CONFIRMED)


def test_after_relink_may_change_is_false_and_f5_leaves_the_open_zone(repo, built):
    """A relink that found nothing to add leaves the graph as it was: the mark
    sits at the current generation, ``may_change`` is false, F5 moves out of
    the open zone (no button), and comes back once new content arrives."""
    repo._runtime.knowledge_lifecycle.relink_notebook_kg(built)
    summary = _service(repo).overview(built).findings_summary
    assert summary.relink["attempted"] is True
    assert summary.relink["may_change"] is False
    assert ("F5", "notebook") not in _status(_page(repo, built))
    assert _status(_page(repo, built, "passed")) == {
        ("F5", "notebook"): (STATUS_RELINK_SPENT, None)}
    _sql(repo, "UPDATE unified_kg_state SET kg_mutation_seq = kg_mutation_seq + 1 "
               "WHERE notebook_id=?", (built,))
    assert _service(repo).overview(built).findings_summary.relink["may_change"] is True
    assert _status(_page(repo, built))[("F5", "notebook")] == (STATUS_OPEN, None)


def test_dismissal_moves_a_row_to_the_dismissed_zone_and_back(repo, built):
    service = _service(repo)
    assert service.dismiss_finding(built, "F1", "src-off", actor_user_id="u-1") is True
    dismissed = _page(repo, built, "dismissed")
    assert _status(dismissed) == {("F1", "src-off"): (STATUS_DISMISSED, None)}
    assert dismissed.rows[0]["dismissed_at"]
    assert ("F1", "src-off") not in _status(_page(repo, built))
    assert service.overview(built).findings_summary.counts["dismissed"] == 1
    service.undismiss_finding(built, "F1", "src-off")
    service.undismiss_finding(built, "F1", "src-off")          # idempotent
    assert _page(repo, built, "dismissed").total == 0
    assert ("F1", "src-off") in _status(_page(repo, built))


def test_only_a_current_finding_can_be_dismissed(repo, built):
    service = _service(repo)
    assert service.dismiss_finding(built, "F1", "src-core-0", actor_user_id="u") is False
    assert service.dismiss_finding(built, "S1", "qformer", actor_user_id="u") is False
    assert service.dismiss_finding(built, "F9", "src-off", actor_user_id="u") is False
    assert _page(repo, built, "dismissed").total == 0


def test_the_page_reads_one_snapshot(repo, built, monkeypatch):
    """Every read of one page rides ONE ``read_snapshot()`` connection: a
    concurrent publish cannot give half a page from each build."""
    store = repo._runtime.unified_kg
    governance = repo._runtime.governance
    original = store.read_snapshot
    snapshots: list = []
    seen: dict = {}

    from contextlib import contextmanager

    @contextmanager
    def counting():
        with original() as db:
            snapshots.append(db)
            yield db

    def spy(target, name):
        inner = getattr(target, name)

        def wrapper(db, *args, **kwargs):
            seen.setdefault(name, set()).add(id(db))
            return inner(db, *args, **kwargs)

        monkeypatch.setattr(target, name, wrapper)

    monkeypatch.setattr(store, "read_snapshot", counting)
    for name in ("state_row", "kg_analysis_artifact_rows", "kg_analysis_finding_rows",
                 "kg_analysis_mark_rows", "kg_finding_member_name_rows",
                 "kg_finding_source_states"):
        spy(store, name)
    spy(governance, "merge_candidate_states_for_seeds")
    _page(repo, built)
    assert len(snapshots) == 1
    assert set(seen) == {"state_row", "kg_analysis_artifact_rows", "kg_analysis_finding_rows",
                         "kg_analysis_mark_rows", "kg_finding_source_states",
                         "merge_candidate_states_for_seeds"}
    assert {id(snapshots[0])} == set().union(*seen.values())


def test_the_page_never_reads_member_objects(repo, built, monkeypatch):
    """PR-2 review: F4's decisions are matched by the member seeds stored in the
    evidence at publication — the page does not read member objects at all (that
    read grew with group sizes, independent of zone and page), and with no stored
    decision at all the decision read is still bounded by the seed list."""
    store = repo._runtime.unified_kg
    called: list = []
    monkeypatch.setattr(store, "kg_finding_member_name_rows",
                        lambda *args, **kwargs: called.append(args) or [])
    for zone in ("open", "passed", "dismissed"):
        _page(repo, built, zone)
    assert called == []


# ---------------------------------------------------------------- summary
def test_the_overview_summary_reads_the_ledger(repo, built):
    summary = _service(repo).overview(built).findings_summary
    assert summary.present is True and summary.absence is None
    assert summary.freshness.stale is False
    assert summary.counts == {"by_rule": {"F1": 1, "F2": 1, "F3": 1, "F4": 1, "F5": 1},
                              "dismissed": 0}
    # The world's merge is published in seed space 1 (the state row's default)
    # while the code builds in 2: the fix is deployed, "更新分析" applies it.
    assert summary.dev_items == [{"item_id": "S1", "status": "fixed_pending_update",
                                  "groups": 1, "objects": 2,
                                  "samples": [{"names": ["Q-Former", "Qformer"]}]}]


def test_the_s1_dev_item_reads_open_once_the_library_publishes_the_fixed_space(
    repo, built,
):
    """``dev_items[].status`` is judged off ``unified_kg_state`` at read time:
    a library already publishing seed space 2 that still shows the narrow half
    reads ``open`` (the fix did not cover it), not "fixed, update to apply"."""
    with repo._write() as db:
        db.execute("UPDATE unified_kg_state SET concept_seed_version=2 "
                   "WHERE notebook_id=?", (built,))
    summary = _service(repo).overview(built).findings_summary
    assert [item["status"] for item in summary.dev_items] == ["open"]
    assert "examples" not in summary.rules["S1"]
    assert summary.structure["island_components"] == 1
    assert summary.comparison["reason"] == "no_previous"
    assert summary.relink == {"attempted": False, "at_kg_seq": None, "at": None,
                              "may_change": True}
    assert summary.units["persisted"] == "findings"


def test_an_upgraded_library_reads_upgrade_pending_not_red(repo, built):
    _sql(repo, "DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
         (built, ARTIFACT_FINDINGS))
    with repo._runtime.database.write() as db:
        for row in db.execute("SELECT kind, payload FROM kg_analysis_artifacts "
                              "WHERE notebook_id=?", (built,)).fetchall():
            payload = json.loads(row["payload"])
            payload.pop(LEDGER_CONTRACT_KEY)
            db.execute("UPDATE kg_analysis_artifacts SET payload=? WHERE notebook_id=? "
                       "AND kind=?", (json.dumps(payload), built, row["kind"]))
    overview = _service(repo).overview(built)
    assert overview.findings_summary.present is False
    assert overview.findings_summary.absence == "upgrade_pending"
    page = _page(repo, built)
    assert (page.present, page.absence, page.total, page.rows) == (
        False, "upgrade_pending", 0, [])


def test_merge_code_compares_the_stored_code_with_the_running_code(repo, built):
    from app.services.kg_merge import cluster_code_version

    current = cluster_code_version(repo.settings)
    _sql(repo, "UPDATE unified_kg_state SET cluster_code_version='' WHERE notebook_id=?",
         (built,))
    code = _service(repo).overview(built).merge_code
    assert (code.stored, code.current, code.matches) == ("", current, None)
    _sql(repo, "UPDATE unified_kg_state SET cluster_code_version=? WHERE notebook_id=?",
         (current, built))
    assert _service(repo).overview(built).merge_code.matches is True
    _sql(repo, "UPDATE unified_kg_state SET cluster_code_version='algo=1;embed_dim=1;"
               "runtime_dim=0' WHERE notebook_id=?", (built,))
    assert _service(repo).overview(built).merge_code.matches is False


# ----------------------------------------------------------------- routes
def _client(tmp_path, monkeypatch):
    from tests.test_notebook_capability_guard import _client as client

    return client(tmp_path, monkeypatch)


def _login(client, username):
    from tests.test_notebook_capability_guard import _login as login

    return login(client, username)


def _route_world(tmp_path, monkeypatch, letter):
    from app.api import deps

    client = _client(tmp_path, monkeypatch)
    owner = _login(client, f"{letter}00000001")
    notebook_id = client.post("/api/notebooks", json={"name": "F"}, headers=owner).json()["id"]
    member = _login(client, f"{letter}00000002")
    member_id = client.get("/api/me", headers=member).json()["id"]
    repository = deps.repository()
    repository._runtime.sharing.add_member(notebook_id, member_id)
    seed_world(repository, postgres=False, notebook_id=notebook_id)
    repository._runtime.knowledge_lifecycle.rebuild_communities(notebook_id)
    return client, notebook_id, owner, member, repository


def _dismissal(notebook_id, rule_id, key):
    return (f"/api/notebooks/{notebook_id}/kg-analysis/findings/"
            f"{quote(rule_id, safe='')}/{quote(key, safe='')}/dismissal")


def test_the_findings_routes(tmp_path, monkeypatch):
    client, nb, owner, member, _repository = _route_world(tmp_path, monkeypatch, "f")
    # read: a read-only member sees the page; the overview carries the summary
    page = client.get(f"/api/notebooks/{nb}/kg-analysis/findings?zone=open&limit=2",
                      headers=member)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 5 and page.json()["returned"] == 2
    overview = client.get(f"/api/notebooks/{nb}/kg-analysis", headers=member).json()
    assert overview["findings_summary"]["present"] is True
    assert set(overview["merge_code"]) == {"stored", "current", "matches"}
    assert client.get(f"/api/notebooks/{nb}/kg-analysis/findings?limit=201",
                      headers=owner).status_code == 422
    assert client.get(f"/api/notebooks/{nb}/kg-analysis/findings?zone=other",
                      headers=owner).status_code == 422
    # write: the key with "|" round-trips URL-encoded; a read-only member gets 404
    path = _dismissal(nb, "F2", "src-dup-a|src-dup-b")
    assert client.put(path, headers=member).status_code == 404
    assert client.put(path, headers=owner).status_code == 204
    dismissed = client.get(f"/api/notebooks/{nb}/kg-analysis/findings?zone=dismissed",
                           headers=owner).json()
    assert [r["subject"]["key"] for r in dismissed["rows"]] == ["src-dup-a|src-dup-b"]
    # a key that is not in the current list is 404; DELETE is idempotent
    assert client.put(_dismissal(nb, "F1", "src-core-0"), headers=owner).status_code == 404
    assert client.delete(path, headers=owner).status_code == 204
    assert client.delete(path, headers=owner).status_code == 204
    assert client.get(f"/api/notebooks/{nb}/kg-analysis/findings?zone=dismissed",
                      headers=owner).json()["total"] == 0


def test_a_key_with_a_slash_reaches_the_service(tmp_path, monkeypatch):
    """F4 keys are squash keys and may contain "/" (``{subject_key:path}``)."""
    client, nb, owner, _member, repository = _route_world(tmp_path, monkeypatch, "g")
    seen = []
    service = repository._runtime.kg_analysis
    monkeypatch.setattr(service, "dismiss_finding",
                        lambda *args, **kwargs: seen.append(args) or True)
    assert client.put(_dismissal(nb, "F4", "tcp/ip+v4"), headers=owner).status_code == 204
    assert seen == [(nb, "F4", "tcp/ip+v4")]


def test_the_dismissal_routes_are_guarded_like_merge_confirmation():
    """"标为正常" is a graph write, at the level of confirming a merge
    (``kg:write``, mirror-fenced); the page is a read at the overview's level."""
    from app.api import kg_routes
    from app.api.deps import require_notebook_capability
    from app.domain.kg_findings_contracts import KG_FINDINGS_PAGE_MAX

    dismissal = "/notebooks/{notebook_id}/kg-analysis/findings/{rule_id}/{subject_key:path}/dismissal"
    methods = {}
    for route in kg_routes.router.routes:
        if getattr(route, "path", None) == dismissal:
            for method in route.methods:
                methods[method] = {d.dependency for d in route.dependencies}
    assert set(methods) == {"PUT", "DELETE"}
    for dependencies in methods.values():
        assert require_notebook_capability("kg:write") in dependencies
    assert KG_FINDINGS_PAGE_MAX == 200


def test_a_full_mark_table_refuses_a_new_dismissal_with_409(tmp_path, monkeypatch):
    """Dismissals outlive the detail, so the mark table can fill; a NEW
    dismissal is then refused with 409 and a message the panel can show — not a
    500 — and no existing mark is touched."""
    from app.repositories.sqlite import unified_kg_store as store_module

    client, nb, owner, _member, repository = _route_world(tmp_path, monkeypatch, "j")
    monkeypatch.setattr(store_module, "KG_ANALYSIS_MARKS_MAX", 1)
    assert client.put(_dismissal(nb, "F1", "src-off"), headers=owner).status_code == 204
    response = client.put(_dismissal(nb, "F3", "src-broken"), headers=owner)
    assert response.status_code == 409, response.text
    assert "撤销" in response.json()["detail"]
    with repository._runtime.unified_kg.read_snapshot() as db:
        marks = repository._runtime.unified_kg.kg_analysis_mark_rows(db, nb)
    assert [(m["rule_id"], m["subject_key"]) for m in marks] == [("F1", "src-off")]
    # overwriting an existing dismissal is not a new row: still fine
    assert client.put(_dismissal(nb, "F1", "src-off"), headers=owner).status_code == 204


def test_dismissal_on_a_mirror_is_409(tmp_path, monkeypatch):
    from tests.test_notebook_capability_guard import _mark_mirror

    client, nb, owner, _member, _repository = _route_world(tmp_path, monkeypatch, "h")
    _mark_mirror(nb)
    response = client.put(_dismissal(nb, "F1", "src-off"), headers=owner)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "notebook_mirrored"
    # reads stay open on a mirror
    assert client.get(f"/api/notebooks/{nb}/kg-analysis/findings",
                      headers=owner).status_code == 200
