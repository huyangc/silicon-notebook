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


# ---------------------------------------------------------------------------
# codex #659 R6 P1: the notebook's finalize sweep can run BETWEEN the
# capability guard and the actual disk write — save()/save_source_image()
# must recheck liveness after writing and self-compensate.
# ---------------------------------------------------------------------------


def test_save_unlinks_the_file_when_the_notebook_is_gone_by_the_time_it_writes(
    tmp_path,
):
    """变异钉:去掉写后复核 → 文件和目录都原样留着,本条报红。"""
    repo = _FakeRepo(tmp_path)
    svc = AssetService(repo, notebook_exists=lambda nb: False)
    with pytest.raises(KeyError):
        svc.save("nb-1", "pic.png", "image/png", b"\x89PNG..", "u")

    notebook_dir = tmp_path / "assets" / "nb-1"
    assert not notebook_dir.exists(), "补偿删除应当连带清空空目录"


def test_save_source_image_unlinks_the_file_when_the_notebook_is_gone(tmp_path):
    """同上,save_source_image 那条路径。"""
    repo = _FakeRepo(tmp_path)
    svc = AssetService(repo, notebook_exists=lambda nb: False)
    with pytest.raises(KeyError):
        svc.save_source_image("nb-1", "src-1", "fig.png", "image/png", b"x", "u")

    notebook_dir = tmp_path / "assets" / "nb-1"
    assert not notebook_dir.exists()


def test_save_keeps_the_file_when_the_notebook_recheck_passes(tmp_path):
    """复核过 = finalize 还没跑,文件正常留着(后续相位 4/终局清扫或用户下次
    正常访问都还会看到它)——这次修复不能把一次健康的上传也删了。"""
    repo = _FakeRepo(tmp_path)
    svc = AssetService(repo, notebook_exists=lambda nb: True)
    asset = svc.save("nb-1", "pic.png", "image/png", b"\x89PNG..", "u")
    assert svc.path_for(asset).is_file()


def test_save_defaults_to_probing_repo_get_notebook(tmp_path):
    """无显式注入时,默认探测 repo.get_notebook——真实 facade 的既有 seam,不
    需要调用方每次手动接线。"""

    class _RepoWithGetNotebook(_FakeRepo):
        def __init__(self, tmp_path, *, exists: bool):
            super().__init__(tmp_path)
            self._exists = exists

        def get_notebook(self, notebook_id):
            if not self._exists:
                raise KeyError(notebook_id)
            return {"id": notebook_id}

    gone = _RepoWithGetNotebook(tmp_path, exists=False)
    svc = AssetService(gone)
    with pytest.raises(KeyError):
        svc.save("nb-1", "pic.png", "image/png", b"x", "u")
    assert not (tmp_path / "assets" / "nb-1").exists()

    alive = _RepoWithGetNotebook(tmp_path, exists=True)
    svc = AssetService(alive)
    asset = svc.save("nb-2", "pic.png", "image/png", b"x", "u")
    assert svc.path_for(asset).is_file()


def test_persist_image_factory_degrades_instead_of_raising_on_keyerror():
    """persist_image 闭包(MinerU 抽图管线)必须把这个 KeyError 当成"这张图没存上"
    优雅降级,不能让整个来源解析被一张图片的笔记本生命周期问题打断。"""
    from app.core.config import Settings
    from app.services.source_image_persist import make_persist_image_factory

    class _Svc:
        def save_source_image(self, *a, **k):
            raise KeyError("nb-1")

    settings = Settings(mineru_max_image_bytes=20 * 1024 * 1024)
    factory = make_persist_image_factory(settings, lambda: _Svc())
    persist = factory("nb-1", "src-1", "u")
    assert persist(b"\x89PNG..", "fig.png") is None
