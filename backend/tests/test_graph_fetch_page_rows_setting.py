"""``GRAPH_FETCH_PAGE_ROWS`` 部署配置合同(批 3·W4,codex #676)。

scale build 图侧 keyset 分页(``graph_rows``/``active_object_graph_rows``/
``id_element_rows``/``notebook_object_evidence_rows_paged``)与 embedding 向量
分页(``embedding_pages``)共用的每页行数预算,此前是生产代码里的字面量
``10_000``。这里只钉 ``Settings`` 合同本身(默认值、env 覆盖、越界拒绝);
旋钮真正生效的行为由 ``backend/tests/postgres/test_graph_rows_paging_oracle.py``
的 ``small_page`` 夹具(改走 ``postgres_settings.graph_fetch_page_rows``)覆盖。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_graph_fetch_page_rows_default_and_env_override(monkeypatch):
    monkeypatch.delenv("GRAPH_FETCH_PAGE_ROWS", raising=False)
    assert Settings(_env_file=None).graph_fetch_page_rows == 10_000

    monkeypatch.setenv("GRAPH_FETCH_PAGE_ROWS", "500")
    assert Settings(_env_file=None).graph_fetch_page_rows == 500


@pytest.mark.parametrize("value", ["99", "200001", "not-an-integer"])
def test_graph_fetch_page_rows_rejects_out_of_bounds_values(monkeypatch, value):
    monkeypatch.setenv("GRAPH_FETCH_PAGE_ROWS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["100", "200000"])
def test_graph_fetch_page_rows_accepts_boundary_values(monkeypatch, value):
    monkeypatch.setenv("GRAPH_FETCH_PAGE_ROWS", value)
    assert Settings(_env_file=None).graph_fetch_page_rows == int(value)
