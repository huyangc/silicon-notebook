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


@pytest.fixture
def local_trusted_repo(tmp_path, monkeypatch):
    """local_repo + 部署级受信代理白名单（URL_IMPORT_TRUSTED_PROXY_HOSTS）。"""
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://localhost:8888")
    monkeypatch.setenv("URL_IMPORT_TRUSTED_PROXY_HOSTS", "http://127.0.0.1:8100")
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


# --- 受信插件代理 SSRF 豁免（URL_IMPORT_TRUSTED_PROXY_HOSTS）--------------


def test_add_url_sources_exempts_only_whitelisted_origins(cloud_repo, monkeypatch):
    """probe 只对 origin 精确命中的 URL 收 allow_private=True；未列名恒 False。"""
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    seen = []

    def spy_probe(url, **kw):
        seen.append((url, kw.get("allow_private")))
        return PdfProbe(True, "", 10, "doc.pdf")

    monkeypatch.setattr(remote_sources, "probe_pdf", spy_probe)
    cloud_repo.add_url_sources(
        nb.id,
        [
            "http://127.0.0.1:8100/export/a.pdf",
            "http://127.0.0.1:8200/export/b.pdf",  # 不同端口 = 不同 origin
            "https://pub.example/c.pdf",
        ],
        scheduler=lambda sid: None,
        trusted_proxy_origins=frozenset({"http://127.0.0.1:8100"}),
    )
    assert seen == [
        ("http://127.0.0.1:8100/export/a.pdf", True),
        ("http://127.0.0.1:8200/export/b.pdf", False),
        ("https://pub.example/c.pdf", False),
    ]


def test_add_url_sources_without_whitelist_never_exempts(cloud_repo, monkeypatch):
    """浏览器/MCP 路径不传白名单 → allow_private 恒 False（历史行为逐位不变）。"""
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    seen = []

    def spy_probe(url, **kw):
        seen.append(kw.get("allow_private"))
        return PdfProbe(True, "", 10, "doc.pdf")

    monkeypatch.setattr(remote_sources, "probe_pdf", spy_probe)
    cloud_repo.add_url_sources(
        nb.id, ["http://127.0.0.1:8100/a.pdf"], scheduler=lambda sid: None
    )
    assert seen == [False]


def test_origin_normalization_and_whitelist_parsing():
    from app.services.source_ingestion import _origin_of, trusted_proxy_origin_set

    # 大小写统一 + 默认端口显式归一；path 不参与。
    assert _origin_of("http://Proxy.Internal/a/b.pdf") == "http://proxy.internal:80"
    assert _origin_of("HTTPS://Proxy.Internal") == "https://proxy.internal:443"
    assert _origin_of("http://proxy.internal:8100/x?y=1") == "http://proxy.internal:8100"
    assert _origin_of("http://proxy.internal:80/x") == _origin_of("http://proxy.internal/y")
    # 不同端口 = 不同 origin。
    assert _origin_of("http://h:8100") != _origin_of("http://h:8200")
    # 非 http(s)/缺 host/端口无效/畸形串 → 空串（永不在任何白名单里）。
    assert _origin_of("ftp://proxy.internal/a.pdf") == ""
    assert _origin_of("http://") == ""
    assert _origin_of("http://h:99999/a.pdf") == ""
    assert _origin_of("http://[::1") == ""
    assert _origin_of("") == ""
    # 名单解析：逐项归一入集合，空段/畸形段丢弃；空串 → 空集合。
    assert trusted_proxy_origin_set("") == frozenset()
    assert trusted_proxy_origin_set(
        " HTTP://127.0.0.1:8100 , ,ftp://x, https://Proxy.Internal/path-ignored "
    ) == frozenset({"http://127.0.0.1:8100", "https://proxy.internal:443"})


#: 两份 `_origin_of` 副本一致性钉的输入表：覆盖实现的每条分支（scheme 大小写与
#: 非 http(s)、缺 host、http/https 默认端口两向、显式端口、越界端口、非数字端口、
#: IPv6 字面量带/不带端口、无效 IPv6 括号、userinfo、path/query、空白与空串），
#: 让任一侧将来单方面加一条归一规则时至少有一个采样落进新分支而立刻红。
_ORIGIN_PARITY_CASES = [
    "http://Proxy.Internal/a/b.pdf",
    "HTTPS://Proxy.Internal",
    "https://proxy.internal:443/x",
    "http://proxy.internal:80/x",
    "http://proxy.internal:8100/x?y=1#frag",
    "ftp://proxy.internal/a.pdf",
    "file:///etc/passwd",
    "http://",
    "http://h:99999/a.pdf",
    "http://h:0x50/a.pdf",
    "http://[::1]/a.pdf",
    "http://[::1]:8100/a.pdf",
    "http://[::1",
    "http://user:pw@proxy.internal:8100/a.pdf",
    "   ",
    "",
    "not a url",
    "//proxy.internal/a.pdf",
    "http:proxy.internal",
]


def test_parser_chain_origin_copy_matches_source_ingestion():
    """parser_chain_execution._origin_of 是防 import 环的独立副本，必须逐位一致。"""
    from app.services import parser_chain_execution, source_ingestion

    for url in _ORIGIN_PARITY_CASES:
        assert parser_chain_execution._origin_of(url) == source_ingestion._origin_of(
            url
        ), url


def test_process_source_download_carries_trusted_proxy_exemption(
    local_trusted_repo, monkeypatch
):
    """特性 B：process_source 的解析下载按部署白名单命中传 allow_private=True，
    否则 probe 已豁免、来源已建，下载仍被拒 → 「已创建但解析失败」。"""
    repo = local_trusted_repo
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    res = repo.add_url_sources(
        nb.id, ["http://127.0.0.1:8100/export/doc.pdf"], scheduler=lambda sid: None
    )
    downloads = []

    def spy_download(url, dest, **kw):
        downloads.append((url, kw.get("allow_private")))
        Path(dest).write_bytes(b"%PDF-")

    monkeypatch.setattr(remote_sources, "download_pdf", spy_download)
    monkeypatch.setattr(
        repo.mineru_client, "parse_with_images",
        lambda path, name: ([{"type": "text", "text": "Proxied", "page_idx": 0}], {}),
    )
    repo.process_source(res.created[0].id)
    assert downloads == [("http://127.0.0.1:8100/export/doc.pdf", True)]
    assert repo.get_source(res.created[0].id).parse_status == "extracted"


def test_process_source_download_defaults_to_no_exemption(local_repo, monkeypatch):
    """未配置白名单（默认部署）时，解析下载收到 allow_private=False。"""
    nb = local_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(local_repo, monkeypatch, nb.id)
    downloads = []

    def spy_download(url, dest, **kw):
        downloads.append((url, kw.get("allow_private")))
        Path(dest).write_bytes(b"%PDF-")

    monkeypatch.setattr(remote_sources, "download_pdf", spy_download)
    monkeypatch.setattr(
        local_repo.mineru_client, "parse_with_images",
        lambda path, name: ([{"type": "text", "text": "Public", "page_idx": 0}], {}),
    )
    local_repo.process_source(sid)
    assert downloads == [("https://a/doc.pdf", False)]
