"""E3-1: every source/element read honours the Memory owner (audit C-3, D-5).

A Memory projection source (``sources.source_type='memory'``) belongs to the
member who created the memory (``memory_items.created_by``), not to the
notebook. Before this change any reader of the notebook -- the notebook owner
included -- could open another member's Memory projection through the plain
source endpoints, the participant-scope proxy, or MCP ``get_cited_element``,
and read its full text element by element (the element ids are predictable:
``el-<source_id>-0001``). The command catalog accepted it too, echoing its
title back.

The matrix below drives the REAL routes (SQLite, the shared ``mcp_env``: alice
owns the notebook, bob is a member):

* another member's Memory source, an ownerless (orphaned) one and a missing id
  are the same 404 on all six HTTP read endpoints and the same MCP error --
  same status, same body, same headers, and (pinned on the helpers) the same
  SQL statements, so nothing tells "exists but not yours" from "does not exist";
* the creator reads their own Memory; Knowhow projections and ordinary
  documents stay readable to every reader (the fix must not over-reach);
* an Agent token without ``memory:read`` cannot read even its owner's Memory;
* the command catalog's seven endpoints refuse Memory and Knowhow sources with
  the missing-id 404, while an ordinary document still reaches the endpoint.

The store predicates and their plans are pinned on PostgreSQL by
``tests/postgres/test_memory_source_reads_pg.py``.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.api import source_routes
from app.api.deps import notebook_access_repository, repository
from tests.test_memory_mcp import OfficialMcpClient, _payload, mcp_env  # noqa: F401

_NOW = "2026-09-30T00:00:00+00:00"
MISSING = "src-e31-missing"

# (id, source_type, memory creator key or None, element text)
_SOURCES = (
    ("src-e31-mem-alice", "memory", "alice", "alice 的私人记忆"),
    ("src-e31-mem-bob", "memory", "bob", "bob 的私人记忆"),
    ("src-e31-mem-orphan", "memory", None, "没有创建者的记忆"),
    ("src-e31-knowhow", "knowhow", None, "共享表格的一格"),
    ("src-e31-doc", "document", None, "普通文档正文"),
)
_TEXT = {source_id: text for source_id, _type, _owner, text in _SOURCES}


def _element_id(source_id: str) -> str:
    # The id Memory projection really writes (source_ingestion.ingest_memory_source).
    return f"el-{source_id}-0001"


@pytest.fixture
def seeded(mcp_env):  # noqa: F811 - pytest fixture injection
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    users = {"alice": mcp_env["alice"], "bob": mcp_env["bob"]}
    with repo._write() as db:
        for source_id, source_type, owner, text in _SOURCES:
            memory_id = None
            if owner is not None:
                memory_id = f"mem-{source_id}"
                db.execute(
                    "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
                    "title,content_md,created_at,updated_at) "
                    "VALUES (?,?,?,'ask_answer','confirmed',?,?,?,?)",
                    (memory_id, notebook_id, users[owner].id, "私人", text, _NOW, _NOW),
                )
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,"
                "parse_status,memory_id,created_at,updated_at) "
                "VALUES (?,?,?,?,'ready','parsed',?,?,?)",
                (source_id, notebook_id, f"{source_id} 标题", source_type,
                 memory_id, _NOW, _NOW),
            )
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,'paragraph','第 1 段',?,'{}',?)",
                (_element_id(source_id), source_id, text, _NOW),
            )
    headers = {
        name: {"Authorization": f"Bearer {repo.create_session(user.id)}"}
        for name, user in users.items()
    }
    return {**mcp_env, "notebook_id": notebook_id, "headers": headers}


def _read_urls(notebook_id: str, source_id: str) -> dict[str, str]:
    return {
        "source": f"/api/sources/{source_id}",
        "elements": f"/api/sources/{source_id}/elements",
        "elements-page": f"/api/sources/{source_id}/elements-page",
        "scoped-source": f"/api/notebooks/{notebook_id}/sources/{source_id}",
        "scoped-elements": f"/api/notebooks/{notebook_id}/sources/{source_id}/elements",
        "scoped-elements-page": (
            f"/api/notebooks/{notebook_id}/sources/{source_id}/elements-page"
        ),
    }


_VOLATILE_HEADERS = {"date", "x-request-id", "server-timing"}


def _shape(response) -> tuple:
    """Everything a client can observe about a response, minus the clock."""
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _VOLATILE_HEADERS
    }
    return response.status_code, response.content, headers


# (viewer, source) -> readable? The whole point: alice OWNS the notebook and
# still cannot open bob's Memory; nobody opens the ownerless one.
_HTTP_MATRIX = (
    ("alice", "src-e31-mem-alice", True),
    ("alice", "src-e31-mem-bob", False),
    ("alice", "src-e31-mem-orphan", False),
    ("alice", "src-e31-knowhow", True),
    ("alice", "src-e31-doc", True),
    ("bob", "src-e31-mem-bob", True),
    ("bob", "src-e31-mem-alice", False),
    ("bob", "src-e31-mem-orphan", False),
    ("bob", "src-e31-knowhow", True),
    ("bob", "src-e31-doc", True),
)


def test_http_source_reads_open_memory_only_to_its_creator(seeded):
    client = TestClient(seeded["app"])
    notebook_id = seeded["notebook_id"]
    for viewer, source_id, readable in _HTTP_MATRIX:
        headers = seeded["headers"][viewer]
        urls = _read_urls(notebook_id, source_id)
        missing = _read_urls(notebook_id, MISSING)
        for name, url in urls.items():
            response = client.get(url, headers=headers)
            if readable:
                assert response.status_code == 200, (viewer, source_id, name)
                if "elements" in name:
                    body = response.json()
                    items = body["items"] if isinstance(body, dict) else body
                    assert [item["text"] for item in items] == [_TEXT[source_id]]
                else:
                    assert response.json()["id"] == source_id
                continue
            assert _TEXT[source_id] not in response.text, (viewer, source_id, name)
            assert _shape(response) == _shape(
                client.get(missing[name], headers=headers)
            ), f"{viewer} → {source_id} via {name} must look exactly like a missing id"
            assert response.status_code == 404


def test_predictable_element_id_does_not_open_another_members_memory(seeded):
    """The page endpoints accept an anchor element id; knowing the predictable
    id of bob's Memory element must not change the answer for alice."""
    client = TestClient(seeded["app"])
    notebook_id = seeded["notebook_id"]
    anchor = _element_id("src-e31-mem-bob")
    for url in (
        f"/api/sources/src-e31-mem-bob/elements-page?anchor_element_id={anchor}",
        f"/api/notebooks/{notebook_id}/sources/src-e31-mem-bob/elements-page"
        f"?anchor_element_id={anchor}",
    ):
        response = client.get(url, headers=seeded["headers"]["alice"])
        assert response.status_code == 404
        assert "bob 的私人记忆" not in response.text
    # Anchoring alice's own readable document on bob's element id pages the
    # document, never bob's text.
    response = client.get(
        f"/api/sources/src-e31-doc/elements-page?anchor_element_id={anchor}",
        headers=seeded["headers"]["alice"],
    )
    assert response.status_code == 200
    assert "bob 的私人记忆" not in response.text


