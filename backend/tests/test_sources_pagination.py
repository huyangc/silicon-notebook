import json

import pytest
from app.core.config import Settings
from app.models.ask import SEARCH_HIT_CAP
from app.services.extraction_profiles import OBJECT_TYPE_LABELS
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


# --------------------------------------------------------------------------
# q 过滤谓词的四腿语义(title / file_name / 作者名 / 论文标题)。
#
# 这组用例先在**旧的 OR-跨表 EXISTS 形态**上跑通,再换成 id 半连接三腿 UNION
# 形态(PostgreSQL 侧同批加 GIN trgm 索引,SQLite 侧只做同构改写、不加索引),
# 所以它们是那次改写的「语义不变」证明,而不是新形态的事后描述。PG 孪生:
# ``tests/postgres/test_core_store_conformance.py`` 的
# ``test_source_search_matches_every_leg_and_stays_inside_the_notebook``。
# --------------------------------------------------------------------------


def _seed_search_source(repo, nb_id, source_id, title, file_name, created,
                        source_type="document"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, nb_id, title, source_type, file_name, f"/tmp/{file_name}",
             0, f"h-{source_id}", "", "", "extracted", created, _now()),
        )


def _seed_paper_meta(repo, nb_id, source_id, paper_title=None, authors=()):
    """``upsert_paper_meta`` 的原始 SQL 等价物 —— facade 不转发那个方法,而这组
    用例要的正是「两张子表里有这样的行」,不是走服务层的写路径。"""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_paper_meta (source_id,notebook_id,is_paper,paper_title,"
            "venue,pub_year,doi,keywords,raw_json,model,created_at,updated_at) "
            "VALUES (?,?,1,?,'',NULL,'','[]','{}','',?,?)",
            (source_id, nb_id, paper_title, now, now),
        )
        for position, name in enumerate(authors):
            db.execute(
                "INSERT INTO source_authors (id,source_id,notebook_id,position,name,"
                "affiliation,created_at) VALUES (?,?,?,?,?,'',?)",
                (f"{source_id}:auth:{position:03d}", source_id, nb_id, position, name, now),
            )


def _search_ids(repo, nb_id, needle):
    page = repo.list_sources_page(nb_id, q=needle, offset=0, limit=200)
    ids = [item.id for item in page.items]
    # total_count 与 items 必须由同一份 where 片段产生 —— 两者分叉会让「共 N 篇」
    # 与实际列出的行数对不上,而那正是 COUNT 与页查询共用 where 的理由。
    assert page.total_count == len(ids), (needle, page.total_count, ids)
    return ids


