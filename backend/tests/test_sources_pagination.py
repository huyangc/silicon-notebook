import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_sources(repo, nb_id, n, prefix="Doc"):
    now = _now()
    # Use nb_id suffix + prefix tag to keep IDs unique across calls on the same db.
    nb_tag = nb_id[-4:]
    pfx_tag = prefix[:3].lower()
    with repo._write() as db:
        for i in range(n):
            # Build a valid ISO timestamp across days to avoid time-overflow for large n.
            total_s = i
            day = total_s // 86400 + 1
            rem = total_s % 86400
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            created = f"2026-01-{day:02d}T{h:02d}:{m:02d}:{s:02d}"
            src_id = f"src-{nb_tag}-{pfx_tag}-{i:04d}"
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (src_id, nb_id, f"{prefix} {i:04d}", "document", f"f{nb_tag}{pfx_tag}{i}.md",
                 f"/tmp/f{i}.md", 0, f"h{i}", "", "", "extracted", created, now))


def test_list_sources_page_paginates(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_sources(repo, nb.id, 130)
    page = repo.list_sources_page(nb.id, offset=0, limit=50)
    assert page.total_count == 130
    assert len(page.items) == 50
    assert page.offset == 0 and page.limit == 50
    assert page.items[0].title == "Doc 0000"        # ORDER BY created_at ASC
    page2 = repo.list_sources_page(nb.id, offset=100, limit=50)
    assert len(page2.items) == 30                    # 末页
    assert page2.items[0].title == "Doc 0100"


def test_list_sources_page_query_filters(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_sources(repo, nb.id, 20, prefix="Alpha")
    _seed_sources(repo, nb.id, 5, prefix="Beta")     # 注意:id 会与上批冲突? 用不同前缀的独立 nb 更稳
    # 重新用干净 nb 避免 id 冲突
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-a", nb2.id, "Voltage Reference", "document", "vref.md", "/tmp/vref.md",
                    0, "ha", "", "", "extracted", "2026-01-01T00:00:00", _now()))
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-b", nb2.id, "Clock Tree", "document", "clk.md", "/tmp/clk.md",
                    0, "hb", "", "", "extracted", "2026-01-01T00:00:01", _now()))
    page = repo.list_sources_page(nb2.id, q="voltage")        # 大小写不敏感、按 title
    assert page.total_count == 1 and page.items[0].id == "src-a"
    page_fn = repo.list_sources_page(nb2.id, q="clk.md")      # 按 file_name
    assert page_fn.total_count == 1 and page_fn.items[0].id == "src-b"


def test_get_sources_route_paginates(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    from app.api import deps
    get_settings.cache_clear(); deps.repository.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app
    client = TestClient(create_app())
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()
    repo = deps.repository()
    _seed_sources(repo, nb["id"], 60)
    r = client.get(f"/api/notebooks/{nb['id']}/sources?offset=0&limit=25")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 60 and len(body["items"]) == 25
    assert body["offset"] == 0 and body["limit"] == 25
    get_settings.cache_clear(); deps.repository.cache_clear()


def test_search_notebook_sql_filtered(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-1", nb.id, "Bandgap Reference", "document", "bg.md", "/tmp/bg.md",
                    0, "h", "", "", "extracted", now, now))
        # 200 个元素,只有 1 个含 needle —— 旧实现会把 200 个全读进内存
        for i in range(200):
            txt = "the curvature correction term" if i == 7 else f"unrelated paragraph {i}"
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,"
                       "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                       (f"el-{i:03d}", "src-1", "paragraph", f"p{i}", txt, "{}", now))
    resp = repo.search_notebook(nb.id, "curvature")
    assert any(h.element_id == "el-007" for h in resp.hits)
    resp_title = repo.search_notebook(nb.id, "bandgap")     # 命中 source title
    assert any(h.scope == "Source" for h in resp_title.hits)
    assert repo.search_notebook(nb.id, "").hits == []        # 空 query 短路


def test_search_notebook_preserves_entity_order_and_filters_payload_keys(repo):
    nb = repo.create_notebook(NotebookCreate(name="needle notebook"))
    now = _now()
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET primary_domain = ? WHERE id = ?",
            ("needle domain", nb.id),
        )
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-order", nb.id, "needle source", "document", "order.md", "/tmp/order.md",
             0, "order-hash", "", "", "extracted", now, now),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("el-order", "src-order", "paragraph", "p1", "needle element", "{}", now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ko-order", nb.id, "concept", '{"name":"needle concept"}', "[]", now, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ko-key-only", nb.id, "concept", '{"needle_key_only":"unrelated"}', "[]", now, now),
        )

    hits = repo.search_notebook(nb.id, "needle").hits
    assert [hit.scope for hit in hits[:4]] == [
        "Notebook", "Domain", "Source", "Element",
    ]
    assert any(hit.label == "needle concept" for hit in hits[4:])
    assert repo.search_notebook(nb.id, "needle_key_only").hits == []
