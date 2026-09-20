"""全局问答会话公开分享的 HTTP 面:三个已认证端点 + 匿名读取(含图片)。

单库那一套(`test_conversation_share_api.py` / `test_public_conversation_api.py` /
`test_public_conversation_asset_api.py`)已经钉住了 token 幂等、水位、白名单投影与
别名通道本身;这里覆盖的是**全局侧真正不同的那一半**:

  * 三个端点挂在 `/global-ask/conversations/{id}/share` 上,没有 notebook 层守卫——
    所有权就是全部的闸;`expected_through_id` 是**作业 id**。
  * `DELETE` 的 404 必须来自服务层所有权判定:store 的 unshare 是一条无返回值的
    幂等 UPDATE,别人的会话 id 不加这道闸就会得到 204。
  * 匿名读取按 token 前缀分流(`gshr-` → 全局,其余 → 单库),两个命名空间互不命中。
  * **每次打开都实时复核**分享者对本快照被引各库的读权:任一读不了 → 整条链接
    404,恢复即复活;零引用的快照退化为复核 `resolved_notebook_ids`,不许变成零复核。
  * 图片额外一条闸:资产所属库必须落在**本次已复核**的库集合内。
  * 公开响应体是白名单:任何可寻址 id、库名、回执、轨迹、意图都不得出现。
"""
from __future__ import annotations

import json
from itertools import count

import pytest
from fastapi.testclient import TestClient

from tests.global_ask_share_cases import insert_conversation, insert_job


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"g{index:08d}" for index in range(1, 999))
_IDS = count(1)

_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nglobal-share-fixture-image"

# 一条公开响应体里绝不允许出现的键名。前四类是可寻址 id(拿去探已认证 API),
# 后面是回执 / 轨迹 / 意图——用户裁决明确不对外。
_FORBIDDEN_KEYS = frozenset({
    "notebook_id", "notebook_ids", "notebook_scope", "source_id", "element_id",
    "object_id", "memory_id", "asset_id", "job_id", "conversation_id", "id",
    "user_id", "created_by", "share_token", "shared_through_id",
    "reasoning_trace", "intent", "retrieval_scope", "retrieval_query",
    "skipped_notebooks", "degraded_notebook_ids", "resolved_notebook_ids",
    "searched_notebook_ids", "cited_notebook_ids", "trace", "provenance",
    "knowhow", "url",
})


def _build_client(monkeypatch, tmp_path, *, images_enabled: bool = True) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true" if images_enabled else "false")
    from app.core.config import get_settings

    # conftest clears this at test start; rebuild it against the env just set so
    # the request-time ``get_settings().mineru_return_images`` reads our value.
    get_settings.cache_clear()
    from app.main import app

    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    return _build_client(monkeypatch, tmp_path, images_enabled=True)


@pytest.fixture
def client_no_images(tmp_path, monkeypatch) -> TestClient:
    return _build_client(monkeypatch, tmp_path, images_enabled=False)


# ------------------------------------------------------------------ 夹具工具


def _new_user(client: TestClient) -> tuple[dict, str]:
    username = next(_USERNAMES)
    client.post("/api/auth/register",
                json={"username": username, "password": _PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def _notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name},
                       headers=headers).json()["id"]


def _store():
    from app.api.deps import repository

    return repository()._runtime.global_ask_store


def _save_asset(notebook_id: str, created_by: str) -> str:
    from app.api.deps import repository
    from app.services.knowhow.assets import AssetService

    asset = AssetService(repository()).save(
        notebook_id, "figure.png", "image/png", _IMAGE_BYTES, created_by
    )
    return asset["id"]


