# backend/tests/test_notebook_assets.py
"""knowhow-tables PR-1 Task 4: notebook image-asset upload + authed serving.

Mirrors the app/login fixture pattern from test_notebook_share_readonly.py
(HTTP-level: register/login via TestClient, share owner's `repo` fixture for
direct `add_member` since there is no HTTP "add member by id" endpoint).
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


# Not a real decodable PNG — the service only validates declared mime + size,
# it never sniffs/decodes image bytes — but it is deterministic and distinct
# from other fixtures so a byte-identical roundtrip check is meaningful.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"knowhow-asset-fixture-bytes" * 10


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _mk_notebook(client, headers, name="N"):
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _upload(client, headers, nb, *, filename="a.png", content=PNG_BYTES, content_type="image/png"):
    return client.post(
        f"/api/notebooks/{nb}/assets",
        headers=headers,
        files={"file": (filename, content, content_type)},
    )


def test_upload_then_get_roundtrip_byte_identical(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000301")
    nb = _mk_notebook(client, owner_h)

    resp = _upload(client, owner_h, nb)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"id", "url"}
    assert body["url"] == f"/api/notebooks/{nb}/assets/{body['id']}"

    got = client.get(body["url"], headers=owner_h)
    assert got.status_code == 200
    assert got.content == PNG_BYTES
    assert got.headers["content-type"] == "image/png"
    assert got.headers["cache-control"] == "private, max-age=86400"


def test_upload_too_large_returns_friendly_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000302")
    nb = _mk_notebook(client, owner_h)

    huge = b"x" * (10 * 1024 * 1024 + 1)
    resp = _upload(client, owner_h, nb, filename="big.png", content=huge)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "10MB" in detail or "过大" in detail
    assert any("一" <= ch <= "鿿" for ch in detail)  # friendly Chinese copy, not a raw exception dump


def test_upload_unsupported_mime_returns_friendly_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000303")
    nb = _mk_notebook(client, owner_h)

    resp = _upload(
        client, owner_h, nb, filename="doc.pdf", content=b"%PDF-1.4", content_type="application/pdf"
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "不支持" in detail
    assert any("一" <= ch <= "鿿" for ch in detail)


def test_readonly_member_can_get_cannot_post(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000304")
    nb = _mk_notebook(client, owner_h)
    asset = _upload(client, owner_h, nb).json()

    bob_h = _login(client, "b00000305")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)

    assert client.get(asset["url"], headers=bob_h).status_code == 200
    # Non-owner write attempt: 404 (not 403) — this codebase's established
    # "don't leak existence" convention for require_notebook_write.
    assert _upload(client, bob_h, nb).status_code == 404


def test_stranger_gets_404_for_read_and_write(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000306")
    nb = _mk_notebook(client, owner_h)
    asset = _upload(client, owner_h, nb).json()

    stranger_h = _login(client, "c00000307")
    assert client.get(asset["url"], headers=stranger_h).status_code == 404
    assert _upload(client, stranger_h, nb).status_code == 404


def test_cross_notebook_asset_id_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000308")
    nb1 = _mk_notebook(client, owner_h, name="N1")
    nb2 = _mk_notebook(client, owner_h, name="N2")
    asset = _upload(client, owner_h, nb1).json()

    # Owner has full read/write access to nb2 itself — but the asset belongs
    # to nb1, so requesting it through nb2's path must still 404.
    cross = client.get(f"/api/notebooks/{nb2}/assets/{asset['id']}", headers=owner_h)
    assert cross.status_code == 404
