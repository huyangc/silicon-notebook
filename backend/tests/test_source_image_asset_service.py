# backend/tests/test_source_image_asset_service.py
import pytest
from app.services.knowhow.assets import AssetService, AssetValidationError


class _FakeRepo:
    def __init__(self, tmp_path):
        self.storage_dir = tmp_path
        self._rows, self._seq = {}, 0
        self.get_calls = 0

    def insert_notebook_asset(self, notebook_id, filename, mime, size, created_by, source_id=None):
        self._seq += 1
        aid = f"asset-{self._seq}"
        self._rows[aid] = {"id": aid, "notebook_id": notebook_id, "filename": filename,
                           "mime": mime, "size": size, "created_by": created_by, "source_id": source_id}
        return aid

    def get_notebook_asset(self, aid):
        self.get_calls += 1
        return self._rows.get(aid)

    def source_asset_ids(self, source_id):
        return [a["id"] for a in self._rows.values() if a["source_id"] == source_id]

    def delete_source_asset_rows(self, source_id):
        ids = self.source_asset_ids(source_id)
        rows = [self._rows[i] for i in ids]
        for i in ids:
            self._rows.pop(i, None)
        return rows


def test_save_source_image_writes_disk_and_row(tmp_path):
    svc = AssetService(_FakeRepo(tmp_path))
    asset = svc.save_source_image("nb-1", "src-1", "images/fig.png", "image/png", b"\x89PNG..", "u")
    path = svc.path_for(asset)
    assert path.is_file() and path.read_bytes() == b"\x89PNG.."
    assert asset["source_id"] == "src-1"


def test_delete_source_images_unlinks(tmp_path):
    repo = _FakeRepo(tmp_path)
    svc = AssetService(repo)
    asset = svc.save_source_image("nb-1", "src-1", "fig.png", "image/png", b"x", "u")
    path = svc.path_for(asset)
    assert path.is_file()
    get_calls_before_delete = repo.get_calls
    svc.delete_source_images("src-1")
    assert not path.exists()
    assert repo.source_asset_ids("src-1") == []
    assert repo.get_calls == get_calls_before_delete


def test_validate_asset_honors_caller_ceiling_above_default():
    """codex R6 P2: MINERU_MAX_IMAGE_BYTES 配置超过 10MB 时不得被默认粘贴图
    上限静默压住——validate_asset 的 max_bytes 覆盖默认闸。"""
    from app.services.knowhow.assets import validate_asset

    eleven_mb = 11 * 1024 * 1024
    validate_asset("image/png", eleven_mb, max_bytes=20 * 1024 * 1024)
    with pytest.raises(AssetValidationError):
        validate_asset("image/png", eleven_mb)  # 默认闸仍是 10MB
    with pytest.raises(AssetValidationError):
        validate_asset("image/png", eleven_mb, max_bytes=5 * 1024 * 1024)


def test_save_source_image_accepts_above_default_with_ceiling(tmp_path):
    """穿过真实 save_source_image 的接线: 11MB 图 + max_bytes=20MB 必须落盘,
    不得被默认 10MB 粘贴图上限拦下。"""
    svc = AssetService(_FakeRepo(tmp_path))
    big = b"\x89PNG" + b"\x00" * (11 * 1024 * 1024)
    asset = svc.save_source_image(
        "nb-1", "src-1", "big.png", "image/png", big, "u",
        max_bytes=20 * 1024 * 1024,
    )
    assert svc.path_for(asset).is_file()


def test_persist_closure_passes_configured_ceiling_to_asset_service():
    """persist_image 闭包必须把部署的来源图片上限传给 save_source_image。"""
    from app.core.config import Settings
    from app.services.source_image_persist import make_persist_image_factory

    captured = {}

    class _Svc:
        def save_source_image(self, notebook_id, source_id, filename, mime,
                              data, created_by, max_bytes=None):
            captured["max_bytes"] = max_bytes
            return {"id": "asset-1"}

    settings = Settings(mineru_max_image_bytes=20 * 1024 * 1024)
    factory = make_persist_image_factory(settings, lambda: _Svc())
    persist = factory("nb-1", "src-1", "u")
    assert persist(b"\x89PNG..", "fig.png") == "asset-1"
    assert captured["max_bytes"] == 20 * 1024 * 1024


def test_save_source_image_rejects_bad_mime(tmp_path):
    svc = AssetService(_FakeRepo(tmp_path))
    with pytest.raises(AssetValidationError):
        svc.save_source_image("nb-1", "src-1", "fig.svg", "image/svg+xml", b"x", "u")