@pytest.fixture
def search_notebooks(repo):
    """两个 notebook:目标库 nb 与「同名作者/同名标题」的干扰库 other。"""
    nb = repo.create_notebook(NotebookCreate(name="target"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    # 目标库:四条腿各一条命中源,外加一条四腿同时命中的源。
    _seed_search_source(repo, nb.id, "s-title", "Needle Voltage Reference",
                        "vref.md", "2026-01-01T00:00:00")
    _seed_search_source(repo, nb.id, "s-file", "Untitled import",
                        "needle-doc.md", "2026-01-01T00:00:01")
    _seed_search_source(repo, nb.id, "s-author", "Anonymous report",
                        "anon.pdf", "2026-01-01T00:00:02")
    _seed_paper_meta(repo, nb.id, "s-author", paper_title="Unrelated title",
                     authors=("Zeta Needleman",))
    _seed_search_source(repo, nb.id, "s-ptitle", "Scanned upload",
                        "scan.pdf", "2026-01-01T00:00:03")
    _seed_paper_meta(repo, nb.id, "s-ptitle", paper_title="Needle in a Haystack")
    _seed_search_source(repo, nb.id, "s-multi", "Needle everywhere",
                        "needle-multi.md", "2026-01-01T00:00:04")
    _seed_paper_meta(repo, nb.id, "s-multi", paper_title="Needle title",
                     authors=("Needle Author",))
    _seed_search_source(repo, nb.id, "s-miss", "Nothing to see",
                        "plain.md", "2026-01-01T00:00:05")
    # 隐藏合成源:title 与作者名都命中,但 memory/knowhow 必须既不进 items
    # 也不进 total_count。
    _seed_search_source(repo, nb.id, "s-memory", "Needle memory projection",
                        "mem.md", "2026-01-01T00:00:06", source_type="memory")
    _seed_paper_meta(repo, nb.id, "s-memory", paper_title="Needle memory paper",
                     authors=("Needle Ghost",))
    _seed_search_source(repo, nb.id, "s-knowhow", "Needle knowhow projection",
                        "kh.md", "2026-01-01T00:00:07", source_type="knowhow")
    _seed_paper_meta(repo, nb.id, "s-knowhow", paper_title="Needle knowhow paper",
                     authors=("Needle Ghost",))
    # 干扰库:同名作者 + 同名论文标题 + 同名 title/file_name。
    _seed_search_source(repo, other.id, "s-other", "Needle everywhere",
                        "needle-multi.md", "2026-01-01T00:00:00")
    _seed_paper_meta(repo, other.id, "s-other", paper_title="Needle title",
                     authors=("Needle Author",))
    return nb, other


def test_source_search_matches_every_leg(repo, search_notebooks):
    nb, _other = search_notebooks
    assert _search_ids(repo, nb.id, "voltage") == ["s-title"]          # title
    assert _search_ids(repo, nb.id, "vref.md") == ["s-title"]          # file_name
    assert _search_ids(repo, nb.id, "zeta needleman") == ["s-author"]  # 作者名
    assert _search_ids(repo, nb.id, "haystack") == ["s-ptitle"]        # 论文标题


def test_source_search_counts_a_multi_leg_hit_exactly_once(repo, search_notebooks):
    """s-multi 同时命中 title / file_name / 作者名 / 论文标题四条腿。旧形态是一
    个布尔 OR,天然只算一次;新形态是三腿 UNION 再 id 半连接,UNION 去重与
    ``IN`` 半连接语义共同保证同一件事 —— 这条用例把它钉住,别让哪天有人把
    ``UNION`` 写成 ``UNION ALL`` 再 JOIN 回来。"""
    nb, _other = search_notebooks
    assert _search_ids(repo, nb.id, "needle-multi") == ["s-multi"]
    ids = _search_ids(repo, nb.id, "needle")
    assert ids.count("s-multi") == 1
    assert ids == ["s-title", "s-file", "s-author", "s-ptitle", "s-multi"]


def test_source_search_excludes_hidden_source_types(repo, search_notebooks):
    """memory / knowhow 合成源即使 title、论文标题、作者名全部命中,也不能出现在
    结果里,更不能进 total_count —— 可见口径 (VISIBLE_SOURCE_TYPES_PREDICATE)
    对 COUNT 与页查询是同一份。"""
    nb, _other = search_notebooks
    ids = _search_ids(repo, nb.id, "needle")
    assert "s-memory" not in ids and "s-knowhow" not in ids
    assert _search_ids(repo, nb.id, "projection") == []
    assert _search_ids(repo, nb.id, "needle ghost") == []


def test_source_search_never_leaks_another_notebook(repo, search_notebooks):
    """干扰库里有逐字同名的 title / file_name / 作者名 / 论文标题。四条腿都必须
    留在本库内 —— 作者腿与论文腿在新形态里靠子表自己的 ``notebook_id=?`` 收窄,
    在旧形态里靠 ``a.source_id=sources.id`` 回连到已限定 notebook 的外层。"""
    nb, other = search_notebooks
    for needle in ("needle", "needle author", "needle title", "needle-multi"):
        assert "s-other" not in _search_ids(repo, nb.id, needle), needle
    # 反向:干扰库自己搜得到自己那一条,搜不到目标库的任何一条。
    assert _search_ids(repo, other.id, "needle") == ["s-other"]
    assert _search_ids(repo, other.id, "haystack") == []


def _corrupt_child_notebook_ids(repo, other_id):
    """先按正常口径写好,再单独改坏 notebook_id —— 与
    tests/test_memory_source_visibility.py 里那条既有的畸形行用例同一手法:
    建模历史脏数据,不削弱写者。s-ptitle 的 meta 行、s-author 的作者行被改到
    干扰库;s-author 自己的 meta 行不动。"""
    with repo._write() as db:
        db.execute(
            "UPDATE source_paper_meta SET notebook_id = ? WHERE source_id = ?",
            (other_id, "s-ptitle"),
        )
        db.execute(
            "UPDATE source_authors SET notebook_id = ? WHERE source_id = ?",
            (other_id, "s-author"),
        )


def _hydrated_summaries(repo, nb_id, source_id):
    """水合腿的四个构造点各取一份:list_sources / list_sources_page(批量)、
    get_source(单条,外部传入 meta)、source_from_row(单条,自取 meta)。"""
    store = repo._runtime.source_store
    listed = {s.id: s for s in repo.list_sources(nb_id)}
    paged = {s.id: s for s in repo.list_sources_page(nb_id, offset=0, limit=200).items}
    with repo._connect() as db:
        row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        single = store.source_from_row(db, row)
    return [listed[source_id], paged[source_id], repo.get_source(source_id), single]


def test_source_search_ignores_a_legacy_cross_notebook_child_row(repo, search_notebooks):
    """子表行的 notebook_id ≠ 其 source 的 notebook_id 的畸形遗留行,搜索腿与
    水合腿同一口径:都不算这条 source 的论文元数据。

    当前写者写不出这种行:PG 侧 ``upsert_paper_meta`` 先做归属校验,两端的深拷贝
    同时改写 source_id 与 notebook_id,全仓没有把 source 移到另一个 notebook 的
    路径;建表以来唯一的写入调用方传的就是 ``source.notebook_id``。仓库对这类行
    的口径:``report_source_rows`` 家族在 JOIN 上写
    ``AND m.notebook_id = s.notebook_id``,``notebook_analytics`` 的 is_paper 计数
    直接按 ``source_paper_meta.notebook_id`` 分组,``list_sources_page`` 的搜索腿
    按子表自身 ``notebook_id`` 收窄,水合腿 ``paper_meta_for_sources`` 也按子表
    自身 ``notebook_id`` 收窄到 source 所在的库。

    于是这类行上搜不到、也显示不出(display_title 回落到 source 自己的 title,
    authors 为空,paper_meta_status 按「没有行」判),元数据抽取的幂等探针
    ``get_paper_meta`` 也判「没有」。这条用例在收窄前的水合腿上是**红**的:
    那时 display_title 因水合腿命中而显示得到,却搜不到。PG 孪生:
    ``tests/postgres/test_core_store_conformance.py`` 的同名一条。"""
    nb, other = search_notebooks
    store = repo._runtime.source_store
    _corrupt_child_notebook_ids(repo, other.id)
    assert _search_ids(repo, nb.id, "haystack") == []
    assert _search_ids(repo, nb.id, "zeta needleman") == []
    # 畸形行也不会因此泄漏进它被写坏成的那个库(source 本身仍在原库)。
    assert _search_ids(repo, other.id, "haystack") == []
    assert _search_ids(repo, other.id, "zeta needleman") == []
    # 其余腿不受影响:source 行自己的 title/file_name 与本库判定无关。
    assert _search_ids(repo, nb.id, "scanned") == ["s-ptitle"]

    for summary in _hydrated_summaries(repo, nb.id, "s-ptitle"):
        assert summary.display_title == "Scanned upload"
        assert summary.authors == []
        assert summary.paper_meta_status == "missing"
    assert repo.get_source("s-ptitle").paper_meta is None
    assert store.get_paper_meta("s-ptitle", nb.id) is None

    # s-author 的 meta 行没被改坏,只有作者行:meta 仍在、作者被收窄掉。
    for summary in _hydrated_summaries(repo, nb.id, "s-author"):
        assert summary.display_title == "Unrelated title"
        assert summary.authors == []
        assert summary.paper_meta_status == "has_meta"
    assert repo.get_source("s-author").paper_meta.authors == []
    assert store.get_paper_meta("s-author", nb.id)["authors"] == []


def test_reextraction_heals_a_legacy_cross_notebook_child_row(repo, search_notebooks):
    """收窄之后,畸形遗留行在幂等探针看来是「没有」,下一次抽取会重写它;
    ``upsert_paper_meta`` 冲突时一并写回 notebook_id,于是重抽一次就被改正,
    四个读面重新一致可见。不写回的话,重抽后的行仍挂在错的库上、永远被收窄
    藏住,每次 run_extraction 都会再判「没有」再花一次模型调用。"""
    nb, other = search_notebooks
    store = repo._runtime.source_store
    _corrupt_child_notebook_ids(repo, other.id)
    assert store.get_paper_meta("s-ptitle", nb.id) is None

    store.upsert_paper_meta("s-ptitle", nb.id, {
        "is_paper": True, "paper_title": "Needle in a Haystack", "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "raw_json": "{}", "model": "m",
        "authors": [{"position": 0, "name": "Hay Stacker", "affiliation": ""}],
    })

    with repo._connect() as db:
        healed = db.execute(
            "SELECT notebook_id FROM source_paper_meta WHERE source_id = ?", ("s-ptitle",)
        ).fetchone()["notebook_id"]
    assert healed == nb.id
    assert store.get_paper_meta("s-ptitle", nb.id)["paper_title"] == "Needle in a Haystack"
    for summary in _hydrated_summaries(repo, nb.id, "s-ptitle"):
        assert summary.display_title == "Needle in a Haystack"
        assert summary.authors == ["Hay Stacker"]
        assert summary.paper_meta_status == "has_meta"
    assert _search_ids(repo, nb.id, "haystack") == ["s-ptitle"]


def _unnarrowed_paper_meta_for_sources(store):
    """收窄前的水合腿,逐字照搬(只按 source_id 取子表)。差分用例的对照组。"""
    def hydrate(db, source_ids, *, notebook_id):
        del notebook_id  # 收窄前的形态不看它
        meta_rows, author_rows = {}, {}
        ids = list(source_ids)
        for i in range(0, len(ids), store.IN_CHUNK):
            batch = ids[i:i + store.IN_CHUNK]
            ph = ",".join("?" for _ in batch)
            for row in db.execute(
                f"SELECT * FROM source_paper_meta WHERE source_id IN ({ph})", batch,
            ).fetchall():
                meta_rows[row["source_id"]] = row
            for a in db.execute(
                f"SELECT source_id, position, name, affiliation FROM source_authors "
                f"WHERE source_id IN ({ph}) ORDER BY source_id, position ASC", batch,
            ).fetchall():
                author_rows.setdefault(a["source_id"], []).append(a)
        return {
            sid: store._paper_meta_dict(row, author_rows.get(sid, []))
            for sid, row in meta_rows.items()
        }
    return hydrate


def _hydration_snapshot(repo, nb_id):
    """所有走水合腿的读面,序列化成一份 JSON 文本(逐字节比较用)。"""
    store = repo._runtime.source_store
    ids = [s.id for s in repo.list_sources(nb_id)]
    payload = {
        "list_sources": [s.model_dump() for s in repo.list_sources(nb_id)],
        "list_sources_page": repo.list_sources_page(
            nb_id, offset=0, limit=200).model_dump(),
        "get_source": [repo.get_source(sid).model_dump() for sid in ids],
        "get_paper_meta": {sid: store.get_paper_meta(sid, nb_id) for sid in ids},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def test_paper_meta_hydration_is_byte_identical_on_well_formed_rows(
    repo, search_notebooks, monkeypatch
):
    """差分:子表 notebook_id 与 source 一致(唯一写得出来的形态)时,收窄后的
    水合腿与收窄前逐字节相同 —— list_sources / list_sources_page / get_source /
    get_paper_meta 全部读面。对照组是 ``_unnarrowed_paper_meta_for_sources``。
    末段在畸形行上再比一次,证明这个差分有区分力(那里两者必须不同)。"""
    nb, other = search_notebooks
    store = repo._runtime.source_store
    narrowed = _hydration_snapshot(repo, nb.id)
    narrowed_other = _hydration_snapshot(repo, other.id)
    with monkeypatch.context() as patch:
        patch.setattr(store, "paper_meta_for_sources",
                      _unnarrowed_paper_meta_for_sources(store))
        assert _hydration_snapshot(repo, nb.id) == narrowed
        assert _hydration_snapshot(repo, other.id) == narrowed_other

    _corrupt_child_notebook_ids(repo, other.id)
    corrupted = _hydration_snapshot(repo, nb.id)
    with monkeypatch.context() as patch:
        patch.setattr(store, "paper_meta_for_sources",
                      _unnarrowed_paper_meta_for_sources(store))
        assert _hydration_snapshot(repo, nb.id) != corrupted


def test_paper_meta_hydration_plan_stays_on_source_id(repo, search_notebooks):
    """计划钉:收窄项写成 ``+notebook_id = ?``,两条水合语句的计划仍由 page 级
    IN 列表经 source_id 索引驱动,与收窄前逐字相同。去掉一元 ``+``,SQLite 在
    没有 ANALYZE 统计时(本库常态)会改走 idx_source_paper_meta_nb /
    idx_source_authors_nb、按库取出全部行再滤 id —— 3.45(CI)与 3.53 都如此,
    这条钉两版都要成立。语句文本用 spy 从真实调用里抓,不手抄。"""
    nb, _other = search_notebooks
    with repo._connect() as db:
        rows = db.execute(
            "SELECT * FROM sources WHERE notebook_id = ?", (nb.id,)
        ).fetchall()
        captured = []
        original_execute = db.execute

        def spying_execute(sql, params=()):
            text = str(sql)
            if "source_paper_meta" in text or "source_authors" in text:
                captured.append((text, params))
            return original_execute(sql, params)

        db.execute = spying_execute
        try:
            repo._runtime.source_store.sources_from_rows(db, rows)
        finally:
            del db.execute

        assert len(captured) == 2, captured
        plans = {}
        for sql, params in captured:
            assert "+notebook_id = ?" in sql, sql
            assert params[-1] == nb.id, params
            table = "source_paper_meta" if "source_paper_meta" in sql else "source_authors"
            plans[table] = "\n".join(
                row["detail"] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            )

    assert (
        "SEARCH source_paper_meta USING INDEX sqlite_autoindex_source_paper_meta_1 "
        "(source_id=?)" in plans["source_paper_meta"]
    ), plans
    assert (
        "SEARCH source_authors USING INDEX idx_source_authors_source (source_id=?)"
        in plans["source_authors"]
    ), plans
    for plan in plans.values():
        assert "idx_source_paper_meta_nb" not in plan, plans
        assert "idx_source_authors_nb" not in plan, plans


def test_source_search_empty_query_path_is_unchanged(repo, search_notebooks):
    """q 为空(或只有空白)时根本不进过滤分支:整库可见源全在,顺序按
    ``(created_at, id)``。"""
    nb, _other = search_notebooks
    for blank in ("", "   ", None):
        page = repo.list_sources_page(nb.id, q=blank, offset=0, limit=200)
        assert page.total_count == 6
        assert [item.id for item in page.items] == [
            "s-title", "s-file", "s-author", "s-ptitle", "s-multi", "s-miss",
        ]


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


def _search_tables_read(repo, action):
    """`action()` 期间被读到的搜索腿表名集合。

    SQLite 的线程本地连接会被复用,所以这里挂上的 trace 回调也覆盖服务内部自己
    `connect()` 拿到的那条连接（与 test_collection_enumeration 的守卫同一手法）。
    """
    with repo._connect() as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            action()
        finally:
            db.set_trace_callback(None)
    return {
        table
        for table in ("sources", "source_elements", "knowledge_objects")
        if any(f"FROM {table}" in item for item in statements)
    }


def _seed_search_legs(repo, nb_id, source_count):
    """`source_count` 个标题含 needle 的来源,外加同样命中的元素与知识对象各一。"""
    now = _now()
    with repo._write() as db:
        for index in range(source_count):
            src = f"src-leg-{index:03d}"
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (src, nb_id, f"needle source {index:03d}", "document", f"leg{index}.md",
                 f"/tmp/leg{index}.md", 0, f"legh{index}", "", "", "extracted", now, now))
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-leg-{index:03d}", src, "paragraph", "p1", "needle element", "{}", now))
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ko-leg", nb_id, "concept", '{"name":"needle concept"}', "[]", now, now))


def test_search_notebook_skips_later_legs_once_the_cap_is_full(repo):
    """来源腿就填满 cap → 元素腿与知识对象腿一条查询都不发。

    这两条腿打的是该 notebook 的 `source_elements` 与 `knowledge_objects`（生产上
    的千万行表，且 `payload::text` / `se.text` 上都没有可用索引），而它们产出的
    hit 全部落在 `hits[:SEARCH_HIT_CAP]` 之后、会被整段丢掉。所以跳过是逐字等价
    的，不是近似——下面同时断言返回值本身没变。
    """
    nb = repo.create_notebook(NotebookCreate(name="collection"))
    _seed_search_legs(repo, nb.id, SEARCH_HIT_CAP)

    hits: list = []
    tables = _search_tables_read(
        repo, lambda: hits.extend(repo.search_notebook(nb.id, "needle").hits)
    )

    assert len(hits) == SEARCH_HIT_CAP
    assert {hit.scope for hit in hits} == {"Source"}
    assert "sources" in tables
    assert "source_elements" not in tables
    assert "knowledge_objects" not in tables


def test_search_notebook_still_reads_later_legs_below_the_cap(repo):
    """反向守卫：cap 没满时三条腿都要照常发。

    没有这一半，把跳过条件写成恒真（或干脆删掉后面两条腿）也能让上面那个用例
    通过，而元素/知识对象就再也搜不到了。
    """
    nb = repo.create_notebook(NotebookCreate(name="collection"))
    _seed_search_legs(repo, nb.id, 1)

    hits: list = []
    tables = _search_tables_read(
        repo, lambda: hits.extend(repo.search_notebook(nb.id, "needle").hits)
    )

    assert len(hits) < SEARCH_HIT_CAP
    # 知识对象腿的 scope 是界面标签，读 OBJECT_TYPE_LABELS 而不是抄一份字面量：
    # 那份对照表是前后端逐字一致的契约，测试里再拼一遍就会各自漂。
    assert {hit.scope for hit in hits} == {
        "Source", "Element", OBJECT_TYPE_LABELS["concept"],
    }
    assert tables == {"sources", "source_elements", "knowledge_objects"}


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
