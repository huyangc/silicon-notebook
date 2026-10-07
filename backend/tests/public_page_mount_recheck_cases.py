"""Every open of a public page re-checks the libraries it draws on (E7-5: D-3
and D-2), defined once.

``test_public_page_mount_recheck.py`` runs these on SQLite and
``postgres/test_public_page_mount_recheck_pg.py`` on PostgreSQL, through the
real HTTP routes with the world of ``report_share_disclosure_cases``: the
owner of a notebook, a member Alice who asks and publishes, and a second
library of the owner mounted on the notebook (a same-owner mount).

* D-3: a notebook conversation or a report quoting the mounted library stops
  being served once the mount is no longer effective for its creator, exactly
  like the global branch losing a library (the indistinguishable 404), and the
  same link revives when the mount comes back.  A page that quotes no other
  library does not depend on the notebook's mounts at all.
* D-2: a global round is re-checked against every library it searched, not
  only the ones it cites.
"""
from __future__ import annotations

from itertools import count
from typing import Callable

from tests.conversation_share_disclosure_cases import (
    answer,
    global_publish,
    publish,
    seed_conversation,
    seed_global,
    source_anchor,
)
from tests.report_share_disclosure_cases import World, make_report, share, source_ref

_KEYS = count(1)


def mounted_library(
    world: World, *, readable_by_alice: bool = True
) -> tuple[str, str]:
    """``(library id, a document source in it)``: an owner's library mounted on
    the world's notebook.

    A mount is effective for the viewer who can read the library themselves, or
    who mounted it (M3), and the anonymous page asks AS the share's creator --
    Alice.  So by default Alice is also a reader of the library, which keeps the
    scenarios below about the mount's lifecycle; ``readable_by_alice=False``
    is the library only the mounter can read."""
    library = world.client.post(
        "/api/notebooks", json={"name": "参考库"}, headers=world.owner.headers
    ).json()["id"]
    source = f"src-mounted-{next(_KEYS)}"
    world.repo._runtime.source_ingestion.sources.insert_source(
        source_id=source, notebook_id=library, title="参考资料",
        source_type="upload", status="active", parse_status="parsed",
        file_name="r.md", file_path="", file_size=1, file_hash="h",
        summary="", doc_type="",
    )
    if readable_by_alice:
        world.repo.add_member(library, world.alice.id)
    mount(world, [library])
    return library, source


def mount(world: World, libraries: list[str]) -> None:
    response = world.client.put(
        f"/api/notebooks/{world.notebook}/bases",
        json={"base_notebook_ids": libraries}, headers=world.owner.headers,
    )
    assert response.status_code == 200, response.text


def case_conversation_page_dies_with_its_mount_and_revives(world: World) -> None:
    library, source = mounted_library(world)
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(source, "k1", notebook_id=library, title="参考资料"),
                source_anchor(world.doc_source, "k2")]),
    ])
    token = publish(world, world.alice, cid, first).json()["share_token"]
    page = f"/api/public/conversations/{token}"
    assert world.client.get(page).status_code == 200
    mount(world, [])
    gone = world.client.get(page)
    assert gone.status_code == 404
    assert gone.json() == world.client.get(
        "/api/public/conversations/cshr-never-issued").json()
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_conversation_image_dies_with_its_mount_and_revives(world: World) -> None:
    """The image endpoint runs the same per-open re-check as the page: an image
    from the mounted library stops being served when the mount goes and
    comes back with it (images are on in this world's deployment)."""
    from app.services.knowhow.assets import AssetService

    library, source = mounted_library(world)
    asset = AssetService(world.repo).save(
        library, "figure.png", "image/png", b"\x89PNG\r\n\x1a\nmounted-figure",
        world.owner.id,
    )["id"]
    anchor = source_anchor(source, "k1", notebook_id=library, title="参考资料")
    anchor["images"] = [{"element_id": anchor["element_id"], "asset_id": asset,
                         "caption": "参考图"}]
    cid, (first,) = seed_conversation(world, world.alice, [answer([anchor])])
    token = publish(world, world.alice, cid, first).json()["share_token"]
    (image,) = world.client.get(f"/api/public/conversations/{token}").json()["turns"][0]["images"]
    url = f"/api/public/conversations/{token}/assets/{image['alias']}"
    assert world.client.get(url).status_code == 200
    mount(world, [])
    assert world.client.get(url).status_code == 404
    mount(world, [library])
    assert world.client.get(url).status_code == 200