def _normalized_statements(action, ids: tuple[str, ...]) -> list[str]:
    repo = repository()
    statements: list[str] = []
    with repo._connect() as db:
        db.set_trace_callback(statements.append)
        try:
            action()
        finally:
            db.set_trace_callback(None)
    normalized = []
    for statement in statements:
        for value in ids:
            statement = statement.replace(value, "<ID>")
        normalized.append(re.sub(r"\s+", " ", statement).strip())
    return normalized


def test_refusal_runs_the_same_reads_as_a_missing_id(seeded):
    """No timing shortcut: another member's Memory stops at the SAME statement
    a missing id stops at, in both gate paths (the plain endpoints' service
    check and the proxy helper, which MCP mirrors). Traced in-thread on the
    thread-local connection."""
    bob = seeded["bob"].id

    def service_check(source_id):
        return lambda: notebook_access_repository().user_can_read_source(source_id, bob)

    def proxy_check(source_id):
        def run():
            with pytest.raises(Exception) as caught:
                source_routes._participant_scoped_source(
                    seeded["notebook_id"], source_id, bob
                )
            assert getattr(caught.value, "status_code", None) == 404
        return run

    for make in (service_check, proxy_check):
        refused = _normalized_statements(
            make("src-e31-mem-alice"), ("src-e31-mem-alice", bob)
        )
        missing = _normalized_statements(make(MISSING), (MISSING, bob))
        assert refused and refused == missing, make.__name__
        assert len(refused) == 1, (
            f"{make.__name__}: a refusal must stop at the gate, nothing after it"
        )
        assert "FROM memory_items rm" in refused[0], (
            "the gate is the shared memory_sql fragment"
        )


# --------------------------------------------------------------------------- #
# MCP get_cited_element
# --------------------------------------------------------------------------- #


def _error_text(result) -> str:
    assert result.isError
    return json.dumps([item.text for item in result.content], ensure_ascii=False)


async def _cite(client, source_id, element_id=None):
    return await client.call("get_cited_element", {
        "source_id": source_id,
        "element_id": element_id or _element_id(source_id),
    })


