from pathlib import Path

import pytest

from app.domain.extensions import PARSER_CLOUD_PROVIDER, ParsedSource
from app.extensions import default_extension_runtime
from app.models.sources import SourceElement
from app.services import parser_chain_execution as execution_module
from app.services.parser_chain_execution import (
    PARSER_FALLBACK_WARNING_CODE,
    ParserChainExecution,
)


class _Connection:
    def __init__(self, held: bool = False):
        self.held = held

    def is_connection_held(self) -> bool:
        return self.held


class _Client:
    def __init__(self, *, configured=False, mode="off", result=None, error=None):
        self.configured = configured
        self.mode = mode
        self.result = result or ([{"type": "text", "text": "remote"}], {})
        self.error = error
        self.last_error = ""
        self.file_calls = 0
        self.url_calls = 0

    def parse_with_images(self, path, name):
        self.file_calls += 1
        if self.error:
            raise self.error
        return self.result

    def parse_file_with_images(self, path, *, data_id=""):
        self.file_calls += 1
        if self.error:
            raise self.error
        return self.result

    def parse_url_with_images(self, url, *, data_id=""):
        self.url_calls += 1
        if self.error:
            raise self.error
        return self.result


def _run(
    *,
    tmp_path: Path,
    source_kind="file",
    file_name="doc.pdf",
    local=None,
    cloud=None,
    connection=None,
    persist=None,
    delete=None,
) -> ParsedSource:
    path = tmp_path / (file_name or "doc.pdf")
    path.write_bytes(b"%PDF-1.4")
    return ParserChainExecution(
        host=default_extension_runtime().parser_chain,
        source_id="source-1",
        source_kind=source_kind,
        file_path=str(path),
        file_name=file_name,
        source_url="https://private.example/document" if source_kind == "url" else "",
        mineru_client=local or _Client(),
        cloud_client=cloud or _Client(),
        connection=connection or _Connection(),
        make_persist_image=lambda: persist,
        delete_source_images=delete or (lambda: None),
        event_sink=lambda _event: None,
    ).run()


def test_self_hosted_failure_never_opens_public_cloud_and_reuses_one_download(
    tmp_path, monkeypatch
):
    local = _Client(configured=True, mode="http", error=RuntimeError("local down"))
    cloud = _Client(configured=True)
    downloads = []
    monkeypatch.setattr(
        execution_module.remote_sources,
        "download_pdf",
        lambda url, dest, **kwargs: (
            downloads.append(url), Path(dest).write_bytes(b"%PDF-1.4")
        )[-1],
    )
    builtin_names = []
    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda source_id, path, file_name, persist_image=None: builtin_names.append(
            file_name
        ) or [
            SourceElement(
                id="",
                source_id=source_id,
                element_type="paragraph",
                location_label="PDF p.1",
                text="builtin",
                metadata={"parser": "pypdf"},
            )
        ],
    )

    result = _run(
        tmp_path=tmp_path,
        source_kind="url",
        file_name="document",
        local=local,
        cloud=cloud,
    )

    assert [item.text for item in result.elements] == ["builtin"]
    assert result.warning_code == PARSER_FALLBACK_WARNING_CODE
    assert local.file_calls == 1
    assert cloud.url_calls == 0
    assert downloads == ["https://private.example/document"]
    assert builtin_names == ["document.pdf"]


def test_extensionless_pdf_url_uses_cloud_without_downloading(tmp_path, monkeypatch):
    cloud = _Client(configured=True)
    downloads = []
    monkeypatch.setattr(
        execution_module.remote_sources,
        "download_pdf",
        lambda *args, **kwargs: downloads.append(args[0]),
    )

    result = _run(
        tmp_path=tmp_path,
        source_kind="url",
        file_name="document",
        cloud=cloud,
    )

    assert [item.text for item in result.elements] == ["remote"]
    assert cloud.url_calls == 1
    assert downloads == []


