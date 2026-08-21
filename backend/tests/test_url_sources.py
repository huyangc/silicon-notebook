from pathlib import Path

import pytest
from app.core.config import Settings
from app.extensions import default_extension_runtime
from app.models.schemas import NotebookCreate
from app.models.sources import ScopedSourceDetail, SourceElement
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.sqlite_repository import SQLiteRepository


def _repository(settings: Settings) -> SQLiteRepository:
    return SQLiteRepository(
        settings,
        parser_provider_chain_host=default_extension_runtime().parser_chain,
    )


def _base_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")


@pytest.fixture
def cloud_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-test")
    monkeypatch.setenv("MINERU_MODE", "off")  # 仅云端：本地未配置，URL 走 mineru.net
    return _repository(Settings())


@pytest.fixture
def notoken_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    monkeypatch.setenv("MINERU_MODE", "off")
    return _repository(Settings())


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    """本地 MinerU(http) 已配置、云端 token 缺失：URL 来源应走本地。"""
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://localhost:8888")
    return _repository(Settings())


def test_add_url_sources_creates_and_rejects(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))

    def fake_probe(url, **kw):
        if url.endswith(".pdf"):
            return PdfProbe(True, "", 123, "doc.pdf")
        return PdfProbe(False, "URL 不是 PDF（Content-Type=text/html）", 0, "x.pdf")

    monkeypatch.setattr(remote_sources, "probe_pdf", fake_probe)
    scheduled = []
    result = cloud_repo.add_url_sources(
        nb.id, ["https://a/doc.pdf", "https://b/page.html"],
        scheduler=lambda sid: scheduled.append(sid),
    )
    assert len(result.created) == 1
    assert result.created[0].source_url == "https://a/doc.pdf"
    assert result.created[0].parse_status == "queued"
    assert result.created[0].type == "pdf"
    assert len(result.rejected) == 1
    assert "不是 PDF" in result.rejected[0].reason
    assert scheduled == [result.created[0].id]


def test_add_url_sources_requires_token(notoken_repo):
    nb = notoken_repo.create_notebook(NotebookCreate(name="n"))
    with pytest.raises(MinerUCloudNotConfigured):
        notoken_repo.add_url_sources(nb.id, ["https://a/doc.pdf"])


def test_add_url_sources_unknown_notebook_raises_keyerror(cloud_repo):
    with pytest.raises(KeyError):
        cloud_repo.add_url_sources("nb-missing", ["https://a/doc.pdf"])


def _make_url_source(repo, monkeypatch, nb_id):
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    res = repo.add_url_sources(nb_id, ["https://a/doc.pdf"], scheduler=lambda sid: None)
    return res.created[0].id


