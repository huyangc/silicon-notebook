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


def test_save_source_image_rejects_bad_mime(tmp_path):
    svc = AssetService(_FakeRepo(tmp_path))
    with pytest.raises(AssetValidationError):
        svc.save_source_image("nb-1", "src-1", "fig.svg", "image/svg+xml", b"x", "u")
