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


def mounted_library(world: World) -> tuple[str, str]:
    """``(library id, a document source in it)``: an owner's library mounted on
    the world's notebook."""
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