def test_extensionless_pdf_url_cloud_failure_uses_pdf_builtin_once(
    tmp_path, monkeypatch
):
    cloud = _Client(configured=True, error=RuntimeError("cloud down"))
    downloads = []
    builtin_names = []
    monkeypatch.setattr(
        execution_module.remote_sources,
        "download_pdf",
        lambda url, dest, **kwargs: (
            downloads.append(url), Path(dest).write_bytes(b"%PDF-1.4")
        )[-1],
    )
    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda source_id, path, file_name, persist_image=None: builtin_names.append(
            file_name
        ) or [],
    )

    result = _run(
        tmp_path=tmp_path,
        source_kind="url",
        file_name="document",
        cloud=cloud,
    )

    assert result.elements == ()
    assert cloud.url_calls == 1
    assert downloads == ["https://private.example/document"]
    assert builtin_names == ["document.pdf"]


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "raised"])
def test_each_provider_probe_performs_at_most_one_external_parse(
    tmp_path, outcome
):
    if outcome == "accepted":
        client = _Client(configured=True)
    elif outcome == "rejected":
        client = _Client(configured=True, result=([], {}))
    else:
        client = _Client(configured=True, error=RuntimeError("provider failed"))
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    execution = ParserChainExecution(
        host=default_extension_runtime().parser_chain,
        source_id="source-1",
        source_kind="file",
        file_path=str(path),
        file_name="doc.pdf",
        source_url="",
        mineru_client=client,
        cloud_client=_Client(),
        connection=_Connection(),
        make_persist_image=lambda: None,
        delete_source_images=lambda: None,
        event_sink=lambda _event: None,
    )

    if outcome == "raised":
        with pytest.raises(RuntimeError, match="provider failed"):
            execution.probe("parser.mineru_self_hosted")
    first_or_cached = execution.probe("parser.mineru_self_hosted")
    again = execution.probe("parser.mineru_self_hosted")

    assert client.file_calls == 1
    assert again is first_or_cached


def test_provider_io_is_blocked_while_a_database_connection_is_held(
    tmp_path, monkeypatch
):
    local = _Client(configured=True)
    builtin_calls = []
    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda *args, **kwargs: builtin_calls.append(1) or [],
    )

    with pytest.raises(RuntimeError, match="parser provider chain exhausted"):
        _run(
            tmp_path=tmp_path,
            local=local,
            connection=_Connection(True),
        )
    assert local.file_calls == 0
    assert builtin_calls == []


def test_raising_builtin_is_a_failed_chain_but_a_real_empty_result_is_valid(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad file")),
    )
    with pytest.raises(RuntimeError, match="parser provider chain exhausted"):
        _run(tmp_path=tmp_path)

    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda *args, **kwargs: [],
    )
    assert _run(tmp_path=tmp_path).elements == ()


def test_plugin_gets_only_opaque_identity_and_client_payload_is_frozen(
    tmp_path
):
    raw = [{"type": "text", "text": "trusted"}]
    cloud = _Client(configured=True, result=(raw, {}))
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    execution = ParserChainExecution(
        host=default_extension_runtime().parser_chain,
        source_id="source-1",
        source_kind="file",
        file_path=str(path),
        file_name="doc.pdf",
        source_url="",
        mineru_client=_Client(),
        cloud_client=cloud,
        connection=_Connection(),
        make_persist_image=lambda: None,
        delete_source_images=lambda: None,
        event_sink=lambda _event: None,
    )

    proposal = execution.probe(PARSER_CLOUD_PROVIDER)
    assert proposal.accepted is True
    assert not hasattr(proposal.value, "content_list")
    raw[0]["text"] = "plugin-mutated"
    assert execution.admit(PARSER_CLOUD_PROVIDER, proposal.value).accepted is True
    result = execution.materialize(PARSER_CLOUD_PROVIDER, proposal.value)

    assert [item.text for item in result.elements] == ["trusted"]


def test_plugin_access_exposes_only_the_narrow_probe(tmp_path, monkeypatch):
    from app.extensions.builtin import parser as builtin_parser

    observed = []

    def hostile_probe(context):
        access = context.access
        observed.append(access)
        assert not hasattr(access, "__dict__")
        for forbidden in (
            "call",
            "source_url",
            "file_path",
            "mineru_client",
            "cloud_client",
            "connection",
            "make_persist_image",
            "delete_source_images",
            "event_sink",
        ):
            assert not hasattr(access, forbidden)
        return access.probe()

    monkeypatch.setattr(
        builtin_parser._DelegatingParserLink,
        "probe",
        staticmethod(hostile_probe),
    )

    result = _run(
        tmp_path=tmp_path,
        cloud=_Client(configured=True),
    )

    assert [item.text for item in result.elements] == ["remote"]
    assert len(observed) == 1


