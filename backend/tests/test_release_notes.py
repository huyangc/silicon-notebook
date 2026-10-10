"""系统更新通知:清单读取、待看说明计算、每账号已看基线与两条接口。

覆盖:
  * ``parse_release_manifest`` 的形状校验(schema/字段类型/级别与受众枚举/标题/重复 id)。
  * ``load_release_manifest``:缺失/损坏 → None,日志只记原因码不记正文,
    按 (mtime, size) 缓存。
  * ``pending_release_notes``:边界含右不含左、回滚、NULL 之外的相等、排序、受众可见性。
  * 弹窗重点/计数分级(``release_notes_for_user``)与更新记录(``release_notes_history``)。
  * ``release-notes/*.md`` 真实目录的文案长度守卫。
  * SQLite IdentityStore 的三个端口方法(条件初始化、原子取 max)。
  * GET/POST /api/me/release-notes[/seen] 的端到端语义。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.services import release_notes as rn
from app.services.release_notes import (
    ManifestInvalid,
    ReleaseNote,
    load_release_manifest,
    parse_release_manifest,
    pending_release_notes,
)
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture(autouse=True)
def _isolate():
    rn._cache.clear()
    yield
    rn._cache.clear()
    set_request_user(None)


def _manifest(build_ordinal=100, notes=(), version="20260929-abc1234", schema=2):
    """``notes`` entries: ``(id, ordinal, body[, level[, audience]])``; the title is
    ``t-<id>``. Default level ``change`` / audience ``all``."""
    out = []
    for nid, ordinal, body, *rest in notes:
        level, audience = (list(rest) + ["change", "all"][len(rest):])[:2]
        out.append({
            "id": nid, "ordinal": ordinal, "sha": "b" * 40,
            "level": level, "audience": audience, "title": f"t-{nid}", "body": body,
        })
    return {
        "schema": schema,
        "build": {"version": version, "sha": "a" * 40, "ordinal": build_ordinal},
        "notes": out,
    }


# --------------------------------------------------------------------------- #
# 1. 校验
# --------------------------------------------------------------------------- #
def test_parse_accepts_a_well_formed_manifest():
    parsed = parse_release_manifest(
        _manifest(100, [("a", 90, "说明 A"), ("b", 95, "说明 B")])
    )
    assert parsed.build.version == "20260929-abc1234"
    assert parsed.build.ordinal == 100
    assert [(n.id, n.ordinal, n.level, n.audience, n.title, n.body) for n in parsed.notes] == [
        ("a", 90, "change", "all", "t-a", "说明 A"),
        ("b", 95, "change", "all", "t-b", "说明 B"),
    ]


def test_parse_accepts_an_empty_body():
    parsed = parse_release_manifest(_manifest(notes=[("a", 1, "")]))
    assert parsed.notes[0].body == ""


def _with_field(**changes):
    raw = _manifest(notes=[("a", 1, "x")])
    raw["notes"][0].update(changes)
    return raw


@pytest.mark.parametrize(
    "raw, code",
    [
        ([], "not_an_object"),
        ("x", "not_an_object"),
        (_manifest(schema=1), "unsupported_schema"),
        (_manifest(schema=3), "unsupported_schema"),
        ({**_manifest(), "schema": True}, "unsupported_schema"),
        ({k: v for k, v in _manifest().items() if k != "schema"}, "unsupported_schema"),
        ({**_manifest(), "build": []}, "bad_build"),
        ({**_manifest(), "build": {"version": "", "ordinal": 1}}, "bad_build"),
        ({**_manifest(), "build": {"version": "v", "ordinal": "1"}}, "bad_build"),
        ({**_manifest(), "build": {"version": "v", "ordinal": True}}, "bad_build"),
        ({**_manifest(), "build": {"version": "v", "ordinal": -1}}, "bad_build"),
        ({**_manifest(), "notes": {}}, "bad_notes"),
        ({**_manifest(), "notes": ["x"]}, "bad_note"),
        (_manifest(notes=[("", 1, "x")]), "bad_note"),
        (_manifest(notes=[("a", "1", "x")]), "bad_note"),
        (_manifest(notes=[("a", 1.5, "x")]), "bad_note"),
        (_with_field(body=None), "bad_note"),
        (_with_field(level="urgent"), "bad_note"),
        (_with_field(level=None), "bad_note"),
        (_with_field(audience="staff"), "bad_note"),
        (_with_field(audience=None), "bad_note"),
        (_with_field(title=""), "bad_note"),
        (_with_field(title="   "), "bad_note"),
        (_with_field(title=3), "bad_note"),
        (_with_field(title=None), "bad_note"),
        (_manifest(notes=[("a", 1, "x"), ("a", 2, "y")]), "duplicate_note"),
    ],
)
def test_parse_rejects_malformed_manifests_with_a_code(raw, code):
    with pytest.raises(ManifestInvalid) as excinfo:
        parse_release_manifest(raw)
    assert excinfo.value.code == code


# --------------------------------------------------------------------------- #
# 2. 读取
# --------------------------------------------------------------------------- #
def test_load_missing_file_is_unavailable_without_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.release_notes"):
        assert load_release_manifest(tmp_path / "nope.json") is None
    assert caplog.records == []


def test_load_corrupt_file_logs_the_reason_code_never_the_body(tmp_path, caplog):
    path = tmp_path / "release-manifest.json"
    secret = "内部说明正文-不许进日志"
    path.write_text('{"schema": 2, "build": ' + secret, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.release_notes"):
        assert load_release_manifest(path) is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "unreadable" in text and "JSONDecodeError" in text
    assert secret not in text


def test_load_invalid_shape_logs_code_only_and_warns_once_per_file_version(
    tmp_path, caplog
):
    path = tmp_path / "release-manifest.json"
    bad = _manifest(notes=[("a", 1, "机密正文")], schema=9)
    path.write_text(json.dumps(bad), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.release_notes"):
        assert load_release_manifest(path) is None
        assert load_release_manifest(path) is None
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "unsupported_schema" in messages[0]
    assert "机密正文" not in messages[0]


def test_load_reparses_when_the_file_changes(tmp_path):
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest(100)), encoding="utf-8")
    first = load_release_manifest(path)
    assert first is not None and first.build.ordinal == 100
    assert load_release_manifest(path) is first  # cached
    path.write_text(json.dumps(_manifest(2000)), encoding="utf-8")
    second = load_release_manifest(path)
    assert second is not None and second.build.ordinal == 2000


def test_load_reparses_a_same_size_rewrite_in_place(tmp_path):
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest(120)), encoding="utf-8")
    first = load_release_manifest(path)
    assert first is not None and first.build.ordinal == 120
    before = path.stat()
    path.write_text(json.dumps(_manifest(130)), encoding="utf-8")
    after = path.stat()
    assert after.st_size == before.st_size and after.st_ino == before.st_ino
    os.utime(path, ns=(after.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    second = load_release_manifest(path)
    assert second is not None and second.build.ordinal == 130


def test_load_reparses_an_atomic_replace_with_the_same_stamp(tmp_path):
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest(120)), encoding="utf-8")
    first = load_release_manifest(path)
    assert first is not None and first.build.ordinal == 120
    before = path.stat()
    staged = tmp_path / "staged.json"
    staged.write_text(json.dumps(_manifest(130)), encoding="utf-8")
    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(staged, path)
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    second = load_release_manifest(path)
    assert second is not None and second.build.ordinal == 130


def test_load_notices_the_file_disappearing(tmp_path):
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest(100)), encoding="utf-8")
    assert load_release_manifest(path) is not None
    path.unlink()
    assert load_release_manifest(path) is None


# --------------------------------------------------------------------------- #
# 3. 待看说明
# --------------------------------------------------------------------------- #
def _notes(*specs):
    """``(id, ordinal[, level[, audience]])`` -> ReleaseNote (defaults change / all)."""
    out = []
    for nid, ordinal, *rest in specs:
        level, audience = (list(rest) + ["change", "all"][len(rest):])[:2]
        out.append(ReleaseNote(nid, ordinal, level, audience, f"t-{nid}", f"body-{nid}"))
    return out


def _pending(seen, build, notes, is_admin=False):
    return pending_release_notes(seen, build, notes, is_admin=is_admin)


def test_pending_is_exclusive_of_seen_and_inclusive_of_build():
    notes = _notes(("at-seen", 10), ("just-after", 11), ("at-build", 20), ("beyond", 21))
    ids = [n.id for n in _pending(10, 20, notes)]
    assert ids == ["at-build", "just-after"]


def test_pending_hides_admin_notes_from_everyone_else():
    notes = _notes(("pub", 15), ("adm", 16, "change", "admin"))
    assert [n.id for n in _pending(10, 20, notes)] == ["pub"]
    assert [n.id for n in _pending(10, 20, notes, is_admin=True)] == ["adm", "pub"]


def test_pending_is_newest_first_and_ties_break_by_id_ascending():
    notes = _notes(("z", 15), ("a", 15), ("old", 12), ("new", 18))
    ids = [n.id for n in _pending(10, 20, notes)]
    assert ids == ["new", "a", "z", "old"]


def test_pending_is_empty_after_a_rollback():
    notes = _notes(("n1", 10), ("n2", 20))
    # 用户已看到 30,现在跑的是更老的 25:不弹任何东西。
    assert _pending(30, 25, notes) == []


def test_pending_is_empty_when_seen_equals_build():
    assert _pending(20, 20, _notes(("n", 20))) == []


def test_pending_from_zero_baseline_includes_everything_up_to_build():
    notes = _notes(("n1", 1), ("n2", 2))
    assert [n.id for n in _pending(0, 2, notes)] == ["n2", "n1"]


# --------------------------------------------------------------------------- #
# 4. 存储端口(SQLite)
# --------------------------------------------------------------------------- #
def _repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None, event_log_enabled=False, llm_log_enabled=False,
        )
    )


def test_store_baseline_starts_null_and_initialize_only_writes_once(tmp_path):
    ident = _repo(tmp_path)._runtime.identity
    user = ident.current_user()
    assert ident.get_seen_release_ordinal(user.id) is None
    ident.initialize_seen_release_ordinal(user.id, 50)
    assert ident.get_seen_release_ordinal(user.id) == 50
    ident.initialize_seen_release_ordinal(user.id, 999)  # 已有值,不动
    assert ident.get_seen_release_ordinal(user.id) == 50


def test_store_advance_is_a_max_that_never_decreases(tmp_path):
    ident = _repo(tmp_path)._runtime.identity
    uid = ident.current_user().id
    ident.advance_seen_release_ordinal(uid, 40)  # NULL -> 直接写入
    assert ident.get_seen_release_ordinal(uid) == 40
    ident.advance_seen_release_ordinal(uid, 30)
    assert ident.get_seen_release_ordinal(uid) == 40
    ident.advance_seen_release_ordinal(uid, 41)
    assert ident.get_seen_release_ordinal(uid) == 41


def test_store_unknown_user_is_a_no_op(tmp_path):
    ident = _repo(tmp_path)._runtime.identity
    assert ident.get_seen_release_ordinal("ghost") is None
    ident.initialize_seen_release_ordinal("ghost", 1)
    ident.advance_seen_release_ordinal("ghost", 2)
    assert ident.get_seen_release_ordinal("ghost") is None


# --------------------------------------------------------------------------- #
# 5. 接口
# --------------------------------------------------------------------------- #
def _make_client(tmp_path, monkeypatch, auth_optional: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", auth_optional)
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch, "false")


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    """Point the service at a manifest file the test controls."""
    path = tmp_path / "release-manifest.json"
    monkeypatch.setattr(rn, "manifest_path", lambda: path)

    def write(build_ordinal, notes=()):
        path.write_text(
            json.dumps(_manifest(build_ordinal, notes)), encoding="utf-8"
        )

    return path, write


def _login(client: TestClient, username: str = "z00123456") -> dict[str, str]:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    r = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _user_id(client: TestClient, headers) -> str:
    return client.get("/api/me", headers=headers).json()["id"]


def _seen(client: TestClient, user_id: str) -> int | None:
    from app.api import deps
    return deps.identity_repository().get_seen_release_ordinal(user_id)


def test_get_requires_auth(client, manifest_file):
    assert client.get("/api/me/release-notes").status_code == 401
    assert client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 1}
    ).status_code == 401


def test_unavailable_manifest_returns_empty_and_writes_nothing(client, manifest_file):
    headers = _login(client)  # manifest 文件不存在
    body = client.get("/api/me/release-notes", headers=headers).json()
    assert body == {"available": False, "build": None, "notes": [], "more_count": 0}
    assert _seen(client, _user_id(client, headers)) is None
    # 损坏的文件同样不写库。
    path, _write = manifest_file
    path.write_text("{not json", encoding="utf-8")
    body = client.get("/api/me/release-notes", headers=headers).json()
    assert body == {"available": False, "build": None, "notes": [], "more_count": 0}
    assert client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 50}, headers=headers
    ).status_code == 204
    assert _seen(client, _user_id(client, headers)) is None


def test_first_call_initializes_silently_then_next_upgrade_shows_the_new_notes(
    client, manifest_file
):
    _path, write = manifest_file
    write(100, [("old", 90, "很早以前的说明")])
    headers = _login(client)
    uid = _user_id(client, headers)

    first = client.get("/api/me/release-notes", headers=headers).json()
    assert first["available"] is True
    assert first["build"] == {"version": "20260929-abc1234", "ordinal": 100}
    assert first["notes"] == []  # 新用户静默记为已看
    assert _seen(client, uid) == 100

    # 同一版本再取:仍然没有。
    assert client.get("/api/me/release-notes", headers=headers).json()["notes"] == []

    # 升级:新清单带两条新说明(id 同序时按 id 升序)。
    write(
        120,
        [
            ("old", 90, "很早以前的说明"),
            ("b-new", 110, "新说明 B"),
            ("a-new", 110, "新说明 A"),
            ("newest", 120, "最新说明"),
            ("future", 121, "尚未发布"),
        ],
    )
    second = client.get("/api/me/release-notes", headers=headers).json()
    assert second["build"]["ordinal"] == 120
    assert [(n["id"], n["ordinal"], n["body"]) for n in second["notes"]] == [
        ("newest", 120, "最新说明"),
        ("a-new", 110, "新说明 A"),
        ("b-new", 110, "新说明 B"),
    ]
    assert second["more_count"] == 0
    assert second["notes"][0]["title"] == "t-newest"
    assert second["notes"][0]["level"] == "change" and second["notes"][0]["audience"] == "all"
    # GET 本身不推进基线:没确认前每次都还在。
    assert _seen(client, uid) == 100
    assert len(client.get("/api/me/release-notes", headers=headers).json()["notes"]) == 3


def test_post_seen_marks_read_and_is_capped_at_the_build(client, manifest_file):
    _path, write = manifest_file
    write(100)
    headers = _login(client)
    uid = _user_id(client, headers)
    client.get("/api/me/release-notes", headers=headers)  # baseline = 100

    write(120, [("n", 115, "说明")])
    assert len(client.get("/api/me/release-notes", headers=headers).json()["notes"]) == 1
    r = client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 120}, headers=headers
    )
    assert r.status_code == 204 and r.content == b""
    assert _seen(client, uid) == 120
    assert client.get("/api/me/release-notes", headers=headers).json()["notes"] == []

    # 超过当前版本的值被截到 build.ordinal(120),不会写成 10**9。
    write(130, [("m", 125, "更新")])
    r = client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 10**9}, headers=headers
    )
    assert r.status_code == 204
    assert _seen(client, uid) == 130


def test_post_seen_uses_the_ordinal_the_client_was_shown(client, manifest_file):
    """弹窗展示期间服务端又升级了:前端传它看到的 120,不该把 130 的说明标成已看。"""
    _path, write = manifest_file
    write(100)
    headers = _login(client)
    uid = _user_id(client, headers)
    client.get("/api/me/release-notes", headers=headers)
    write(130, [("shown", 115, "已展示"), ("later", 125, "弹窗期间才上线")])
    client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 120}, headers=headers
    )
    assert _seen(client, uid) == 120
    ids = [n["id"] for n in client.get("/api/me/release-notes", headers=headers).json()["notes"]]
    assert ids == ["later"]


def test_post_seen_never_decreases_and_a_rollback_shows_nothing(client, manifest_file):
    _path, write = manifest_file
    write(200, [("n", 150, "x")])
    headers = _login(client)
    uid = _user_id(client, headers)
    client.get("/api/me/release-notes", headers=headers)
    assert _seen(client, uid) == 200

    # 回滚部署:build=180 < seen=200。无待看,标记已看不把基线拉低。
    write(180, [("n", 150, "x"), ("k", 190, "回滚前才有")])
    body = client.get("/api/me/release-notes", headers=headers).json()
    assert body["available"] is True and body["notes"] == []
    client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 180}, headers=headers
    )
    assert _seen(client, uid) == 200

    # 回滚后再升级到 210:190 的说明早于基线 200,不重复弹,只弹 205。
    write(210, [("k", 190, "回滚前才有"), ("p", 205, "新的")])
    ids = [n["id"] for n in client.get("/api/me/release-notes", headers=headers).json()["notes"]]
    assert ids == ["p"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"through_ordinal": -1}, {"through_ordinal": "5"}, {"through_ordinal": True},
     {"through_ordinal": 1.5}, {"through_ordinal": 1, "extra": 1}],
)
def test_post_seen_rejects_bad_bodies(client, manifest_file, payload):
    headers = _login(client)
    r = client.post("/api/me/release-notes/seen", json=payload, headers=headers)
    assert r.status_code == 422


def test_baselines_are_per_account(client, manifest_file):
    _path, write = manifest_file
    write(100)
    alice = _login(client, "z00100001")
    bob = _login(client, "z00100002")
    client.get("/api/me/release-notes", headers=alice)
    client.get("/api/me/release-notes", headers=bob)
    write(120, [("n", 110, "说明")])
    client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 120}, headers=alice
    )
    assert client.get("/api/me/release-notes", headers=alice).json()["notes"] == []
    assert len(client.get("/api/me/release-notes", headers=bob).json()["notes"]) == 1


def test_auth_optional_local_user_gets_a_baseline_too(tmp_path, monkeypatch, manifest_file):
    client = _make_client(tmp_path, monkeypatch, "true")
    _path, write = manifest_file
    write(100)
    first = client.get("/api/me/release-notes").json()
    assert first["available"] is True and first["notes"] == []
    write(110, [("n", 105, "说明")])
    assert [n["id"] for n in client.get("/api/me/release-notes").json()["notes"]] == ["n"]
    assert client.post(
        "/api/me/release-notes/seen", json={"through_ordinal": 110}
    ).status_code == 204
    assert client.get("/api/me/release-notes").json()["notes"] == []


# --------------------------------------------------------------------------- #
# 6. 弹窗分级:重点 / 上限 / 计数 / 受众
# --------------------------------------------------------------------------- #
def _get_notes(client, headers):
    return client.get("/api/me/release-notes", headers=headers).json()


def _baselined(client, manifest_file, notes, build=200, headers=None):
    """Register a user, baseline them at 100, then publish ``notes`` at ``build``."""
    _path, write = manifest_file
    write(100)
    headers = headers or _login(client)
    client.get("/api/me/release-notes", headers=headers)
    write(build, notes)
    return headers


def test_headline_orders_change_before_feature_then_newest_first(client, manifest_file):
    headers = _baselined(client, manifest_file, [
        ("f-new", 190, "", "feature"), ("c-old", 110, "", "change"),
        ("f-old", 120, "", "feature"), ("c-new", 150, "", "change"),
    ])
    body = _get_notes(client, headers)
    assert [n["id"] for n in body["notes"]] == ["c-new", "c-old", "f-new", "f-old"]
    assert body["more_count"] == 0


def test_headline_is_capped_and_overflow_plus_fixes_feed_more_count(client, manifest_file):
    notes = [(f"f{i}", 110 + i, "", "feature") for i in range(7)]  # 7 重点
    notes += [("x1", 150, "", "fix"), ("x2", 151, "", "fix"), ("x3", 152, "", "fix")]
    notes += [("i1", 160, "", "internal"), ("i2", 161, "", "internal")]
    headers = _baselined(client, manifest_file, notes)
    body = _get_notes(client, headers)
    assert [n["id"] for n in body["notes"]] == ["f6", "f5", "f4", "f3", "f2"]
    # 超出 2 条重点 + 3 条修复;后台改进不计。
    assert body["more_count"] == 2 + 3


def test_without_a_headline_nothing_is_shown_but_fixes_wait_to_be_counted(
    client, manifest_file
):
    headers = _baselined(client, manifest_file, [
        ("x", 150, "", "fix"), ("i", 160, "", "internal"),
    ])
    uid = _user_id(client, headers)
    body = _get_notes(client, headers)
    assert body["available"] is True and body["notes"] == [] and body["more_count"] == 1
    assert _seen(client, uid) == 100  # GET 不推进基线,fix 留给下次有重点时一起计数


def test_admin_notes_only_reach_admins(client, manifest_file):
    _path, write = manifest_file
    write(100)
    user = _login(client)
    admin = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]}
    client.get("/api/me/release-notes", headers=user)
    client.get("/api/me/release-notes", headers=admin)
    write(200, [
        ("pub", 150, "", "change", "all"),
        ("adm", 160, "", "feature", "admin"),
        ("adm-fix", 170, "", "fix", "admin"),
    ])
    mine = _get_notes(client, user)
    assert [n["id"] for n in mine["notes"]] == ["pub"] and mine["more_count"] == 0
    theirs = _get_notes(client, admin)
    assert [n["id"] for n in theirs["notes"]] == ["pub", "adm"]
    assert theirs["more_count"] == 1


# --------------------------------------------------------------------------- #
# 7. 更新记录
# --------------------------------------------------------------------------- #
def _history(client, headers=None):
    return client.get("/api/me/release-notes/history", headers=headers)


def test_history_requires_auth(client, manifest_file):
    assert _history(client).status_code == 401


def test_history_unavailable_manifest(client, manifest_file):
    headers = _login(client)
    assert _history(client, headers).json() == {"available": False, "build": None, "notes": []}


def test_history_lists_everything_up_to_the_build_newest_first_without_touching_the_baseline(
    client, manifest_file
):
    _path, write = manifest_file
    write(200, [
        ("a", 100, "", "internal"), ("b", 150, "正文", "fix"), ("c", 150, "", "feature"),
        ("adm", 160, "", "change", "admin"), ("future", 201, "", "change"),
    ])
    headers = _login(client)
    uid = _user_id(client, headers)
    body = _history(client, headers).json()
    assert body["available"] is True
    assert body["build"] == {"version": "20260929-abc1234", "ordinal": 200}
    # 含 internal;admin 说明与超出当前版本的都不给普通用户;同序按 id 升序。
    assert [n["id"] for n in body["notes"]] == ["b", "c", "a"]
    assert body["notes"][0] == {
        "id": "b", "ordinal": 150, "level": "fix", "audience": "all",
        "title": "t-b", "body": "正文",
    }
    assert _seen(client, uid) is None  # 既不读也不写基线

    admin = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]}
    assert [n["id"] for n in _history(client, admin).json()["notes"]] == ["adm", "b", "c", "a"]


# --------------------------------------------------------------------------- #
# 8. 真实目录守卫:每条说明都能被清单生成脚本解析,标题/正文不得超长
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[2]
TITLE_MAX = 30  # code points
BODY_MAX = 160  # code points, after strip


def _load_manifest_script():
    spec = importlib.util.spec_from_file_location(
        "build_release_manifest_for_guard", _ROOT / "scripts" / "build_release_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _real_note_files() -> list[Path]:
    return sorted(
        p for p in (_ROOT / "release-notes").glob("*.md") if p.name != "README.md"
    )


def test_every_real_release_note_parses_and_stays_short():
    gen = _load_manifest_script()
    problems: list[str] = []
    for path in _real_note_files():
        rel = f"release-notes/{path.name}"
        try:
            parsed = gen.parse_note_text(path.read_text(encoding="utf-8"), rel)
        except gen.ManifestError as exc:
            problems.append(str(exc))
            continue
        if len(parsed["title"]) > TITLE_MAX:
            problems.append(f"{rel}: 标题 {len(parsed['title'])} 字,超过 {TITLE_MAX}")
        if len(parsed["body"]) > BODY_MAX:
            problems.append(f"{rel}: 正文 {len(parsed['body'])} 字,超过 {BODY_MAX}")
    assert not problems, "\n".join(problems)