def _token(seeded, user: str, scopes: list[str]) -> str:
    service = seeded["service"]
    owner = seeded[user]
    profile = service.create_agent_profile(owner.id, f"E31 {user} {len(scopes)}", "")
    return service.issue_agent_token(
        owner.id, profile.id, scopes, seeded["notebook_id"],
        [seeded["notebook_id"]], None,
    ).token


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("user", "scopes", "readable"),
    [
        # With memory:read: exactly one's own Memory, plus shared content.
        ("alice", ["knowledge:read", "memory:read"],
         {"src-e31-mem-alice", "src-e31-knowhow", "src-e31-doc"}),
        ("bob", ["knowledge:read", "memory:read"],
         {"src-e31-mem-bob", "src-e31-knowhow", "src-e31-doc"}),
        # Without memory:read: not even the token owner's own Memory.
        ("alice", ["knowledge:read"], {"src-e31-knowhow", "src-e31-doc"}),
    ],
)
async def test_get_cited_element_honours_memory_owner_and_memory_read(
    seeded, user, scopes, readable
):
    token = _token(seeded, user, scopes)
    async with OfficialMcpClient(seeded["app"], token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": seeded["notebook_id"]}
        ))
        missing_text = _error_text(await _cite(client, MISSING))
        for source_id, *_rest in _SOURCES:
            result = await _cite(client, source_id)
            if source_id in readable:
                assert _payload(result)["text"] == _TEXT[source_id], source_id
                continue
            text = _error_text(result)
            assert _TEXT[source_id] not in text
            assert text.replace(source_id, MISSING) == missing_text, (
                f"{user}{scopes}: {source_id} must fail exactly like a missing id"
            )
        # Predictable element id, paired with a source the caller CAN open:
        # the element must belong to that source, so this is a miss too.
        crossed = await _cite(
            client, "src-e31-doc", _element_id("src-e31-mem-bob" if user == "alice"
                                               else "src-e31-mem-alice")
        )
        assert crossed.isError
        assert "私人记忆" not in _error_text(crossed)


# --------------------------------------------------------------------------- #
# Command catalog (D-5 catalog half)
# --------------------------------------------------------------------------- #


def _catalog_calls(notebook_id: str, source_id: str):
    base = f"/api/notebooks/{notebook_id}/sources/{source_id}/command-catalog"
    return (
        ("GET", f"{base}/preview", None),
        ("POST", base, None),
        ("GET", f"{base}/job", None),
        ("POST", f"{base}/cancel", None),
        ("GET", f"{base}/candidates", None),
        ("POST", f"{base}/apply", {"candidate_ids": ["cand-x"]}),
        ("POST", f"{base}/dismiss", {"candidate_ids": ["cand-x"]}),
    )


def test_command_catalog_refuses_hidden_sources_like_a_missing_id(seeded):
    client = TestClient(seeded["app"])
    notebook_id = seeded["notebook_id"]
    owner = seeded["headers"]["alice"]  # catalog:write is owner-only
    missing = _catalog_calls(notebook_id, MISSING)
    for source_id in ("src-e31-mem-alice", "src-e31-mem-bob", "src-e31-knowhow"):
        for (method, url, body), (_m, missing_url, _b) in zip(
            _catalog_calls(notebook_id, source_id), missing
        ):
            response = client.request(method, url, headers=owner, json=body)
            assert _shape(response) == _shape(
                client.request(method, missing_url, headers=owner, json=body)
            ), (source_id, method, url)
            assert response.status_code == 404
            assert f"{source_id} 标题" not in response.text
    # Control: an ordinary document gets PAST the source check on every
    # endpoint (whatever it then answers -- 200, 409 "model not configured",
    # 404 "Catalog job not found" -- it is not the missing-source answer).
    for (method, url, body), (_m, missing_url, _b) in zip(
        _catalog_calls(notebook_id, "src-e31-doc"), missing
    ):
        response = client.request(method, url, headers=owner, json=body)
        assert _shape(response) != _shape(
            client.request(method, missing_url, headers=owner, json=body)
        ), (method, url, response.text)


def test_hidden_source_ids_admit_only_the_viewers_own_memory(seeded):
    """The retrieval ceiling's hidden half reads through the same fragment the
    endpoints gate on, so the two cannot disagree about whose Memory is whose."""
    store = repository()
    notebook_id = seeded["notebook_id"]
    assert store.hidden_source_ids(notebook_id, seeded["alice"].id) == [
        "src-e31-knowhow", "src-e31-mem-alice",
    ]
    assert store.hidden_source_ids(notebook_id, seeded["bob"].id) == [
        "src-e31-knowhow", "src-e31-mem-bob",
    ]
    assert store.hidden_source_ids(notebook_id, "") == ["src-e31-knowhow"]


def test_ungated_notebook_lookup_is_unchanged_for_write_callers(seeded):
    """``viewer_id`` omitted keeps the historical lookup: the write-side
    capability guards (parse/delete) resolve any source's notebook."""
    access = notebook_access_repository()
    assert access.source_notebook_id("src-e31-mem-bob") == seeded["notebook_id"]
    assert access.source_notebook_id(
        "src-e31-mem-bob", viewer_id=seeded["alice"].id
    ) is None
    assert access.source_notebook_id(
        "src-e31-mem-bob", viewer_id=seeded["bob"].id
    ) == seeded["notebook_id"]
    assert access.source_notebook_id("src-e31-mem-bob", viewer_id="") is None
    assert access.source_notebook_id(MISSING, viewer_id=seeded["bob"].id) is None