def test_non_workbook_remote_output_is_mapped_once(tmp_path, monkeypatch):
    calls = []
    real_mapper = execution_module.mineru_content_list_to_elements

    def counted_mapper(*args, **kwargs):
        calls.append(1)
        return real_mapper(*args, **kwargs)

    monkeypatch.setattr(
        execution_module, "mineru_content_list_to_elements", counted_mapper
    )

    result = _run(
        tmp_path=tmp_path,
        cloud=_Client(configured=True),
    )

    assert [item.text for item in result.elements] == ["remote"]
    assert calls == [1]


def test_baseexception_during_materialize_cleans_started_asset_generation(
    tmp_path
):
    class HardAbort(BaseException):
        pass

    cloud = _Client(
        configured=True,
        result=(
            [
                {
                    "type": "image",
                    "img_path": "figure.png",
                    "image_caption": ["Figure."],
                }
            ],
            {"figure.png": b"PNG"},
        ),
    )
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    deletes = []
    execution = ParserChainExecution(
        host=default_extension_runtime().parser_chain,
        source_id="source-1",
        source_kind="file",
        file_path=str(path),
        file_name="doc.pdf",
        source_url="",
        mineru_client=_Client(),
        cloud_client=cloud,
        connection=_Connection(),
        make_persist_image=lambda: (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                HardAbort("asset write interrupted")
            )
        ),
        delete_source_images=lambda: deletes.append(1),
        event_sink=lambda _event: None,
    )

    proposal = execution.probe(PARSER_CLOUD_PROVIDER)
    assert proposal.accepted is True
    with pytest.raises(HardAbort, match="asset write interrupted"):
        execution.materialize(PARSER_CLOUD_PROVIDER, proposal.value)

    assert execution.materialized is False
    assert execution.assets_pending is False
    assert deletes == [1, 1]


def test_rejected_workbook_writes_no_assets_before_builtin_fallback(
    tmp_path, monkeypatch
):
    from openpyxl import Workbook

    workbook = tmp_path / "book.xlsx"
    book = Workbook()
    sheet = book.active
    for index in range(10):
        sheet.append([f"a{index}", f"b{index}"])
    book.save(workbook)
    cloud = _Client(
        configured=True,
        result=(
            [{"type": "table", "table_body": "<table><tr><td>a</td></tr></table>"}],
            {"a.png": b"PNG"},
        ),
    )
    persisted = []
    monkeypatch.setattr(
        execution_module,
        "parse_builtin_source_file",
        lambda source_id, path, file_name, persist_image=None: [
            SourceElement(
                id="",
                source_id=source_id,
                element_type="table_row",
                location_label="XLSX row 1",
                text="complete",
                metadata={"parser": "xlsx"},
            )
        ],
    )

    result = ParserChainExecution(
        host=default_extension_runtime().parser_chain,
        source_id="source-1",
        source_kind="file",
        file_path=str(workbook),
        file_name="book.xlsx",
        source_url="",
        mineru_client=_Client(),
        cloud_client=cloud,
        connection=_Connection(),
        make_persist_image=lambda: (
            lambda data, name: persisted.append(name) or "asset-1"
        ),
        delete_source_images=lambda: None,
        event_sink=lambda _event: None,
    ).run()

    assert cloud.file_calls == 1
    assert persisted == []
    assert [item.metadata["parser"] for item in result.elements] == ["xlsx"]


def test_production_ingestion_has_one_host_route_and_no_legacy_dispatcher():
    root = Path(__file__).resolve().parents[2]
    ingestion = (root / "backend/app/services/source_ingestion.py").read_text()
    facade = (root / "backend/app/services/repository_facade.py").read_text()
    sqlite_facade = (root / "backend/app/services/sqlite_repository.py").read_text()
    bootstrap = (root / "backend/app/bootstrap.py").read_text()

    assert ingestion.count("ParserChainExecution(") == 1
    for forbidden in (
        "engine_supports_file(",
        "mineru_content_list_to_elements(",
        "parse_url_via_local(",
        "parse_source_file(",
    ):
        assert forbidden not in ingestion
    assert "parse_source_file" not in facade
    assert "parse_source_file" not in sqlite_facade
    assert "parser_provider_chain_host=runtime.parser_chain" in bootstrap