def _answer(*, asset_id: str | None = None) -> dict:
    """一条 reasoning 形状的回答:锚点携带**每一个**可寻址字段,于是投影的泄露
    面可以被端到端检查。"""
    images = ([{"element_id": "IMGELE-secret", "asset_id": asset_id,
                "caption": "配图说明"}] if asset_id else [])
    return {
        "conclusion": "简要结论 [k1]。",
        "answer": "详细答案 [k1]。",
        "asked_at": "2026-01-01T00:00:00+08:00",
        "answered_at": "2026-01-01T00:00:02+08:00",
        "evidence_level": "grounded",
        "anchors": [{
            "key": "k1", "object_id": "OBJ-secret", "object_type": "concept",
            "label": "标签", "name": "名称", "snippet": "锚点摘录内容",
            "source_title": "论文标题甲", "source_file_name": "paper.pdf",
            "location_label": "第 2 节", "source_id": "SRC-secret",
            "element_id": "ELE-secret", "tier": "personal",
            "notebook_id": "NB-secret", "images": images,
        }],
        "citations": [],
        "reasoning_trace": [{"step_type": "plan", "summary": "轨迹泄露词"}],
        "retrieval_query": "检索泄露词",
    }


def _job_payload(job_id: str, conversation_id: str, *, question: str,
                 cited: list[str], resolved: list[str],
                 answer: dict | None = None, legacy: bool = False) -> dict:
    body = {
        "job_id": job_id, "conversation_id": conversation_id, "status": "done",
        "question": question, "created_at": "2026-01-01T00:00:01",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": resolved, "cited_notebook_ids": cited,
        "searched_notebook_ids": resolved,
        "skipped_notebooks": [{"notebook_id": "NB-skipped", "reason": "timeout"}],
        "degraded_notebook_ids": ["NB-degraded"],
    }
    shape = answer if answer is not None else _answer()
    # The fixture anchor's placeholder library becomes this turn's real one: the
    # re-check reads the libraries named on anchors and citations too (the public
    # page renders anchors first), so a made-up id there is a library the sharer
    # can never read and every link built from the fixture would be born dead.
    owning = (cited or resolved or [""])[0]
    for key in ("anchors", "citations"):
        for item in shape.get(key) or ():
            if isinstance(item, dict) and item.get("notebook_id") == "NB-secret":
                item["notebook_id"] = owning
    # 旧形状的轮次写在 ``response`` 下(引擎切换之前的持久化),新形状写 ``answer``。
    body["response" if legacy else "answer"] = shape
    return body


def _seed(user_id: str, jobs: list[dict], *, conversation_id: str | None = None) -> str:
    store = _store()
    cid = conversation_id or f"gconv-{next(_IDS)}"
    insert_conversation(store, cid, user_id)
    for index, payload in enumerate(jobs):
        insert_job(store, cid, user_id, payload["job_id"],
                   f"2026-01-01T00:00:0{index + 1}", payload=payload)
    return cid


def _share_path(conversation_id: str) -> str:
    return f"/api/global-ask/conversations/{conversation_id}/share"


def _share(client: TestClient, headers: dict, cid: str,
           expected_through_id: str = "") -> dict:
    resp = client.post(_share_path(cid), headers=headers,
                       json={"expected_through_id": expected_through_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _grant_notebook_to_group(client, owner, nb, group_id) -> str:
    grant = client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner,
    )
    assert grant.status_code == 200, grant.text
    return grant.json()["id"]


def _group_granted_reader(client, owner, notebooks) -> tuple[dict, str, str, dict]:
    """建组 → 拉新用户进组 → 把若干库共享给该组。返回
    (reader headers, reader id, group id, {notebook: grant id})。"""
    reader, reader_id = _new_user(client)
    group_id = client.post("/api/groups", json={"name": "组"},
                           headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"}, headers=owner,
    ).status_code == 200
    grants = {nb: _grant_notebook_to_group(client, owner, nb, group_id)
              for nb in notebooks}
    return reader, reader_id, group_id, grants


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


# ------------------------------------------------------- 已认证三端点


def test_share_is_token_idempotent_and_advances_the_watermark(client):
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题一", cited=[nb], resolved=[nb]),
        _job_payload("job-b", "x", question="问题二", cited=[nb], resolved=[nb]),
    ])

    first = _share(client, owner, cid, "job-a")
    assert first["share_token"].startswith("gshr-")
    assert first["shared_through_id"] == "job-a"

    second = _share(client, owner, cid, "job-b")
    assert second["share_token"] == first["share_token"]
    assert second["shared_through_id"] == "job-b"

    read_back = client.get(_share_path(cid), headers=owner)
    assert read_back.status_code == 200
    assert read_back.json() == second