def case_conversation_share_is_refused_while_its_mount_is_gone(world: World) -> None:
    """Like the global share's authority sweep: a link that would 404 on its
    first open is not issued, and the disclosure read refuses the same way."""
    library, source = mounted_library(world)
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(source, "k1", notebook_id=library, title="参考资料")]),
    ])
    mount(world, [])
    lost = {"detail": "部分笔记本已无法访问，请重新选择范围。"}
    read = world.client.get(
        f"/api/notebooks/{world.notebook}/conversations/{cid}/share/disclosure",
        params={"through_id": first}, headers=world.alice.headers,
    )
    assert read.status_code == 404 and read.json() == lost
    refused = publish(world, world.alice, cid, first)
    assert refused.status_code == 404 and refused.json() == lost
    assert world.repo.conversation_share_state(world.notebook, cid)["share_token"] == ""
    mount(world, [library])
    assert publish(world, world.alice, cid, first).status_code == 200


def case_a_mount_its_creator_cannot_read_is_not_served(world: World) -> None:
    """M3: the owner mounts a private library on the shared notebook, and Alice
    (a member who asks and publishes) cannot read it herself.  The mount is
    effective for its mounter only, so a page quoting it is not issued, and one
    already issued stops being served the moment Alice stops reading the
    library -- the same indistinguishable 404 -- and revives when she reads it
    again.  The mounter's own reading of the library is untouched."""
    library, source = mounted_library(world, readable_by_alice=False)
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(source, "k1", notebook_id=library, title="参考资料")]),
    ])
    lost = {"detail": "部分笔记本已无法访问，请重新选择范围。"}
    refused = publish(world, world.alice, cid, first)
    assert refused.status_code == 404 and refused.json() == lost
    assert world.repo.participant_notebook_ids(
        world.notebook, viewer_id=world.owner.id
    ) == [world.notebook, library], "the mounter still has the mount"
    world.repo.add_member(library, world.alice.id)
    token = publish(world, world.alice, cid, first).json()["share_token"]
    page = f"/api/public/conversations/{token}"
    assert world.client.get(page).status_code == 200
    world.repo.remove_member(library, world.alice.id)
    assert world.client.get(page).status_code == 404
    world.repo.add_member(library, world.alice.id)
    assert world.client.get(page).status_code == 200


def case_a_report_quoting_a_mount_its_author_cannot_read_is_not_served(
    world: World,
) -> None:
    """M3, the report side of the case above: the owner's private library is
    mounted, Alice cannot read it, and her report quotes it.  Sharing is
    refused (the link would 404 on its first open); once Alice may read the
    library the link is issued and served, it dies the moment she stops
    reading it, and revives when she reads it again."""
    library, source = mounted_library(world, readable_by_alice=False)
    rid = make_report(world, world.alice, [{
        **source_ref(source, "k1"), "from_reference_library": True, "notebook_id": library,
    }])
    refused = share(world, world.alice, rid)
    assert refused.status_code == 404
    assert refused.json() == {"detail": "部分笔记本已无法访问，请重新选择范围。"}
    assert world.repo.report_share_token(world.notebook, rid) == ""
    world.repo.add_member(library, world.alice.id)
    token = share(world, world.alice, rid).json()["share_token"]
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    world.repo.remove_member(library, world.alice.id)
    gone = world.client.get(page)
    assert gone.status_code == 404
    assert gone.json() == {"detail": "shared report not found"}
    world.repo.add_member(library, world.alice.id)
    assert world.client.get(page).status_code == 200


