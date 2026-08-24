from app.core.config import Settings
from app.services.parser_registry import (
    SUPPORTED_SOURCE_EXTENSIONS,
    SUPPORTED_SOURCE_SUFFIXES,
    builtin_parser_id,
    engine_supports_file,
    parser_engine_capabilities,
)


def _settings(**overrides) -> Settings:
    values = {
        "MINERU_MODE": "off",
        "MINERU_API_URL": "",
        "MINERU_API_TOKEN": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_registry_is_the_shared_upload_and_builtin_dispatch_surface():
    assert SUPPORTED_SOURCE_EXTENSIONS == (
        "pdf", "md", "markdown", "zip", "docx", "pptx", "csv", "xlsx", "xlsm", "xls"
    )
    assert SUPPORTED_SOURCE_SUFFIXES == {
        ".pdf", ".md", ".markdown", ".zip", ".docx", ".pptx", ".csv", ".xlsx", ".xlsm", ".xls"
    }
    assert builtin_parser_id("README.Markdown") == "markdown"
    assert builtin_parser_id("export.ZIP") == "markdown_bundle"
    assert builtin_parser_id("table.XLSM") == "xlsx"
    assert builtin_parser_id("legacy.XLS") == "xls"
    assert builtin_parser_id("unknown.txt") == "plain_text"


def test_optional_engine_format_declarations_prevent_unrelated_cloud_uploads():
    # 工作簿(xlsx/xlsm)自 Office 格式解析升级批次起进入 MinerU 引擎声明:
    # 云端上传由行+格覆盖对账把关(mineru_workbook_output_accepted),不再整类拒发。
    for name in ("paper.pdf", "paper.docx", "slides.pptx", "book.xlsx", "book.xlsm"):
        assert engine_supports_file("mineru_cloud", name)
    # .md/.csv 外发解析不了它们的第三方纯属隐私暴露;旧版二进制 .xls MinerU 不支持。
    for name in ("notes.md", "bundle.zip", "data.csv", "legacy.xls"):
        assert not engine_supports_file("mineru_cloud", name)


def test_capability_projection_is_ordered_sanitized_and_explains_unavailability():
    rows = parser_engine_capabilities(_settings())
    assert [row["id"] for row in rows] == [
        "mineru_self_hosted", "mineru_cloud", "builtin"
    ]
    assert [row["priority"] for row in rows] == [1, 2, 3]
    assert rows[0]["available"] is False
    assert rows[0]["unavailable_reason"] == "disabled"
    assert rows[1]["available"] is False
    assert rows[1]["unavailable_reason"] == "missing_credentials"
    assert rows[2]["available"] is True
    assert rows[2]["fallback"] is True
    assert "MINERU_API_TOKEN" not in repr(rows)


def test_self_hosted_execution_boundary_and_missing_endpoint_are_visible():
    missing = parser_engine_capabilities(
        _settings(MINERU_MODE="http", MINERU_API_URL="")
    )[0]
    assert missing["execution"] == "private_service"
    assert missing["available"] is False
    assert missing["unavailable_reason"] == "missing_endpoint"

    configured = parser_engine_capabilities(
        _settings(MINERU_MODE="http", MINERU_API_URL="http://mineru.internal")
    )[0]
    assert configured["execution"] == "private_service"
    assert configured["available"] is True
    assert configured["unavailable_reason"] is None

    local = parser_engine_capabilities(_settings(MINERU_MODE="cli"))[0]
    assert local["execution"] == "local"
    assert local["available"] is True
