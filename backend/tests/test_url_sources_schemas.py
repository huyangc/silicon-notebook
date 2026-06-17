from app.models.schemas import (
    SourceSummary, AddUrlSourcesRequest, RejectedUrl, AddUrlSourcesResult,
)


def test_source_summary_source_url_defaults_empty():
    s = SourceSummary(id="s", notebook_id="n", title="t", type="pdf",
                      status="queued", summary="", element_count=0)
    assert s.source_url == ""


def test_add_url_sources_models():
    req = AddUrlSourcesRequest(urls=["https://a/x.pdf"])
    res = AddUrlSourcesResult(created=[], rejected=[RejectedUrl(url="u", reason="非 PDF")])
    assert req.urls == ["https://a/x.pdf"]
    assert res.rejected[0].reason == "非 PDF"