def _knowledge_object(world: World, notebook_id: str) -> str:
    object_id = f"ko-mounted-{next(_KEYS)}"
    sql = world.repo._runtime.global_ask_store._sql
    with world.repo._runtime.database.write() as db:
        db.execute(sql(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, payload, "
            "evidence, source_id, created_at, updated_at) "
            "VALUES (?, ?, 'concept', 'approved', '{}', '[]', '', ?, ?)"
        ), (object_id, notebook_id, "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00"))
    return object_id


def _object_ref(object_id: str, key: str, *, mounted: bool) -> dict:
    """A knowledge-object citation without any source, as the report engine
    stores one whose object had no occurrence."""
    return {
        "key": key, "object_id": object_id, "object_type": "concept",
        "label": "概念", "name": "概念", "source_title": "", "source_id": "",
        "element_id": "", "snippet": "概念定义", "location_label": "",
        "tier": "personal", "from_reference_library": mounted,
    }


def case_report_quoting_a_mounted_object_without_a_source_dies_with_its_mount(
    world: World,
) -> None:
    library, _source = mounted_library(world)
    object_id = _knowledge_object(world, library)
    rid = make_report(world, world.alice, [_object_ref(object_id, "k1", mounted=True)])
    token = share(world, world.alice, rid).json()["share_token"]
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    mount(world, [])
    assert world.client.get(page).status_code == 404
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_report_quoting_a_mounted_library_it_can_no_longer_name_fails_closed(
    world: World,
) -> None:
    """Marked as coming from a mounted library, written before the library was
    stored on it, and its object is gone: nothing can show the library is
    still mounted, so the page is not served -- and no new link is issued."""
    mounted_library(world)
    rid = make_report(world, world.alice, [
        _object_ref("ko-gone", "k1", mounted=True), source_ref(world.doc_source, "k2"),
    ])
    refused = share(world, world.alice, rid)
    assert refused.status_code == 404
    token = world.repo.share_report(world.notebook, rid)        # a link issued earlier
    assert world.client.get(f"/api/public/reports/{token}").status_code == 404


def case_report_of_local_citations_reads_no_ownership(world: World) -> None:
    """Every citation marked local: the per-open re-check reads nothing."""
    rid = make_report(world, world.alice, [
        {**source_ref(world.doc_source, "k1"), "from_reference_library": False},
        _object_ref("ko-local", "k2", mounted=False),
    ])
    token = share(world, world.alice, rid).json()["share_token"]

    def refuse(*_args, **_kwargs):
        raise AssertionError("a report of local citations reads no ownership")

    runtime = world.repo._runtime
    world.monkeypatch.setattr(runtime.source_store, "visible_source_owners", refuse)
    world.monkeypatch.setattr(runtime.knowledge, "object_owners", refuse)
    world.monkeypatch.setattr(world.repo, "participant_notebook_ids", refuse)
    assert world.client.get(f"/api/public/reports/{token}").status_code == 200


def _hidden_source(world: World, notebook_id: str, source_type: str) -> str:
    source_id = f"src-{source_type}-{next(_KEYS)}"
    world.repo._runtime.source_ingestion.sources.insert_source(
        source_id=source_id, notebook_id=notebook_id, title="表格投影",
        source_type=source_type, status="active", parse_status="parsed",
        file_name="", file_path="", file_size=1, file_hash="h",
        summary="", doc_type="",
    )
    return source_id


def _memory_in(world: World, user, notebook_id: str, key: str) -> tuple[str, str]:
    """A confirmed Memory of ``user`` in ``notebook_id`` and its projection source."""
    service = world.repo._runtime.memory_service
    candidate = service.create_candidate(
        notebook_id, user.id, None, f"req-mnt-{key}-{next(_KEYS)}", f"记忆 {key}",
        f"记忆 {key}：参考库里的个人记忆。", [], "reason", {}, [],
    )
    memory = service.confirm(candidate.id, user.id)
    source_id = world.repo._runtime.source_ingestion.ingest_memory_source(
        notebook_id, memory.id, memory.title, memory.content_md
    )
    assert source_id
    return memory.id, source_id