def test_expected_through_id_has_three_outcomes(client):
    """命中 / 解析不到 / 回退——后两者都是 409,文案与单库逐字相同。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题一", cited=[nb], resolved=[nb]),
        _job_payload("job-b", "x", question="问题二", cited=[nb], resolved=[nb]),
    ])

    assert _share(client, owner, cid, "job-b")["shared_through_id"] == "job-b"

    unknown = client.post(_share_path(cid), headers=owner,
                          json={"expected_through_id": "job-never"})
    assert unknown.status_code == 409
    assert unknown.json()["detail"] == "这条会话已有变化，请刷新后重新分享。"

    regress = client.post(_share_path(cid), headers=owner,
                          json={"expected_through_id": "job-a"})
    assert regress.status_code == 409
    assert regress.json()["detail"] == "这条会话已有变化，请刷新后重新分享。"

    # 两次 409 都没有动过已发布的水位。
    assert client.get(
        _share_path(cid), headers=owner
    ).json()["shared_through_id"] == "job-b"


def test_a_conversation_with_no_completed_answer_is_409_and_mints_no_token(client):
    owner, owner_id = _new_user(client)
    cid = _seed(owner_id, [])
    running = _job_payload("job-run", cid, question="问题", cited=[], resolved=[])
    running["status"] = "running"
    insert_job(_store(), cid, owner_id, "job-run", "2026-01-01T00:00:01",
               status="running", payload=running)

    resp = client.post(_share_path(cid), headers=owner, json={})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "这条会话还没有已完成的回答，暂时无法分享。"
    # 没有铸出 token:回读仍是「未分享」的 404。
    assert client.get(_share_path(cid), headers=owner).status_code == 404


def test_get_share_is_404_until_the_conversation_is_shared(client):
    """弹窗按这个 404 判「正常未分享」,所以它必须是 404 而不是空 200。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb]),
    ])

    assert client.get(_share_path(cid), headers=owner).status_code == 404
    _share(client, owner, cid, "job-a")
    assert client.get(_share_path(cid), headers=owner).status_code == 200


def test_a_foreign_or_missing_conversation_is_404_on_all_three_endpoints(client):
    """别人的会话与不存在的会话得到同一个 404,且 DELETE 不改动它的分享状态。

    DELETE 这条是真正会滑掉的一条:store 的 ``unshare_conversation`` 是一条无返回
    值的幂等 UPDATE,匹配不到行也照样「成功」,所以没有服务层所有权闸的话,别人的
    会话 id 会拿到 204——一个能被外人无声调用的撤销入口。
    """
    owner, owner_id = _new_user(client)
    intruder, _ = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb]),
    ])
    published = _share(client, owner, cid, "job-a")

    for target in (cid, "gconv-does-not-exist"):
        assert client.get(
            _share_path(target), headers=intruder).status_code == 404
        assert client.post(
            _share_path(target), headers=intruder, json={}).status_code == 404
        assert client.delete(
            _share_path(target), headers=intruder).status_code == 404

    # 本人回读:token 与水位一字未动,链接仍然可读。
    assert client.get(_share_path(cid), headers=owner).json() == published
    assert client.get(
        f"/api/public/conversations/{published['share_token']}"
    ).status_code == 200