def test_process_source_url_branch_parses_via_cloud(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(cloud_repo, monkeypatch, nb.id)
    monkeypatch.setattr(
        cloud_repo.mineru_cloud_client, "parse_url_with_images",
        lambda url, **kw: ([{"type": "text", "text": "Hello world", "page_idx": 0}], {}),
    )
    cloud_repo.process_source(sid)
    detail = cloud_repo.get_source(sid)
    assert detail.parse_status == "extracted"
    assert any("Hello world" in e.text for e in cloud_repo.source_elements(sid))


def test_add_url_sources_allows_local_only(local_repo, monkeypatch):
    nb = local_repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    res = local_repo.add_url_sources(nb.id, ["https://a/doc.pdf"], scheduler=lambda sid: None)
    assert len(res.created) == 1


def test_process_source_url_prefers_local_over_cloud(local_repo, monkeypatch):
    nb = local_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(local_repo, monkeypatch, nb.id)

    downloaded = []
    monkeypatch.setattr(
        remote_sources, "download_pdf",
        lambda url, dest, **kw: (downloaded.append(url), open(dest, "wb").write(b"%PDF-"))[0],
    )
    monkeypatch.setattr(
        local_repo.mineru_client, "parse_with_images",
        lambda path, name: ([{"type": "text", "text": "Local parsed", "page_idx": 0}], {}),
    )

    def cloud_must_not_run(url, **kw):
        raise AssertionError("本地优先时不得触达云端 mineru.net")

    monkeypatch.setattr(
        local_repo.mineru_cloud_client, "parse_url_with_images", cloud_must_not_run
    )

    local_repo.process_source(sid)
    detail = local_repo.get_source(sid)
    assert detail.parse_status == "extracted"
    assert downloaded == ["https://a/doc.pdf"]
    assert any("Local parsed" in e.text for e in local_repo.source_elements(sid))


def test_process_source_url_cloud_failure_uses_python_fallback_and_can_reparse(
    cloud_repo, monkeypatch
):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(cloud_repo, monkeypatch, nb.id)

    def boom(url, **kw):
        raise RuntimeError("MinerU 云端解析失败: 超过页数")

    monkeypatch.setattr(cloud_repo.mineru_cloud_client, "parse_url_with_images", boom)
    fallback_calls = []

    def local_python_fallback(source_id, path, file_name, persist_image=None):
        fallback_calls.append((source_id, "https://a/doc.pdf", file_name))
        return [
            SourceElement(
                id="",
                source_id=source_id,
                element_type="paragraph",
                location_label="PDF p.1 paragraph 1",
                text="Locally recovered text",
                metadata={"parser": "pymupdf4llm", "page_number": 1},
            )
        ]

    monkeypatch.setattr(
        remote_sources,
        "download_pdf",
        lambda url, dest, **kw: Path(dest).write_bytes(b"%PDF-"),
    )
    import app.services.parser_chain_execution as parser_execution
    monkeypatch.setattr(
        parser_execution, "parse_builtin_source_file", local_python_fallback
    )
    cloud_repo.process_source(sid)
    detail = cloud_repo.get_source(sid)
    assert detail.parse_status == "extracted"
    assert detail.parse_quality_warning is True
    assert cloud_repo.list_sources(nb.id)[0].parse_quality_warning is True
    assert detail.error_message.startswith("[pdf-python-fallback]")
    assert "超过页数" in detail.error_message
    assert fallback_calls == [(sid, "https://a/doc.pdf", "doc.pdf")]
    assert ScopedSourceDetail.of(detail).parse_failed is False
    assert any(
        element.text == "Locally recovered text"
        for element in cloud_repo.source_elements(sid)
    )

    # The existing reparse action runs the same pipeline. Once MinerU is back,
    # a successful run clears both the persisted diagnostic and safe warning.
    monkeypatch.setattr(
        cloud_repo.mineru_cloud_client,
        "parse_url_with_images",
        lambda url, **kw: ([{"type": "text", "text": "MinerU restored"}], {}),
    )
    cloud_repo.process_source(sid)
    reparsed = cloud_repo.get_source(sid)
    assert reparsed.parse_status == "extracted"
    assert reparsed.parse_quality_warning is False
    assert cloud_repo.list_sources(nb.id)[0].parse_quality_warning is False
    assert reparsed.error_message == ""


def test_process_source_url_cloud_failure_empty_fallback_keeps_quality_warning(
    cloud_repo, monkeypatch
):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(cloud_repo, monkeypatch, nb.id)
    monkeypatch.setattr(
        cloud_repo.mineru_cloud_client,
        "parse_url_with_images",
        lambda url, **kw: (_ for _ in ()).throw(RuntimeError("cloud unavailable")),
    )
    monkeypatch.setattr(
        remote_sources,
        "download_pdf",
        lambda url, dest, **kw: Path(dest).write_bytes(b"%PDF-"),
    )
    import app.services.parser_chain_execution as parser_execution
    monkeypatch.setattr(
        parser_execution, "parse_builtin_source_file", lambda *args, **kwargs: []
    )

    cloud_repo.process_source(sid)

    detail = cloud_repo.get_source(sid)
    assert detail.parse_status == "extracted"
    assert detail.parse_quality_warning is True
    assert detail.error_message.startswith("[pdf-python-fallback]")
    assert "No extractable text" in detail.error_message
    assert ScopedSourceDetail.of(detail).parse_failed is False


def test_url_scheduler_sees_committed_queued_row(cloud_repo, monkeypatch):
    """Task 12: the queued URL source row is COMMITTED before the scheduler
    callback fires — a background job must never race an uncommitted row."""
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    seen = []

    def scheduler(sid):
        with cloud_repo._connect() as db:
            row = db.execute(
                "SELECT parse_status FROM sources WHERE id=?", (sid,)
            ).fetchone()
        seen.append((sid, row["parse_status"] if row else None))

    res = cloud_repo.add_url_sources(nb.id, ["https://a/doc.pdf"], scheduler=scheduler)
    assert seen == [(res.created[0].id, "queued")]