def _mounted_ref(source_id: str, key: str) -> dict:
    """A citation of a mounted library's source written before the library was
    stored on it (only the mark), so its library is read by source."""
    return {**source_ref(source_id, key), "object_id": f"chunk-{key}",
            "object_type": "chunk", "from_reference_library": True}


def case_report_quoting_a_mounted_knowhow_projection_follows_its_mount(world: World) -> None:
    """The library of a hidden projection source is read like any other
    source's: a live mount serves the page, unmounting kills it, remounting
    revives it."""
    library, _source = mounted_library(world)
    knowhow = _hidden_source(world, library, "knowhow")
    rid = make_report(world, world.alice, [_mounted_ref(knowhow, "k1")])
    token = share(world, world.alice, rid).json()["share_token"]
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    mount(world, [])
    assert world.client.get(page).status_code == 404
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_report_quoting_the_authors_memory_in_a_mounted_library_follows_its_mount(
    world: World,
) -> None:
    from tests.report_share_disclosure_cases import recorded

    library, _source = mounted_library(world)
    world.repo.add_member(library, world.alice.id)
    memory_id, memory_source = _memory_in(world, world.alice, library, "am")
    rid = make_report(world, world.alice, [
        recorded(_mounted_ref(memory_source, "k1"), memory_id, world.alice),
    ])
    published = share(world, world.alice, rid, 1)
    assert published.status_code == 200, published.text
    page = f"/api/public/reports/{published.json()['share_token']}"
    served = world.client.get(page)
    assert served.status_code == 200 and served.json()["references"][0]["is_memory"] is True
    mount(world, [])
    assert world.client.get(page).status_code == 404


def case_another_members_memory_in_a_mounted_library_is_never_served(world: World) -> None:
    """Ownership is not readability: the mount is live and the library is
    named, but the cited Memory is another member's, so the page stays 404."""
    from tests.report_share_disclosure_cases import recorded

    library, _source = mounted_library(world)
    memory_id, memory_source = _memory_in(world, world.owner, library, "om")
    rid = make_report(world, world.alice, [
        recorded(_mounted_ref(memory_source, "k1"), memory_id, world.owner),
    ])
    token = world.repo.share_report(world.notebook, rid)
    assert world.client.get(f"/api/public/reports/{token}").status_code == 404
    mount(world, [library])
    assert world.client.get(f"/api/public/reports/{token}").status_code == 404


def case_report_naming_its_library_survives_a_deleted_source(world: World) -> None:
    """A report generated since the library is stored on its citations: the
    cited source is deleted from the mounted library, the mount is intact, the
    page is served; unmounting kills it, remounting revives it."""
    library, source = mounted_library(world)
    rid = make_report(world, world.alice, [{
        **source_ref(source, "k1"), "from_reference_library": True, "notebook_id": library,
    }])
    token = share(world, world.alice, rid).json()["share_token"]
    sql = world.repo._runtime.global_ask_store._sql
    with world.repo._runtime.database.write() as db:
        db.execute(sql("DELETE FROM sources WHERE id=?"), (source,))
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    mount(world, [])
    assert world.client.get(page).status_code == 404
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_report_share_is_refused_while_its_mount_is_gone(world: World) -> None:
    """Like the conversation share: a report quoting a mounted library that is
    no longer effective gets no link (it would 404 on its first open)."""
    library, source = mounted_library(world)
    rid = make_report(world, world.alice, [{
        **source_ref(source, "k1"), "from_reference_library": True, "notebook_id": library,
    }])
    mount(world, [])
    refused = share(world, world.alice, rid)
    assert refused.status_code == 404
    assert refused.json() == {"detail": "部分笔记本已无法访问，请重新选择范围。"}
    assert world.repo.report_share_token(world.notebook, rid) == ""
    mount(world, [library])
    assert share(world, world.alice, rid).status_code == 200