def test_unsharing_is_idempotent_for_the_owner_and_mints_a_new_token_next_time(client):
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb]),
    ])
    first = _share(client, owner, cid, "job-a")["share_token"]

    assert client.delete(_share_path(cid), headers=owner).status_code == 204
    # 自己的、已经撤销过的会话再撤一次仍是 204(与单库一致)。
    assert client.delete(_share_path(cid), headers=owner).status_code == 204
    assert client.get(f"/api/public/conversations/{first}").status_code == 404

    second = _share(client, owner, cid, "job-a")["share_token"]
    assert second != first
    assert client.get(f"/api/public/conversations/{first}").status_code == 404
    assert client.get(f"/api/public/conversations/{second}").status_code == 200


# ------------------------------------------------------- 匿名读取 + 实时复核


def test_anonymous_reader_gets_the_whitelisted_projection(client):
    """不带任何 session 就能读到白名单投影:问答正文 + 证据标题/摘录,别的都没有。

    禁用词表是**穷举**的:fixture 里每一个 id 字符串、库名、回执词都列进来,直接对
    序列化后的响应文本断言。把投影换成「直接回传 payload」会当场让这条红。
    """
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner, "泄露库名甲")
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="我的问题?", cited=[nb], resolved=[nb]),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]

    resp = client.get(f"/api/public/conversations/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "全局会话"
    assert [turn["question"] for turn in body["turns"]] == ["我的问题?"]
    assert body["turns"][0]["answer_md"] == "详细答案 [k1]。"
    assert body["turns"][0]["evidence_level"] == "grounded"
    assert body["turns"][0]["references"][0]["title"] == "论文标题甲"
    assert body["turns"][0]["references"][0]["snippet"] == "锚点摘录内容"

    raw = resp.text
    for banned in (nb, cid, "job-a", owner_id, token, "泄露库名甲", "OBJ-secret",
                   "SRC-secret", "ELE-secret", "NB-secret", "NB-skipped",
                   "NB-degraded", "轨迹泄露词", "检索泄露词", "timeout"):
        assert banned not in raw, banned
    leaked = _FORBIDDEN_KEYS.intersection(_walk_keys(body))
    assert not leaked, leaked


def test_a_legacy_response_shaped_turn_still_projects(client):
    """引擎切换之前写下的轮次挂在 ``response`` 上,且没有
    ``asked_at``/``answered_at``/``evidence_level``——走投影既有的退化分支,而不是
    从公开页上消失。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    # `GlobalAskAnswer` 的真实形状:它没有 `conclusion`/`asked_at`/`evidence_level`,
    # 这正是投影退化分支要覆盖的那一档。
    legacy = {
        "answer_id": "OLD-ANS", "question": "旧问题", "answer": "旧形状答案 [k1]。",
        "grounded": True, "created_at": "2026-01-01T00:00:01",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": [nb], "searched_notebook_ids": [nb],
        "cited_notebook_ids": [nb],
        "anchors": [{"key": "k1", "object_id": "OLD-OBJ", "object_type": "concept",
                     "label": "旧标签", "snippet": "旧摘录"}],
        "citations": [],
    }
    cid = _seed(owner_id, [
        _job_payload("job-old", "x", question="旧问题", cited=[nb], resolved=[nb],
                     answer=legacy, legacy=True),
    ])
    token = _share(client, owner, cid, "job-old")["share_token"]

    turn = client.get(f"/api/public/conversations/{token}").json()["turns"][0]
    assert turn["question"] == "旧问题"
    assert turn["answer_md"] == "旧形状答案 [k1]。"
    assert turn["references"][0]["snippet"] == "旧摘录"
    assert (turn["asked_at"], turn["answered_at"]) == ("", "")
    assert turn["evidence_level"] == "inferred"


def test_the_link_dies_when_a_cited_library_becomes_unreadable_and_revives(client):
    """用户裁决:每次打开都实时复核分享者对**被引各库**的读权。

    组成员用共享库的语料问出一条全局回答并分享;撤销授权 → 整条链接当场 404,
    恢复 → **同一个** token 复活。未被引用的那个库读不了不影响。
    """
    owner, _owner_id = _new_user(client)
    cited_nb = _notebook(client, owner, "被引库")
    other_nb = _notebook(client, owner, "未引库")
    reader, reader_id, group_id, grants = _group_granted_reader(
        client, owner, [cited_nb, other_nb])
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题", cited=[cited_nb],
                     resolved=[cited_nb, other_nb]),
    ])
    token = _share(client, reader, cid, "job-a")["share_token"]
    page = f"/api/public/conversations/{token}"

    assert client.get(page).status_code == 200

    # 未被引用的库失权:引用面没变,链接照常。
    assert client.delete(
        f"/api/notebooks/{other_nb}/grants/{grants[other_nb]}", headers=owner
    ).status_code == 204
    assert client.get(page).status_code == 200

    # 被引用的库失权:整条链接 404,与未知 token 不可区分。
    assert client.delete(
        f"/api/notebooks/{cited_nb}/grants/{grants[cited_nb]}", headers=owner
    ).status_code == 204
    assert client.get(page).status_code == 404

    # 权限恢复 → 同一个 token 复活。
    _grant_notebook_to_group(client, owner, cited_nb, group_id)
    assert client.get(page).status_code == 200


def test_a_snapshot_that_cited_nothing_re_checks_its_resolved_libraries(client):
    """零引用不等于零复核:没有任何 ``cited_notebook_ids`` 时退化为复核各轮
    ``resolved_notebook_ids``,否则一条未接地的回答就成了永不失效的链接。"""
    owner, _owner_id = _new_user(client)
    nb = _notebook(client, owner)
    reader, reader_id, _group_id, grants = _group_granted_reader(client, owner, [nb])
    ungrounded = {"conclusion": "没有找到可引用的原文。",
                  "answer": "没有找到可引用的原文。",
                  "evidence_level": "inferred", "anchors": [], "citations": []}
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题", cited=[], resolved=[nb],
                     answer=ungrounded),
    ])
    token = _share(client, reader, cid, "job-a")["share_token"]

    assert client.get(f"/api/public/conversations/{token}").status_code == 200
    assert client.delete(
        f"/api/notebooks/{nb}/grants/{grants[nb]}", headers=owner
    ).status_code == 204
    assert client.get(f"/api/public/conversations/{token}").status_code == 404


def test_a_zero_citation_turn_is_re_checked_even_when_another_turn_cites(client):
    """退化到 resolved 是**逐轮**判的,不是整份快照判一次。

    第一轮引用库 A;第二轮是枚举型回答——引擎把它的 citations 清空了,正文却仍是
    库 B 的文档清单。整份判空时,第一轮的引用会把退化分支关掉,库 B 从此不被复核:
    分享者失去库 B 的读权之后,这条链接照样把库 B 的内容端给匿名读者(安全评审 P1)。
    """
    owner, _owner_id = _new_user(client)
    nb_a, nb_b = _notebook(client, owner), _notebook(client, owner)
    reader, reader_id, group_id, grants = _group_granted_reader(
        client, owner, [nb_a, nb_b])
    enumeration = {"conclusion": "库里有这些文档:甲、乙。",
                   "answer": "库里有这些文档:甲、乙。",
                   "evidence_level": "overview", "anchors": [], "citations": []}
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题一", cited=[nb_a], resolved=[nb_a]),
        _job_payload("job-b", "x", question="列出文档", cited=[], resolved=[nb_b],
                     answer=enumeration),
    ])
    token = _share(client, reader, cid, "job-b")["share_token"]
    page = f"/api/public/conversations/{token}"
    assert client.get(page).status_code == 200

    assert client.delete(
        f"/api/notebooks/{nb_b}/grants/{grants[nb_b]}", headers=owner
    ).status_code == 204
    assert client.get(page).status_code == 404

    _grant_notebook_to_group(client, owner, nb_b, group_id)
    assert client.get(page).status_code == 200


def test_a_library_named_only_on_an_anchor_is_re_checked(client):
    """公开页锚点优先渲染,所以复核集合不能只从 citations 推。

    一条证据的所属库只落在锚点上、``cited_notebook_ids`` 里没有它时,只读 cited 的
    复核会放过这个库,而公开页展示的正是这条锚点的标题与原文片段。
    """
    owner, _owner_id = _new_user(client)
    nb_cited, nb_anchor = _notebook(client, owner), _notebook(client, owner)
    reader, reader_id, _group_id, grants = _group_granted_reader(
        client, owner, [nb_cited, nb_anchor])
    answer = _answer()
    assert answer["anchors"], "fixture answer must carry an anchor for this case"
    answer["anchors"][0]["notebook_id"] = nb_anchor
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb_cited],
                     resolved=[nb_cited, nb_anchor], answer=answer),
    ])
    token = _share(client, reader, cid, "job-a")["share_token"]
    page = f"/api/public/conversations/{token}"
    assert client.get(page).status_code == 200

    assert client.delete(
        f"/api/notebooks/{nb_anchor}/grants/{grants[nb_anchor]}", headers=owner
    ).status_code == 204
    assert client.get(page).status_code == 404


def test_sharing_is_refused_while_a_referenced_library_is_unreadable(client):
    """分享者此刻读不了的库被引用 → 不许创建分享(否则发出去的是一条生来就 404
    的链接)。权限恢复后同一次点击就能成功。"""
    owner, _owner_id = _new_user(client)
    nb = _notebook(client, owner)
    reader, reader_id, group_id, grants = _group_granted_reader(client, owner, [nb])
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb]),
    ])

    assert client.delete(
        f"/api/notebooks/{nb}/grants/{grants[nb]}", headers=owner
    ).status_code == 204
    refused = client.post(_share_path(cid), headers=reader,
                          json={"expected_through_id": "job-a"})
    assert refused.status_code == 404
    assert refused.json()["detail"] == "部分笔记本已无法访问，请重新选择范围。"
    assert client.get(_share_path(cid), headers=reader).status_code == 404

    _grant_notebook_to_group(client, owner, nb, group_id)
    assert _share(client, reader, cid, "job-a")["share_token"].startswith("gshr-")


def test_an_unpublished_later_turn_cannot_veto_sharing_an_earlier_one(client):
    """分享前的读权复核只看**将要公开的那段前缀**。

    早一轮用的是仍可读的库 A,晚一轮用的是已被撤权的库 B:只分享到早一轮时,晚一轮
    根本不公开,它不该否决这次分享——匿名读取本来就会成功(codex #758 第 1 轮 P2)。
    分享到晚一轮则照旧拒绝。
    """
    owner, _owner_id = _new_user(client)
    nb_a, nb_b = _notebook(client, owner), _notebook(client, owner)
    reader, reader_id, _group_id, grants = _group_granted_reader(
        client, owner, [nb_a, nb_b])
    cid = _seed(reader_id, [
        _job_payload("job-early", "x", question="早一轮", cited=[nb_a], resolved=[nb_a]),
        _job_payload("job-late", "x", question="晚一轮", cited=[nb_b], resolved=[nb_b]),
    ])
    assert client.delete(
        f"/api/notebooks/{nb_b}/grants/{grants[nb_b]}", headers=owner
    ).status_code == 204

    token = _share(client, reader, cid, "job-early")["share_token"]
    page = client.get(f"/api/public/conversations/{token}")
    assert page.status_code == 200
    assert [turn["question"] for turn in page.json()["turns"]] == ["早一轮"]

    refused = client.post(_share_path(cid), headers=reader,
                          json={"expected_through_id": "job-late"})
    assert refused.status_code == 404
    # 被拒的那次推进不许动已发布的水位。
    assert [turn["question"] for turn in
            client.get(f"/api/public/conversations/{token}").json()["turns"]] == ["早一轮"]


# ------------------------------------------------------- token 命名空间分流


def test_the_two_share_token_namespaces_never_cross(client):
    """``gshr-`` 与 ``cshr-`` 划分 token 空间:任一分支都不会命中另一个的 token。

    这是全局侧复用同一个匿名端点(于是 ``/c/{token}`` 只有一个公开页)的前提;
    分流一旦退化成「先查单库、查不到再查全局」,两种 token 的失败面就会互相串味。
    """
    from app.api.deps import repository

    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    gcid = _seed(owner_id, [
        _job_payload("job-a", "x", question="全局问题", cited=[nb], resolved=[nb]),
    ])
    gtoken = _share(client, owner, gcid, "job-a")["share_token"]

    # 单库会话 + 一条答案,走单库端点铸它自己的 token。
    ncid = f"nconv-{next(_IDS)}"
    db = repository()._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, '单库会话', ?, '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:02')", (ncid, nb, owner_id))
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES (?, ?, ?, '单库问题?', ?, '2026-01-01T00:00:01')",
            (f"{ncid}-ans", nb, ncid, json.dumps(_answer(), ensure_ascii=False)))
    ntoken = client.post(
        f"/api/notebooks/{nb}/conversations/{ncid}/share", headers=owner
    ).json()["share_token"]
    assert ntoken.startswith("cshr-")

    # 每个 token 只在自己的分支上解析,且各自解析出自己的标题。
    assert client.get(
        f"/api/public/conversations/{gtoken}").json()["title"] == "全局会话"
    assert client.get(
        f"/api/public/conversations/{ntoken}").json()["title"] == "单库会话"

    # 把前缀换成对方的:两边都 404,没有任何一侧兜住对方的 token。
    swapped_global = "cshr-" + gtoken.split("-", 1)[1]
    swapped_notebook = "gshr-" + ntoken.split("-", 1)[1]
    assert client.get(
        f"/api/public/conversations/{swapped_global}").status_code == 404
    assert client.get(
        f"/api/public/conversations/{swapped_notebook}").status_code == 404


def test_the_anonymous_global_read_binds_no_request_user(client, monkeypatch):
    """匿名面上**没有**请求用户,所以实现不许调用任何「按当前用户取身份」的仓储
    方法:ContextVar 未设置时它会回退到种子管理员,那等于把匿名读者当管理员授权。

    行为级钉法:把 ``identity.current_user`` 换成当场抛错的实现,全局公开页与图片
    端点仍必须 200。顺带断言这两条路由没有继承 router 级的会话守卫。
    """
    from app.api.deps import get_current_user, repository
    from app.main import create_app

    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb],
                     answer=_answer(asset_id=asset_id)),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]
    alias = client.get(
        f"/api/public/conversations/{token}"
    ).json()["turns"][0]["images"][0]["alias"]

    def _explode():
        raise AssertionError("匿名路径读取了当前用户")

    monkeypatch.setattr(
        repository()._runtime.identity, "current_user", _explode, raising=True
    )
    assert client.get(f"/api/public/conversations/{token}").status_code == 200
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 200

    app = create_app()
    for path in ("/api/public/conversations/{token}",
                 "/api/public/conversations/{token}/assets/{alias}"):
        routes = [route for route in app.routes
                  if getattr(route, "path", "") == path]
        assert routes, path
        for route in routes:
            dependant = getattr(route, "dependant", None)
            calls = [d.call for d in dependant.dependencies] if dependant else []
            assert get_current_user not in calls, path


# ------------------------------------------------------- 匿名图片通道


def test_a_disclosed_image_is_fetchable_and_dies_with_the_link(client):
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb],
                     answer=_answer(asset_id=asset_id)),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]

    page = client.get(f"/api/public/conversations/{token}")
    alias = page.json()["turns"][0]["images"][0]["alias"]
    assert asset_id not in page.text and "IMGELE-secret" not in page.text

    resp = client.get(f"/api/public/conversations/{token}/assets/{alias}")
    assert resp.status_code == 200, resp.text
    assert resp.content == _IMAGE_BYTES
    assert resp.headers["cache-control"] == "no-store"

    assert client.delete(_share_path(cid), headers=owner).status_code == 204
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}").status_code == 404


def test_an_asset_outside_the_re_checked_libraries_is_404(client):
    """全局侧独有的一条闸:资产所属库必须落在**本次已复核**的库集合内。

    快照跨多个库,「冻结的快照就是授权」只能按库逐个成立——一张属于本次没有复核过
    的库的图,不许靠别的库把链接撑着而继续供字节。去掉这条检查它会 200。
    """
    owner, owner_id = _new_user(client)
    cited_nb = _notebook(client, owner, "被引库")
    stray_nb = _notebook(client, owner, "未复核库")
    stray_asset = _save_asset(stray_nb, owner_id)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[cited_nb],
                     resolved=[cited_nb], answer=_answer(asset_id=stray_asset)),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]

    # 页面照样发别名(投影不认识库),端点必须拒绝。
    alias = client.get(
        f"/api/public/conversations/{token}"
    ).json()["turns"][0]["images"][0]["alias"]
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}").status_code == 404


def test_an_undisclosed_asset_alias_is_404(client):
    """存在、但本次分享没有披露的资产:端点只对**页面实际发出的**那批别名反查。"""
    from app.services.conversation_public_view import conversation_asset_alias

    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    referenced = _save_asset(nb, owner_id)
    stranger = _save_asset(nb, owner_id)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb],
                     answer=_answer(asset_id=referenced)),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]

    assert client.get(
        f"/api/public/conversations/{token}/assets/"
        f"{conversation_asset_alias(token, stranger)}"
    ).status_code == 404
    assert client.get(
        f"/api/public/conversations/{token}/assets/{referenced}"
    ).status_code == 404


def test_the_image_dies_when_a_cited_library_becomes_unreadable(client):
    owner, _owner_id = _new_user(client)
    nb = _notebook(client, owner)
    reader, reader_id, group_id, grants = _group_granted_reader(client, owner, [nb])
    asset_id = _save_asset(nb, reader_id)
    cid = _seed(reader_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb],
                     answer=_answer(asset_id=asset_id)),
    ])
    token = _share(client, reader, cid, "job-a")["share_token"]
    alias = client.get(
        f"/api/public/conversations/{token}"
    ).json()["turns"][0]["images"][0]["alias"]
    image = f"/api/public/conversations/{token}/assets/{alias}"

    assert client.get(image).status_code == 200
    assert client.delete(
        f"/api/notebooks/{nb}/grants/{grants[nb]}", headers=owner
    ).status_code == 204
    assert client.get(image).status_code == 404
    _grant_notebook_to_group(client, owner, nb, group_id)
    assert client.get(image).status_code == 200


def test_images_off_deployment_wide_emits_no_alias_and_serves_no_bytes(client_no_images):
    """``MINERU_RETURN_IMAGES=false``:投影不发别名,端点也不供字节——自己算出来的
    别名同样取不到(与单库同一条短路)。"""
    from app.services.conversation_public_view import conversation_asset_alias

    client = client_no_images
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed(owner_id, [
        _job_payload("job-a", "x", question="问题", cited=[nb], resolved=[nb],
                     answer=_answer(asset_id=asset_id)),
    ])
    token = _share(client, owner, cid, "job-a")["share_token"]

    assert client.get(
        f"/api/public/conversations/{token}").json()["turns"][0]["images"] == []
    assert client.get(
        f"/api/public/conversations/{token}/assets/"
        f"{conversation_asset_alias(token, asset_id)}"
    ).status_code == 404