def case_generated_report_marks_a_sourceless_mounted_object_by_its_context(
    world: World,
) -> None:
    """Through the real engine (fake models): the section cites a knowledge
    object of a mounted personal library that has no occurrence, so no source
    names its library; the evidence context does.  The stored citation is
    marked as coming from the mounted library and names it, and the public
    page follows the mount: 200, unmounted 404, remounted 200."""
    import re

    from app.domain.retrieval import RetrievedKnowledge
    from app.services.reasoning_retrieval import ReasoningResult
    from tests.report_share_disclosure_cases import (
        _Models,
        _engine,
        _generate,
        _new_report,
        _outline_ready,
        _serve_memory,
    )

    library, _source = mounted_library(world)
    object_id = _knowledge_object(world, library)
    hit = RetrievedKnowledge(
        object_id=object_id, object_type="concept", payload={"name": "参考库概念"},
        score=1.0, relevance=1.0, notebook_id=library, tier="personal",
    )

    class _CitingModels(_Models):
        """Cites whichever key the section prompt gave the mounted object."""

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            if "ONLY this section" in content:
                found = re.search(r"(k\d+): \[concept\]\[[a-z]+\] 参考库概念", content)
                assert found, "the mounted object reaches the section prompt"
                self.section_markdown = f"## 结论\n参考库概念成立 [{found.group(1)}]。"
            return super().chat_json(messages, schema_hint, **kwargs)

    engine = _engine(world, world.alice, _CitingModels(),
                     deep_dive=lambda *a, **k: ReasoningResult(top_hits=[hit]))
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    _serve_memory(world, [])
    stored = _generate(world, engine, rid)
    (reference,) = [ref for ref in stored["references"] if ref["object_id"] == object_id]
    assert reference["source_id"] == ""
    assert reference["from_reference_library"] is True
    assert reference["notebook_id"] == library

    token = share(world, world.alice, rid).json()["share_token"]
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    assert library not in world.client.get(page).text
    mount(world, [])
    assert world.client.get(page).status_code == 404
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_report_page_dies_with_its_mount_and_revives(world: World) -> None:
    library, source = mounted_library(world)
    rid = make_report(world, world.alice, [
        source_ref(source, "k1"), source_ref(world.doc_source, "k2"),
    ])
    token = share(world, world.alice, rid).json()["share_token"]
    page = f"/api/public/reports/{token}"
    assert world.client.get(page).status_code == 200
    mount(world, [])
    gone = world.client.get(page)
    assert gone.status_code == 404
    assert gone.json() == {"detail": "shared report not found"}
    mount(world, [library])
    assert world.client.get(page).status_code == 200


def case_pages_quoting_no_other_library_ignore_the_mounts(world: World) -> None:
    library, _source = mounted_library(world)
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(world.doc_source, "k1")]),
    ])
    conversation = publish(world, world.alice, cid, first).json()["share_token"]
    rid = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    report = share(world, world.alice, rid).json()["share_token"]
    mount(world, [])
    assert world.client.get(f"/api/public/conversations/{conversation}").status_code == 200
    assert world.client.get(f"/api/public/reports/{report}").status_code == 200
    assert library


def case_global_round_is_re_checked_against_every_searched_library(world: World) -> None:
    """D-2: the round cites the world's notebook and paraphrases a second
    library it searched; Alice losing that second library kills the link."""
    searched = world.client.post(
        "/api/notebooks", json={"name": "被转述库"}, headers=world.owner.headers
    ).json()["id"]
    world.repo.add_member(searched, world.alice.id)
    paraphrase = answer([source_anchor(world.doc_source, "k1", notebook_id=world.notebook)])
    paraphrase["answer"] += " 另一库的设计说明也给出同一结论。"
    cid, (first,) = seed_global(world, world.alice, [paraphrase],
                                resolved=[world.notebook, searched])
    token = global_publish(world, world.alice, cid, first).json()["share_token"]
    page = f"/api/public/conversations/{token}"
    assert world.client.get(page).status_code == 200
    world.repo.remove_member(searched, world.alice.id)
    assert world.client.get(page).status_code == 404
    world.repo.add_member(searched, world.alice.id)
    assert world.client.get(page).status_code == 200


CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("case_") and callable(case)
}
