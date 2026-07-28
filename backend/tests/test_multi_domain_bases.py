"""多领域基准库 —— 挂载集合取代全局唯一 base。"""

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _mount(repo, notebook_id, base_ids):
    repo.replace_notebook_bases(notebook_id, base_ids, "user-local")


class TestResolve:
    def test_no_mount_means_only_self(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        with repo._connect() as db:
            got = repo._runtime.notebook_store.resolve_participants(db, a.id)
        assert got == [(a.id, "personal")], "未挂载就不该吃到任何 base"

    def test_mounted_public_base_participates(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        with repo._connect() as db:
            got = dict(repo._runtime.notebook_store.resolve_participants(db, a.id))
        assert got == {a.id: "personal", base.id: "base"}

    def test_multi_mount(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        _mount(repo, a.id, [b1.id, b2.id])
        with repo._connect() as db:
            ids = repo._runtime.notebook_store.participant_ids(db, a.id)
        assert set(ids) == {a.id, b1.id, b2.id}

    def test_participants_order_base_before_personal(self, repo):
        """MOUNT_ORDER 的次序契约:参与集里(除首项恒为 active 自身外)公共知识库
        (tier='base')必须排在个人库之前,不管两者名字的字典序如何——这里刻意让
        个人库名字典序在前("AAA"),公共库名字典序在后("ZZZ"),故意与插入顺序
        (先挂个人库、后挂公共库)也相反。纯按名字排序、或曾经的 bug
        (`ORDER BY b.tier DESC, b.name`——'personal' 字典序大于 'base',DESC 反而
        把个人库排到公共库前面)都会让个人库排在公共库前面;只有真正按 tier
        优先级排序才能通过。下游消费者(reasoning_retrieval.py 的
        expand_community 总量帽 `_COMMUNITY_PEERS_CAP_FACTOR`、combined-index
        组装)都假设这个顺序——公共知识库排后面会导致个人库先把帽子吃掉。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        personal_lib = repo.create_notebook(NotebookCreate(name="AAA个人库"))
        base_lib = repo.create_notebook(NotebookCreate(name="ZZZ公共库"))
        repo.mark_notebook_base(base_lib.id)
        _mount(repo, a.id, [personal_lib.id, base_lib.id])
        with repo._connect() as db:
            got = repo._runtime.notebook_store.resolve_participants(db, a.id)
        assert got[0] == (a.id, "personal"), "首项恒为 active 自身"
        mounted_ids = [nb_id for nb_id, _tier in got[1:]]
        assert mounted_ids == [base_lib.id, personal_lib.id], (
            "公共知识库(tier=base)必须排在个人库之前,不受名字字典序或挂载顺序"
            f"影响,实际次序 {mounted_ids}"
        )

    def test_mounting_own_personal_notebook_keeps_personal_tier(self, repo):
        a = repo.create_notebook(NotebookCreate(name="项目"))
        mine = repo.create_notebook(NotebookCreate(name="我的模拟笔记"))
        _mount(repo, a.id, [mine.id])
        with repo._connect() as db:
            got = dict(repo._runtime.notebook_store.resolve_participants(db, a.id))
        assert got[mine.id] == "personal", "挂自己的库不应被提升为 base"

    def test_mounting_is_not_transitive(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b = repo.create_notebook(NotebookCreate(name="b"))
        c = repo.create_notebook(NotebookCreate(name="c"))
        repo.mark_notebook_base(c.id)
        _mount(repo, b.id, [c.id])
        _mount(repo, a.id, [b.id])
        with repo._connect() as db:
            ids = repo._runtime.notebook_store.participant_ids(db, a.id)
        assert set(ids) == {a.id, b.id}, "只看直接挂的一跳，C 不应出现"

    def test_edge_to_other_owners_personal_notebook_is_skipped(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        theirs = repo.create_notebook(NotebookCreate(name="别人的"))
        _mount(repo, a.id, [theirs.id])
        other = repo.create_user("a00900001", "pw")  # created_by 有 FK REFERENCES users(id)
        with repo._write() as db:  # 模拟易主
            db.execute(
                "UPDATE notebooks SET created_by=? WHERE id=?", (other.id, theirs.id)
            )
        with repo._connect() as db:
            ids = repo._runtime.notebook_store.participant_ids(db, a.id)
        assert ids == [a.id], "边不是授权凭证，易主后必须跳过"

    def test_demoted_public_base_is_skipped_then_restored(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        other = repo.create_user("a00900002", "pw")  # created_by 有 FK REFERENCES users(id)
        with repo._write() as db:  # 公共库不是我的，且被降级
            db.execute(
                "UPDATE notebooks SET created_by=? WHERE id=?", (other.id, base.id)
            )
        repo.set_notebook_personal(base.id)
        with repo._connect() as db:
            assert repo._runtime.notebook_store.participant_ids(db, a.id) == [a.id]
        repo.mark_notebook_base(base.id)  # 重新发布 → 边自动恢复（从未删除）
        with repo._connect() as db:
            assert set(repo._runtime.notebook_store.participant_ids(db, a.id)) == {a.id, base.id}

    def test_list_mount_edges_reports_inactive(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        other = repo.create_user("a00900003", "pw")  # created_by 有 FK REFERENCES users(id)
        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET created_by=? WHERE id=?", (other.id, base.id)
            )
        repo.set_notebook_personal(base.id)
        edges = repo.list_notebook_bases(a.id)
        assert len(edges) == 1
        assert edges[0]["active"] is False
        assert edges[0]["inactive_reason"]

    def test_replace_mounts_is_full_replacement(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="b1"))
        b2 = repo.create_notebook(NotebookCreate(name="b2"))
        _mount(repo, a.id, [b1.id])
        _mount(repo, a.id, [b2.id])
        with repo._connect() as db:
            assert set(repo._runtime.notebook_store.participant_ids(db, a.id)) == {a.id, b2.id}

    def test_mountable_excludes_self_and_others_personal(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        mine = repo.create_notebook(NotebookCreate(name="mine"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        theirs = repo.create_notebook(NotebookCreate(name="theirs"))
        repo.mark_notebook_base(base.id)
        other = repo.create_user("a00900004", "pw")  # created_by 有 FK REFERENCES users(id)
        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET created_by=? WHERE id=?", (other.id, theirs.id)
            )
        got = {n["id"] for n in repo.mountable_notebooks(a.id)}
        assert got == {mine.id, base.id}

    def test_participant_resolution_uses_mounting_owner_not_requester(self, repo):
        """owner 判定取「挂载方笔记本 A 的 created_by」而非发起解析的请求用户——
        用两个真实用户才区分得出来,单一 user-local(拥有全部笔记本)测不出这条
        不变量。被挂库刻意用个人库(非公共 base),逼 MOUNT_VALID_EXPR 走
        created_by 分支而非 tier='base' 分支。只读共享的访客与库主必须解析出
        同一个参与集(mount_sql.py 模块 docstring 的设计意图)。"""
        from app.core.request_context import reset_request_user, set_request_user
        jia = repo.create_user("a00900005", "pw")  # 甲:A 与被挂个人库的 owner
        yi = repo.create_user("a00900006", "pw")   # 乙:发起解析请求的另一用户

        tok = set_request_user(jia)
        try:
            a = repo.create_notebook(NotebookCreate(name="a"))       # created_by=甲
            lib = repo.create_notebook(NotebookCreate(name="lib"))   # created_by=甲,personal
            _mount(repo, a.id, [lib.id])
        finally:
            reset_request_user(tok)

        tok = set_request_user(yi)  # 以乙的身份解析——乙既不拥有 a 也不拥有 lib
        try:
            with repo._connect() as db:
                ids = repo._runtime.notebook_store.participant_ids(db, a.id)
        finally:
            reset_request_user(tok)
        assert set(ids) == {a.id, lib.id}, "参与集只取决于 A 的 owner(甲),与发起请求的乙无关"

class TestKgGate:
    """深入分析可用性门:_any_base_notebook_has_kg 按「本库是否挂载了该 base」判定,
    不再是「系统里存在任一有 KG 的 base」就全局放行。

    Task 4 brief 原始 Step 1 用 `repo.get_notebook(a.id).base_kg_available` 做断言载体,
    但写这条测试时该字段由 NotebookSummaryQuery.base_notebook_info/QueryStore.
    base_notebook_info_row 独立计算(全局 tier='base' EXISTS,无 notebook_id 参数),
    与本任务改的 KnowledgeStore.any_mounted_has_kg*/_any_base_notebook_has_kg 是完全
    不同的代码路径,当时改完 KnowledgeStore 不会让 base_kg_available 的值发生任何
    变化。Task 5(消除单例 base_notebooks 契约)已把这条查询改名为 mounted_bases/
    mounted_bases_row 并按挂载判定——base_notebook_info/base_notebook_info_row 这两个
    名字现在已不存在,证明规划时就没打算让 Task 4 碰它,那是 Task 5
    "NotebookSummary.base_kg_available(语义改为按挂载)"明确认领并已完成的产出。
    故这里改测 Task 4 真正产出、并被 ask_service/retrieval_candidates 实际消费的接口:
    facade `_any_base_notebook_has_kg(notebook_id)` 本身。
    """

    def test_unmounted_base_with_kg_does_not_open_gate(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.store_kg(base.id, None, [
            {"local_id": "K1", "object_type": "concept",
             "payload": {"name": "Gain", "definition": "增益"}, "evidence": []},
        ], [])
        assert repo._any_base_notebook_has_kg(a.id) is False

    def test_mounted_base_with_kg_opens_gate(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.store_kg(base.id, None, [
            {"local_id": "K1", "object_type": "concept",
             "payload": {"name": "Gain", "definition": "增益"}, "evidence": []},
        ], [])
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        assert repo._any_base_notebook_has_kg(a.id) is True


class TestScaleEligible:
    """挂自己的一个大笔记本当参考库时,它既非 tier='base' 也未必已有磁盘索引 ——
    没有这一条,大库会在 PPR 侧触发 ppr_fallback_refused 直接返空(静默失效,比
    报错难查)。「被任何笔记本挂载」本身必须构成建索引资格,不问挂载是否仍然
    「有效」(mount_sql.py 的有效性谓词是解析参与集用的,这里要的是「该不该为它
    投入索引成本」,边失效通常是临时的,不该让索引跟着过期)。"""

    def test_mounted_personal_notebook_becomes_index_eligible(self, repo):
        a = repo.create_notebook(NotebookCreate(name="项目"))
        mine = repo.create_notebook(NotebookCreate(name="我的大笔记"))
        assert repo._scale_index_eligible(mine.id) is False
        repo.replace_notebook_bases(a.id, [mine.id], "user-local")
        assert repo._scale_index_eligible(mine.id) is True

    def test_unmounted_personal_notebook_stays_ineligible(self, repo):
        """对照组:纯粹创建/挂载别的库不该误伤——只有「自己被挂」才翻转。"""
        a = repo.create_notebook(NotebookCreate(name="项目"))
        other = repo.create_notebook(NotebookCreate(name="别的库"))
        mine = repo.create_notebook(NotebookCreate(name="我的大笔记"))
        repo.replace_notebook_bases(a.id, [other.id], "user-local")
        assert repo._scale_index_eligible(mine.id) is False


class TestPromotionTarget:
    """晋升目标显式化(Task 7)——`first_base_notebook_row` 的全局
    `ORDER BY created_at ASC LIMIT 1` 在多领域下语义已不成立:挂 0 个公共库
    必须拒绝(不给候选一个「反正总有个默认」的假象);挂 1 个可以安全默认;
    挂 >1 个必须由调用方显式指定,不能替用户猜。"""

    def _concept(self, repo, notebook_id, name="Gain"):
        return repo._test_insert_object(
            notebook_id, "concept", {"name": name, "definition": "增益"}
        )

    def test_promote_without_mounted_public_base_is_rejected(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        oid = self._concept(repo, a.id)
        with pytest.raises(ValueError, match="尚未挂载任何公共知识库"):
            repo.propose_promotion(a.id, oid, target_base_id="")

    def test_promote_rejects_when_only_a_personal_library_is_mounted(self, repo):
        """`mounted_public_base_ids` 的 `AND b.tier = 'base'` 过滤是这个方法
        唯一的任务特有语义(docstring:「晋升只能进公共知识库,不能进别人的个人
        库」)。与 test_promote_without_mounted_public_base_is_rejected(0 个挂载)
        不同,这里挂了 1 个东西——只是它不是公共库。删掉那个 tier 过滤,这个
        个人库会被 _resolve_promotion_target 当成唯一候选默认选中,晋升就会
        悄悄写进别人的个人笔记本。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        personal_lib = repo.create_notebook(NotebookCreate(name="别人的个人库"))
        repo.replace_notebook_bases(a.id, [personal_lib.id], "user-local")
        oid = self._concept(repo, a.id)
        with pytest.raises(ValueError, match="尚未挂载任何公共知识库"):
            repo.propose_promotion(a.id, oid)

    def test_promote_with_single_mounted_base_defaults_to_it(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid)
        with repo._connect() as db:
            row = db.execute(
                "SELECT target_base_id FROM promotion_candidates WHERE id=?",
                (cand["id"],),
            ).fetchone()
        assert row["target_base_id"] == base.id

    def test_promote_with_multiple_mounted_bases_requires_explicit_target(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        oid = self._concept(repo, a.id)
        with pytest.raises(ValueError, match="指定晋升目标"):
            repo.propose_promotion(a.id, oid)

    def test_promote_records_explicit_target(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid, target_base_id=b2.id)
        with repo._connect() as db:
            row = db.execute(
                "SELECT target_base_id FROM promotion_candidates WHERE id=?",
                (cand["id"],),
            ).fetchone()
        assert row["target_base_id"] == b2.id

    def test_promote_rejects_target_not_in_mounted_set(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        other = repo.create_notebook(NotebookCreate(name="other"))
        repo.mark_notebook_base(base.id)
        repo.mark_notebook_base(other.id)  # 公共库,但 a 没挂它
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)
        with pytest.raises(ValueError, match="已挂载"):
            repo.propose_promotion(a.id, oid, target_base_id=other.id)

    def test_approve_promotion_writes_into_explicit_target_not_earliest_created(self, repo):
        """回归防守:原 `first_base_notebook_row` 取
        `ORDER BY created_at ASC LIMIT 1`——本用例故意让「最早创建的库」(b1)
        与「显式指定的晋升目标」(b2)不同,证明审批确实按 target_base_id
        写入,而不是静默落进最早创建的那个 base。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="早创建"))
        b2 = repo.create_notebook(NotebookCreate(name="后创建"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid, target_base_id=b2.id)
        repo.approve_promotion(cand["id"])
        with repo._connect() as db:
            b1_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (b1.id,),
            ).fetchone()["c"]
            b2_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (b2.id,),
            ).fetchone()["c"]
        assert b2_count == 1
        assert b1_count == 0

    def test_approve_promotion_with_empty_legacy_target_raises_actionable_error(self, repo):
        """迁移 20 之前遗留的候选行 target_base_id 是迁移给的默认空串——按设计
        宁可报错也不去猜(不悄悄捞「最早创建的 base」)。错误信息本身要点名
        target_base_id,让操作者看得懂该去补什么,而不是一个笼统的失败。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        oid = self._concept(repo, a.id)
        cand_id = "promo-legacy-empty-target"
        with repo._write() as db:
            db.execute(
                "INSERT INTO promotion_candidates "
                "(id, notebook_id, object_id, object_type, status, reason, "
                " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
                "VALUES (?,?,?,?,'proposed','','','',?,?,'')",
                (cand_id, a.id, oid, "concept",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        with pytest.raises(ValueError, match="target_base_id"):
            repo.approve_promotion(cand_id)


# ---------------------------------------------------------------------------
# Task 8: API 端点(GET/PUT bases, GET mountable)+ Step 4b 晋升目标 HTTP 通路。
# 全部追加在文件末尾(而非穿插进上面按行号钉住的类)——本文件不在
# LINE_NUMBER_INSENSITIVE_FILES 白名单里,顶行会打破已登记的 consumer 行号;
# 新增测试内部一律用局部 import(TestClient/create_app/repository),不碰文件
# 顶部的 import 块。
# ---------------------------------------------------------------------------


class TestApi:
    """三个挂载端点。Step 1 复用 auth_optional 的隐式单用户——conftest 的
    autouse _reset_singleton_caches 先跑并清空 repository()/get_settings() 的
    lru_cache,随后本文件的 repo fixture 把 DATABASE_URL 指到自己的 tmp db,
    所以 TestClient(create_app()) 首次触发 repository() 时读到的是同一个
    DATABASE_URL——app 与 repo fixture 落在同一份 sqlite 文件上。"""

    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import create_app
        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        return TestClient(create_app())

    def test_bases_roundtrip(self, repo, monkeypatch):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)

        got = client.get(f"/api/notebooks/{a.id}/mountable").json()
        assert base.id in [n["id"] for n in got]
        assert a.id not in [n["id"] for n in got]

        put = client.put(
            f"/api/notebooks/{a.id}/bases", json={"base_notebook_ids": [base.id]}
        )
        assert put.status_code == 200
        assert [b["id"] for b in put.json()] == [base.id]
        assert client.get(f"/api/notebooks/{a.id}/bases").json()[0]["active"] is True

        client.put(f"/api/notebooks/{a.id}/bases", json={"base_notebook_ids": []})
        assert client.get(f"/api/notebooks/{a.id}/bases").json() == []

    def test_put_bases_dedupes_repeated_ids_in_the_payload(self, repo, monkeypatch):
        """路由层 dict.fromkeys 去重 —— 重复 id 不该报错也不该产生重复行
        (facade replace_mounts 本身也是全量 DELETE+INSERT,双重保险)。"""
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)

        resp = client.put(
            f"/api/notebooks/{a.id}/bases",
            json={"base_notebook_ids": [base.id, base.id]},
        )
        assert resp.status_code == 200
        assert [b["id"] for b in resp.json()] == [base.id]


@pytest.fixture
def two_users_client(monkeypatch, tmp_path):
    """Task 8 安全测试专用:两个真实注册用户(auth_optional=false,不共享隐式
    的 user-local),notebook 一律经 HTTP 创建以获得正确的 created_by —— 直接
    repo.create_notebook() 都会落在同一个 owner 上,测不出跨 owner 场景。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_auth.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "api_storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    client = TestClient(create_app())

    def _register(username):
        body = client.post(
            "/api/auth/register", json={"username": username, "password": "pw"}
        ).json()
        return body["user"]["id"], {"Authorization": f"Bearer {body['token']}"}

    u1_id, u1 = _register("a00900201")
    u2_id, u2 = _register("a00900202")
    return {"client": client, "u1_id": u1_id, "u1": u1, "u2_id": u2_id, "u2": u2}


class TestApiUnauthorizedWrite:
    """PUT /notebooks/{id}/bases 是写入侧:必须同时守写权限(notebook_id 本身
    是不是我的 —— require_notebook_access,owner-only)与候选集(传入的
    base_notebook_ids 是不是我能挂的 —— mountable_notebooks)。两条防线分别用
    真实两用户打穿,并且每条都查库确认失败请求真的零写入,不能只看响应码。
    可挂候选刻意排除只读分享(notebook_members)——本仓库 2026-07-17 的
    knowhow/memory 转移设计对"只读访问 ≠ 可挂候选"这个问题拍过同样的板,
    notebook_store.mountable_notebooks 的 docstring 原话也是这句。"""

    def test_non_owner_cannot_write_bases_at_all(self, two_users_client):
        c = two_users_client
        client = c["client"]
        a = client.post("/api/notebooks", headers=c["u1"], json={"name": "u1 项目"}).json()

        resp = client.put(
            f"/api/notebooks/{a['id']}/bases", headers=c["u2"],
            json={"base_notebook_ids": []},
        )
        assert resp.status_code == 404, "u2 对 a 没有任何访问权限,写守卫先挡(404 不泄露存在性)"

        from app.api.deps import repository
        assert repository().list_notebook_bases(a["id"]) == []

    def test_owner_cannot_mount_a_strangers_private_notebook(self, two_users_client):
        c = two_users_client
        client = c["client"]
        a = client.post("/api/notebooks", headers=c["u1"], json={"name": "u1 项目"}).json()
        theirs = client.post(
            "/api/notebooks", headers=c["u2"], json={"name": "u2 私有笔记"}
        ).json()

        resp = client.put(
            f"/api/notebooks/{a['id']}/bases", headers=c["u1"],
            json={"base_notebook_ids": [theirs["id"]]},
        )
        assert resp.status_code == 400, "theirs 既非公共库也不同 owner,不在候选集里"

        from app.api.deps import repository
        assert repository().list_notebook_bases(a["id"]) == [], "候选校验拒绝后必须零写入"

    def test_read_only_share_does_not_grant_mountable_status(self, two_users_client):
        """显式构造"u2 把私有库分享给 u1(notebook_members,role='reader')"这个
        状态,证明即便 u1 对 stranger 有合法只读权限,仍然挂不上——可挂候选
        (mountable_notebooks)刻意不查 notebook_members,对方撤销分享后边仍在
        也不会变成越权通道。"""
        c = two_users_client
        client = c["client"]
        from app.api.deps import repository
        repo_api = repository()

        a = client.post("/api/notebooks", headers=c["u1"], json={"name": "u1 项目"}).json()
        shared = client.post(
            "/api/notebooks", headers=c["u2"], json={"name": "u2 分享给我的库"}
        ).json()
        with repo_api._write() as db:
            db.execute(
                "INSERT INTO notebook_members (notebook_id, user_id, role, added_at) "
                "VALUES (?,?,?,?)",
                (shared["id"], c["u1_id"], "reader", "2026-07-19T00:00:00Z"),
            )
        assert repo_api.user_can_read_notebook(shared["id"], c["u1_id"]) is True, (
            "前置条件:分享确实生效,下面的 400 不能是因为 u1 连读都读不到"
        )

        resp = client.put(
            f"/api/notebooks/{a['id']}/bases", headers=c["u1"],
            json={"base_notebook_ids": [shared["id"]]},
        )
        assert resp.status_code == 400
        assert repo_api.list_notebook_bases(a["id"]) == []

    def test_owner_can_mount_a_public_base_as_a_positive_control(self, two_users_client):
        """对照组:证明上面三条拒绝不是端点整体失灵——同一撮用户,挂一个真正
        公共的库必须 200 且边真的落库。"""
        c = two_users_client
        client = c["client"]
        from app.api.deps import repository
        repo_api = repository()

        a = client.post("/api/notebooks", headers=c["u1"], json={"name": "u1 项目"}).json()
        base = client.post(
            "/api/notebooks", headers=c["u2"], json={"name": "公共库"}
        ).json()
        repo_api.mark_notebook_base(base["id"])

        resp = client.put(
            f"/api/notebooks/{a['id']}/bases", headers=c["u1"],
            json={"base_notebook_ids": [base["id"]]},
        )
        assert resp.status_code == 200
        assert [b["id"] for b in resp.json()] == [base["id"]]
        assert [e["id"] for e in repo_api.list_notebook_bases(a["id"])] == [base["id"]]

    def test_mounter_locates_base_kg_object_through_active_notebook_scope(self, two_users_client):
        """A public base participates in retrieval without granting notebook
        membership. KG deep links must therefore authorize on the active personal
        notebook and proxy only to one of its valid mounted participants."""
        c = two_users_client
        client = c["client"]
        from app.api.deps import repository
        repo_api = repository()

        active = client.post(
            "/api/notebooks", headers=c["u1"], json={"name": "u1 项目"}
        ).json()
        base = client.post(
            "/api/notebooks", headers=c["u2"], json={"name": "u2 公共库"}
        ).json()
        repo_api.mark_notebook_base(base["id"])
        mounted = client.put(
            f"/api/notebooks/{active['id']}/bases",
            headers=c["u1"],
            json={"base_notebook_ids": [base["id"]]},
        )
        assert mounted.status_code == 200

        source_id = repo_api._test_insert_object(
            base["id"], "concept", {"name": "Base source concept"}
        )
        target_id = repo_api._test_insert_object(
            base["id"], "claim", {"name": "Base target claim"}
        )
        with repo_api._write() as db:
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "cc-base-focus",
                    base["id"],
                    "K-base-focus",
                    source_id,
                    "Base source concept",
                    "concept",
                    "2026-07-28T00:00:00Z",
                ),
            )
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "kr-base-focus",
                    base["id"],
                    None,
                    source_id,
                    target_id,
                    "supports",
                    "[]",
                    "2026-07-28T00:00:00Z",
                ),
            )

        # The mounter is deliberately not a base member; direct reads remain 404.
        direct = client.get(
            f"/api/notebooks/{base['id']}/objects/{source_id}/neighbors",
            headers=c["u1"],
        )
        assert direct.status_code == 404

        proxied = client.get(
            f"/api/notebooks/{active['id']}/objects/{source_id}/neighbors",
            headers=c["u1"],
            params={"source_notebook_id": base["id"]},
        )
        assert proxied.status_code == 200
        assert proxied.json()["source_notebook_id"] == base["id"]
        assert proxied.json()["focus_id"] == "K-base-focus"
        assert proxied.json()["focus_object_id"] == source_id
        assert {node["id"] for node in proxied.json()["nodes"]} == {
            "K-base-focus",
            target_id,
        }

        inferred = client.get(
            f"/api/notebooks/{active['id']}/objects/{source_id}/neighbors",
            headers=c["u1"],
        )
        assert inferred.status_code == 200
        assert inferred.json()["source_notebook_id"] == base["id"]

        context = client.get(
            f"/api/notebooks/{active['id']}/objects/{proxied.json()['focus_object_id']}/context",
            headers=c["u1"],
            params={"source_notebook_id": base["id"]},
        )
        assert context.status_code == 200
        assert context.json()["id"] == source_id


class TestPromotionTargetApi:
    """Task 8 Step 4b(计划补记,2026-07-19):晋升目标 target_base_id 的三处
    HTTP 通路。服务层参数 Task 7 已就绪(propose_promotion 早就接
    target_base_id);本任务给 propose_memory_promotion 在 knowledge_governance
    / memory_service / facade 三层补齐对称的关键字参数,这里测路由层是否正确
    透传,以及晋升队列的读接口是否把目标暴露出来。"""

    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import create_app
        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        return TestClient(create_app())

    def _concept(self, repo, notebook_id):
        return repo._test_insert_object(
            notebook_id, "concept", {"name": "Gain", "definition": "增益"}
        )

    def test_promote_knowledge_object_requires_target_when_multiple_bases_mounted(
        self, repo, monkeypatch
    ):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        oid = self._concept(repo, a.id)

        no_target = client.post(f"/api/notebooks/{a.id}/knowledge/{oid}/promote")
        assert no_target.status_code == 400, no_target.text

        ok = client.post(
            f"/api/notebooks/{a.id}/knowledge/{oid}/promote",
            json={"target_base_id": b2.id},
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["target_base_id"] == b2.id

    def test_promote_knowledge_object_rejects_target_outside_mounted_set(
        self, repo, monkeypatch
    ):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        other = repo.create_notebook(NotebookCreate(name="other"))
        repo.mark_notebook_base(base.id)
        repo.mark_notebook_base(other.id)  # 公共库,但 a 没挂它
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)

        resp = client.post(
            f"/api/notebooks/{a.id}/knowledge/{oid}/promote",
            json={"target_base_id": other.id},
        )
        assert resp.status_code == 400, resp.text

    def test_promote_memory_requires_target_when_multiple_bases_mounted(
        self, repo, monkeypatch
    ):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        candidate = repo.create_memory_candidate(
            a.id, "user-local", None, "task8-promote-1", "Claim",
            "A grounded claim", [], "reason", {}, [],
        )
        memory = repo.confirm_memory(candidate.id, "user-local")

        no_target = client.post(f"/api/memories/{memory.id}/promote")
        assert no_target.status_code == 400, no_target.text

        ok = client.post(
            f"/api/memories/{memory.id}/promote", json={"target_base_id": b2.id}
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["target_base_id"] == b2.id

    def test_promote_memory_rejects_target_outside_mounted_set(self, repo, monkeypatch):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        other = repo.create_notebook(NotebookCreate(name="other"))
        repo.mark_notebook_base(base.id)
        repo.mark_notebook_base(other.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        candidate = repo.create_memory_candidate(
            a.id, "user-local", None, "task8-promote-2", "Claim",
            "A grounded claim", [], "reason", {}, [],
        )
        memory = repo.confirm_memory(candidate.id, "user-local")

        resp = client.post(
            f"/api/memories/{memory.id}/promote", json={"target_base_id": other.id}
        )
        assert resp.status_code == 400, resp.text

    def test_promotion_queue_exposes_target_base_id(self, repo, monkeypatch):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)

        promoted = client.post(f"/api/notebooks/{a.id}/knowledge/{oid}/promote")
        assert promoted.status_code == 201, promoted.text

        queue = client.get("/api/promotion-queue").json()
        row = next(c for c in queue if c["id"] == promoted.json()["id"])
        assert row["target_base_id"] == base.id

    def test_promote_memory_with_zero_mounted_bases_is_400_not_409(self, repo, monkeypatch):
        """Task 8 审查 Minor #2:PromotionTargetError 是 ValueError 的子类,
        `_memory_call`(memory_routes.py)正是靠这层子类关系把「晋升目标不合法」
        从默认的 409(状态冲突)分流成 400(输入错误)。三条 raise 里挂 0 个公共库
        这条最常见——新笔记本什么都没挂正是最典型的真实场景——却是三条里唯一
        只有服务层 `pytest.raises(ValueError, match=...)` 覆盖(基类 + 文案匹配,
        测不出「是不是 PromotionTargetError 这个子类」)、没有 HTTP 状态码回归的
        一条:若它退化成裸 ValueError,这条路由会静默把 400 错判成 409,而当时
        全套测试仍会全绿。"""
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))  # 故意不挂任何库
        candidate = repo.create_memory_candidate(
            a.id, "user-local", None, "task8-review-zero-mount", "Claim",
            "A grounded claim", [], "reason", {}, [],
        )
        memory = repo.confirm_memory(candidate.id, "user-local")

        resp = client.post(f"/api/memories/{memory.id}/promote")
        assert resp.status_code == 400, resp.text


class TestMountEdgeNamePrivacy:
    """Task 8 审查 Minor #1(信息泄露):挂载时合法看到过库当时的名字,不代表
    库主之后改的新名字也该继续流给已经无权访问的挂载方——list_mount_edges
    若对失效边原样吐 b.name,GET .../bases 就会把「实时」名字变成一条持续的
    信息泄露通道(参见 notebook_store.py list_mount_edges 的实现注释)。"""

    def test_demoted_and_renamed_base_does_not_leak_new_name(self, repo):
        """最小复现:库主(与 a 的 owner 不同)发布公共库→我挂上→库主把库降级
        为 personal 并改名——不涉及易主,base.created_by 全程不变,证明单是
        「降级」(不需要额外的易主)就足以让新名字被遮蔽。"""
        from app.core.request_context import reset_request_user, set_request_user
        owner = repo.create_user("a00900008", "pw")  # 库主,与 a 的 owner 不同
        tok = set_request_user(owner)
        try:
            base = repo.create_notebook(NotebookCreate(name="旧名字"))
        finally:
            reset_request_user(tok)
        repo.mark_notebook_base(base.id)

        a = repo.create_notebook(NotebookCreate(name="a"))
        _mount(repo, a.id, [base.id])

        with repo._write() as db:  # 库主改名——created_by 全程不变,只有 tier 会变
            db.execute("UPDATE notebooks SET name=? WHERE id=?", ("改名后的新名字", base.id))
        repo.set_notebook_personal(base.id)

        edges = repo.list_notebook_bases(a.id)
        assert len(edges) == 1
        assert edges[0]["active"] is False
        assert edges[0]["inactive_reason"]
        assert edges[0]["name"] != "改名后的新名字", "降级后改的新名字不该流向已经无权访问的挂载方"

    def test_own_personal_library_mount_is_never_masked(self, repo):
        """对照组:挂自己的个人库——从未 mark_notebook_base,纯粹靠
        MOUNT_VALID_EXPR 的「b.created_by = a.created_by」分支保持有效(同
        test_mounting_own_personal_notebook_keeps_personal_tier 的挂载方式)。

        注:MOUNT_VALID_EXPR = (b.tier='base' OR b.created_by=a.created_by),
        同 owner 是它自身的一个 OR 分支,数学上「同 owner 却失效」在当前谓词
        下不可达(同 owner 必然有效)——「自己拥有的失效边不遮蔽」这条要求在
        今天的数据模型下是恒成立的,遮蔽逻辑仍显式带上 same_owner 分支只是
        不把这条不变量的成立隐式寄生在 MOUNT_VALID_EXPR 的具体写法上。这里
        转而验证同一个不变量在「有效」一侧的可达形态:同 owner 挂载即便从未
        公开过,也必须显示真实名字,不能把「不是公共库」本身错当成遮蔽信号。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        mine = repo.create_notebook(NotebookCreate(name="我自己的库"))
        _mount(repo, a.id, [mine.id])

        edges = repo.list_notebook_bases(a.id)
        assert len(edges) == 1
        assert edges[0]["active"] is True
        assert edges[0]["name"] == "我自己的库"


class TestPromotionQueueTargetBaseName:
    """Task 13 审查 #4:晋升队列必须由后端直接给出 target_base_name,不能只让
    前端 notebooks.find(本用户自有∪只读加入的笔记本列表)去猜——那个列表天生
    不包含"别人创建的公共知识库",策展人只有恰好是目标库 owner 时才能猜中真名,
    否则回退成截断 id。GET /promotion-queue 是 admin-only 端点(下面路由强制
    user.role == 'admin'),策展人天然需要看到任意用户创建的目标库真名——这与
    TestMountEdgeNamePrivacy 对普通挂载方遮蔽陌生库改名的隐私考量不冲突:那里
    针对的是"失去挂载资格后还能继续窥看库主改名"，这里是"策展人生来就该看见"。

    独立成一个新类、追加在文件末尾(而不是插进 TestPromotionTargetApi 中间)—
    —本文件顶部注释已经写明:不在 LINE_NUMBER_INSENSITIVE_FILES 白名单里,
    插入既有类中间会移动其后测试的行号,打破 test_repository_surface_manifest
    的按行号断言。"""

    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import create_app
        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        return TestClient(create_app())

    def _concept(self, repo, notebook_id):
        return repo._test_insert_object(
            notebook_id, "concept", {"name": "Gain", "definition": "增益"}
        )

    def test_target_base_name_resolves_for_same_owner(self, repo, monkeypatch):
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)

        promoted = client.post(f"/api/notebooks/{a.id}/knowledge/{oid}/promote")
        assert promoted.status_code == 201, promoted.text

        queue = client.get("/api/promotion-queue").json()
        row = next(c for c in queue if c["id"] == promoted.json()["id"])
        assert row["target_base_name"] == "base"

    def test_target_base_name_visible_even_when_curator_is_not_the_owner(
        self, repo, monkeypatch
    ):
        """审查原文的必测场景:目标库不属于当前(查看队列的)用户,策展人
        (auth_optional 下的种子 admin)仍必须拿到真名,而不是退化成截断 id。"""
        from app.core.request_context import reset_request_user, set_request_user
        client = self._client(monkeypatch)
        owner = repo.create_user("a00900011", "pw")  # 与晋升队列查看者(种子 admin)不同
        tok = set_request_user(owner)
        try:
            base = repo.create_notebook(NotebookCreate(name="第三方公共库"))
        finally:
            reset_request_user(tok)
        repo.mark_notebook_base(base.id)

        a = repo.create_notebook(NotebookCreate(name="a"))  # 种子 admin 自己的笔记本
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)

        promoted = client.post(f"/api/notebooks/{a.id}/knowledge/{oid}/promote")
        assert promoted.status_code == 201, promoted.text

        queue = client.get("/api/promotion-queue").json()
        row = next(c for c in queue if c["id"] == promoted.json()["id"])
        assert row["target_base_id"] == base.id
        assert row["target_base_name"] == "第三方公共库"

    def test_target_base_name_is_empty_string_for_legacy_empty_target(self, repo):
        """迁移 20 之前的存量候选行 target_base_id 是空串(同
        test_approve_promotion_with_empty_legacy_target_raises_actionable_error
        的构造方式)——target_base_name 必须优雅给空串,不能报错。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        oid = self._concept(repo, a.id)
        cand_id = "promo-legacy-empty-target-name"
        with repo._write() as db:
            db.execute(
                "INSERT INTO promotion_candidates "
                "(id, notebook_id, object_id, object_type, status, reason, "
                " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
                "VALUES (?,?,?,?,'proposed','','','',?,?,'')",
                (cand_id, a.id, oid, "concept",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        queue = repo.list_promotion_queue()
        row = next(c for c in queue if c["id"] == cand_id)
        assert row["target_base_id"] == ""
        assert row["target_base_name"] == ""

    def test_target_base_name_is_empty_string_for_dangling_target(self, repo):
        """target_base_id 指向 notebooks 表里没有对应行的 id(库已被删除,或
        候选行本身就带着一个野 id)——必须优雅回退空串,不能让读接口炸掉,
        也不能张冠李戴显示成别的库的名字。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        oid = self._concept(repo, a.id)
        cand_id = "promo-dangling-target"
        with repo._write() as db:
            db.execute(
                "INSERT INTO promotion_candidates "
                "(id, notebook_id, object_id, object_type, status, reason, "
                " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
                "VALUES (?,?,?,?,'proposed','','','',?,?,'nb-does-not-exist')",
                (cand_id, a.id, oid, "concept",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        queue = repo.list_promotion_queue()
        row = next(c for c in queue if c["id"] == cand_id)
        assert row["target_base_id"] == "nb-does-not-exist"
        assert row["target_base_name"] == ""


# ---------------------------------------------------------------------------
# 最终整支审查 BLOCKER 1(2026-07-19):失效挂载边渲染不出来,且让编辑表单永久
# 保存失败。追加在文件末尾(而非穿插进已有类)——本文件不在 LINE_NUMBER_INSENSITIVE_
# FILES 白名单里,插入既有类中间会移动其后测试的行号,打破 test_repository_surface_
# manifest 的按行号断言(见文件顶部注释与 TestPromotionQueueTargetBaseName 的同款说明)。
# ---------------------------------------------------------------------------


class TestPutBasesKeepsInactiveEdge:
    """`mountable_notebooks` 与 `MOUNT_VALID_EXPR` 是同一个表达式——失效边
    (active=False)按设计永远不会出现在 mountable 候选里。PUT 端点若只拿 mountable
    当白名单,前端原样重新提交"保留这条失效边不变"的 payload(page.tsx:2272 的
    mountedIds 来自全部 edges,含失效的;:2297 原样发出,不做任何前端过滤)就会被
    400 拒绝——用户在这条边恢复生效之前永远没法保存编辑表单,哪怕一个字段都没改。

    修法:allowed 放宽为 mountable ∪ 当前已挂载的 id(list_notebook_bases 返回的
    全部边,含失效的)。刻意不在前端过滤 payload——那会静默删掉设计上刻意保留、
    等对方重新发布后自动恢复的边(mount_sql.py 顶部注释:"挂载边不是授权凭证")。"""

    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import create_app
        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        return TestClient(create_app())

    def test_put_keeps_an_existing_inactive_edge_without_400(self, repo, monkeypatch):
        from app.core.request_context import reset_request_user, set_request_user
        client = self._client(monkeypatch)

        owner = repo.create_user("a00900021", "pw")  # 库主,与 a 的 owner(隐式 user-local)不同
        tok = set_request_user(owner)
        try:
            base = repo.create_notebook(NotebookCreate(name="base"))
        finally:
            reset_request_user(tok)
        repo.mark_notebook_base(base.id)

        a = repo.create_notebook(NotebookCreate(name="a"))
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        repo.set_notebook_personal(base.id)  # 库主降级 —— 边失效但不删除(不同 owner)

        assert client.get(f"/api/notebooks/{a.id}/mountable").json() == [], (
            "前置条件:失效边确实不在 mountable 候选里(否则测的就不是这个 bug)"
        )
        edges = client.get(f"/api/notebooks/{a.id}/bases").json()
        assert len(edges) == 1 and edges[0]["active"] is False, "前置条件:边还在,只是 active=False"

        # 复现前端 openNotebookEditor → saveNotebookEdit 的原样 payload:mountedIds 来自
        # 全部 edges(含失效的),原样发出去。
        resp = client.put(
            f"/api/notebooks/{a.id}/bases",
            json={"base_notebook_ids": [e["id"] for e in edges]},
        )
        assert resp.status_code == 200, (
            f"重新提交同一份挂载集合(含失效边)不该 400 —— 用户在这条边恢复生效之前"
            f"永远没法保存编辑表单; got {resp.status_code} {resp.text}"
        )
        assert [e["id"] for e in resp.json()] == [base.id]

    def test_put_still_rejects_a_new_id_even_when_an_inactive_edge_already_exists(
        self, repo, monkeypatch
    ):
        """回归防线:放宽不能变成"只要本笔记本有一条失效边,就任何新 id 都能挂"——
        allowed 的并集只应该纳入本笔记本自己已有的边,不能把"存在失效边"变成一个
        通用后门。用一个从未和 a 有过任何挂载关系、也不是公共库/同 owner 的 id 探针,
        与"保留既有失效边"一起提交,校验必须整体失败且零写入。"""
        from app.core.request_context import reset_request_user, set_request_user
        client = self._client(monkeypatch)

        owner = repo.create_user("a00900022", "pw")
        tok = set_request_user(owner)
        try:
            base = repo.create_notebook(NotebookCreate(name="base"))
        finally:
            reset_request_user(tok)
        repo.mark_notebook_base(base.id)

        a = repo.create_notebook(NotebookCreate(name="a"))
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        repo.set_notebook_personal(base.id)  # a 现在有一条失效边

        stranger = repo.create_user("a00900023", "pw")
        tok2 = set_request_user(stranger)
        try:
            theirs = repo.create_notebook(NotebookCreate(name="别人的私有库"))
        finally:
            reset_request_user(tok2)

        resp = client.put(
            f"/api/notebooks/{a.id}/bases",
            json={"base_notebook_ids": [base.id, theirs.id]},
        )
        assert resp.status_code == 400
        edges = repo.list_notebook_bases(a.id)
        assert [e["id"] for e in edges] == [base.id], (
            "校验失败必须整体零写入(不能因为 base.id 有效就把 payload 部分提交)"
        )
        assert edges[0]["active"] is False, "已有的失效边必须原样保留,不受这次失败请求影响"


# ---------------------------------------------------------------------------
# 最终整支审查 必办 4(2026-07-19):删除确认弹窗需要显示"N 个笔记本正在把它作为
# 参考库"(spec §6 原文)——ON DELETE CASCADE 会清边但用户此前无感知地失去参考库。
# 追加在文件末尾,理由同上面 BLOCKER 1 的说明。
# ---------------------------------------------------------------------------


class TestMountedByCount:
    """`mounted_by_count` —— 删除确认弹窗专用的挂载方计数,故意不按
    MOUNT_VALID_EXPR 过滤生效性(见 notebook_store.py 的方法 docstring):删除
    CASCADE 连失效边也一起清空,影响面理应包含它们。"""

    def test_counts_distinct_mounting_notebooks(self, repo):
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        a = repo.create_notebook(NotebookCreate(name="a"))
        b = repo.create_notebook(NotebookCreate(name="b"))
        assert repo.mounted_by_count(base.id) == 0

        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        assert repo.mounted_by_count(base.id) == 1

        repo.replace_notebook_bases(b.id, [base.id], "user-local")
        assert repo.mounted_by_count(base.id) == 2

        # 幂等:同一挂载方重复 PUT 同一集合不会把自己计两次(PRIMARY KEY 天然去重)。
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        assert repo.mounted_by_count(base.id) == 2

        repo.replace_notebook_bases(a.id, [], "user-local")  # a 取消挂载
        assert repo.mounted_by_count(base.id) == 1

    def test_counts_inactive_edges_too(self, repo):
        """降级后边失效(active=False),但 CASCADE 删除时这条边照样被清掉——
        计数不应该因为"当前不生效"就假装它不存在,用户仍会失去这条(暂时休眠的)
        参考关系恢复的可能性。需要 base 与 a 不同 owner(同 owner 会落进
        MOUNT_VALID_EXPR 的 same_owner 分支,降级也不会失效——见
        TestMountEdgeNamePrivacy 的同款前置条件)。"""
        from app.core.request_context import reset_request_user, set_request_user
        owner = repo.create_user("a00900033", "pw")
        tok = set_request_user(owner)
        try:
            base = repo.create_notebook(NotebookCreate(name="base"))
        finally:
            reset_request_user(tok)
        repo.mark_notebook_base(base.id)

        a = repo.create_notebook(NotebookCreate(name="a"))
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        repo.set_notebook_personal(base.id)  # 边失效,但没被删除

        assert repo.list_notebook_bases(a.id)[0]["active"] is False, "前置条件:边确实失效了"
        assert repo.mounted_by_count(base.id) == 1

    def test_http_endpoint_is_owner_only_and_matches_facade_count(self, two_users_client):
        """复用文件顶部已有的 two_users_client 夹具(两个真实注册用户),与
        TestApiUnauthorizedWrite 同款手法:notebook 一律经 HTTP 创建以获得正确的
        created_by,mark_notebook_base 直连 repo(绕开 admin-role 的 HTTP 校验,
        本测试测的是 mounted-by-count 端点而非发布流程本身)。"""
        c = two_users_client
        client = c["client"]
        from app.api.deps import repository
        repo_api = repository()

        base = client.post("/api/notebooks", headers=c["u1"], json={"name": "base"}).json()
        repo_api.mark_notebook_base(base["id"])

        resp = client.get(f"/api/notebooks/{base['id']}/mounted-by-count", headers=c["u1"])
        assert resp.status_code == 200
        assert resp.json() == {"count": 0}

        denied = client.get(f"/api/notebooks/{base['id']}/mounted-by-count", headers=c["u2"])
        assert denied.status_code == 404, "非 owner 不可见(与 DELETE 端点同一权限口径,404 不泄露存在性)"

        a = client.post("/api/notebooks", headers=c["u2"], json={"name": "u2 的笔记"}).json()
        client.put(
            f"/api/notebooks/{a['id']}/bases", headers=c["u2"],
            json={"base_notebook_ids": [base["id"]]},
        )
        resp2 = client.get(f"/api/notebooks/{base['id']}/mounted-by-count", headers=c["u1"])
        assert resp2.json() == {"count": 1}


# ---------------------------------------------------------------------------
# 最终整支审查 BLOCKER 2(2026-07-19):晋升审批从不复核目标库。propose 阶段的
# 挂载校验(_resolve_promotion_target/mounted_public_base_ids)只是快照——审批
# 可能发生在数天后,期间目标可能被降级/转让/删除。governance_store.py 的两处
# approve_*_in_transaction 此前只检查 target_base_id 非空,不复核目标行是否还
# 存在、还是不是 tier='base'。追加在文件末尾,理由同上面两个小节的说明。
# ---------------------------------------------------------------------------


class TestApprovePromotionRevalidatesTarget:
    """三个场景与审查的探针表一一对应:
      1) propose → 目标降级为 personal → approve:此前会把知识对象悄悄写进
         降级后的个人笔记本(数据破坏,静默,最严重)
      2) propose → 取消挂载(不降级、不删除)→ approve:仍应成功——挂载是
         检索关系,不是晋升授权,这条不能被误伤(回归防线)
      3) propose → 删除目标笔记本 → approve:此前会撞裸 sqlite3.IntegrityError
         (FOREIGN KEY constraint failed)一路冒泡成 500,而不是可操作的 400
    """

    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import create_app
        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        return TestClient(create_app())

    def _concept(self, repo, notebook_id, name="Gain"):
        return repo._test_insert_object(
            notebook_id, "concept", {"name": name, "definition": "增益"}
        )

    def test_approve_rejects_a_target_demoted_to_personal_after_propose(self, repo):
        """场景 1(最严重):裸 pytest.raises 分不出"是不是这条新校验"，用 match
        钉住错误文案必须点名"公共知识库",且用查库断言证明拒绝是硬拒绝——不是
        一边报错一边已经把对象写进去了。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid)  # 唯一挂载的公共库,自动选中 base

        repo.set_notebook_personal(base.id)  # propose 之后、approve 之前,目标被降级

        with pytest.raises(ValueError, match="公共知识库"):
            repo.approve_promotion(cand["id"])

        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 0, (
            "拒绝必须是硬拒绝 —— 不能一边报 ValueError 一边已经把知识对象写进了"
            "降级后的个人笔记本(Task 7 审查点名的失败态'可以把知识晋升进某人的"
            "个人笔记本'只修了 propose 侧一半,approve 侧必须同样挡住)"
        )

    def test_approve_still_succeeds_after_target_is_only_unmounted(self, repo):
        """场景 2(回归防线):取消挂载 ≠ 目标失效——base 依然是公共知识库、依然
        存在,只是 a 不再挂它。approve 不该因为"不再挂载"就拒绝,这是刻意保留的
        行为(挂载是检索关系,不是晋升授权),不能被这次修复误伤。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid)

        repo.replace_notebook_bases(a.id, [], "user-local")  # 取消挂载,不降级、不删除

        repo.approve_promotion(cand["id"])  # 不该抛——挂载状态不影响已经通过 propose 校验的目标
        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 1

    def test_approve_memory_promotion_raises_clean_error_when_target_deleted_after_propose(
        self, repo, monkeypatch
    ):
        """场景 3,走 Memory 晋升路径(与场景 1 的知识对象路径分摊覆盖两个
        approve_*_in_transaction)。断言必须是"干净的 ValueError",不是任何
        sqlite3.IntegrityError 的子类冒泡上来。"""
        import sqlite3
        client = self._client(monkeypatch)
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        candidate = repo.create_memory_candidate(
            a.id, "user-local", None, "final-review-blocker2-delete", "Claim",
            "A grounded claim", [], "reason", {}, [],
        )
        memory = repo.confirm_memory(candidate.id, "user-local")
        promoted = client.post(f"/api/memories/{memory.id}/promote")
        assert promoted.status_code == 201, promoted.text
        cand_id = promoted.json()["id"]

        repo.delete_notebook(base.id)  # propose 之后、approve 之前,目标被整个删除

        try:
            repo.approve_promotion(cand_id)
            raise AssertionError("目标已被删除,approve 必须拒绝,不能悄悄成功")
        except sqlite3.IntegrityError:
            raise AssertionError(
                "approve 撞到了裸 sqlite3.IntegrityError(FOREIGN KEY constraint "
                "failed)—— 必须在写入前用可操作的 ValueError 挡住,不能让约束冲突"
                "冒泡成 500"
            )
        except ValueError as exc:
            assert "公共知识库" in str(exc)


# ---------------------------------------------------------------------------
# codex 自动评审 PR#304 P2 #1(2026-07-19):require_live_promotion_target 被放在
# approve_*_in_transaction 的幂等早返回**之前**,于是"重试一个已经批准过的候选"
# 这条既有属性被打破——目标降级/存量 target_base_id='' 都会让重试报错而不是
# 返回既有结果。三个场景覆盖:批准后才降级(与 TestApprovePromotionRevalidatesTarget
# 场景 1 的"propose 之后降级"互补)、_migration_20 未回填的存量已批准行、以及
# 合并去重命中场景下重试仍能定位到既有对象(防止"定位既有对象"改用全局
# source_candidate_id 查找之后漏掉合并分支)。追加在文件末尾,理由同上面小节。
# ---------------------------------------------------------------------------


class TestApprovePromotionRetryDespiteTargetLifecycle:
    def _concept(self, repo, notebook_id, name="Gain"):
        return repo._test_insert_object(
            notebook_id, "concept", {"name": name, "definition": "增益"}
        )

    def test_approve_promotion_retry_succeeds_after_target_demoted_post_approval(
        self, repo
    ):
        """幂等重试不该被批准之后才发生的目标生命周期变化拖下水——这条和
        TestApprovePromotionRevalidatesTarget 场景 1 的唯一区别是降级发生在
        approve **之后**而不是 propose 之后。approve 第一次必须成功(彼时目标
        仍是公共知识库);第二次(重试)必须仍然成功,返回与第一次相同的既有
        结果,而不是被 require_live_promotion_target 拦下——那条校验是为了
        堵"目标在 propose 和 approve 之间失效"这个洞开的,不该殃及"已经批准
        完了,只是又被调用了一次"的重试。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        oid = self._concept(repo, a.id)
        cand = repo.propose_promotion(a.id, oid)
        first = repo.approve_promotion(cand["id"])

        repo.set_notebook_personal(base.id)  # approve 之后才降级(与场景 1 不同)

        second = repo.approve_promotion(cand["id"])  # 重试:不该抛
        assert second["base_object_id"] == first["base_object_id"]
        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 1, "重试不该再写一个重复对象"

    def test_approve_promotion_retry_succeeds_for_legacy_approved_row_with_empty_target(
        self, repo
    ):
        """_migration_20 只是 ALTER ... ADD COLUMN target_base_id DEFAULT '',
        不会回填迁移前已经 approved 的存量候选——那些行的 target_base_id 就是
        空串,且没有任何补救 CLI 覆盖它们(pending_promotion_targets 只挑
        status IN ('proposed','under_review') 的行,approved 行不在补救范围
        内)。这类行重试批准时必须仍能定位到当年已经晋升出去的那个基准对象,
        而不是被"目标缺失"的校验拦下——那条校验的用意是挡住"真正要写数据、
        但没有目标"的路径,不是挡"已经写完了、只是不记得写去哪"的路径。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        oid = self._concept(repo, a.id)
        cand_id = "promo-legacy-approved-empty-target"
        base_object_id = "ko-legacy-promoted-empty-target"
        now = "2020-01-01T00:00:00Z"
        with repo._write() as db:
            db.execute(
                "INSERT INTO promotion_candidates "
                "(id, notebook_id, object_id, object_type, status, reason, "
                " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
                "VALUES (?,?,?,?,'approved','','curator','',?,?,'')",
                (cand_id, a.id, oid, "concept", now, now),
            )
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,"
                "source_candidate_id,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved','',?,'[]',?,'',?,?)",
                (
                    base_object_id, base.id,
                    '{"name": "Legacy Gain", "definition": "遗留增益"}',
                    cand_id, now, now,
                ),
            )

        result = repo.approve_promotion(cand_id)  # 重试(建库时就是 approved):不该抛

        assert result["base_object_id"] == base_object_id
        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 1, "重试不该再写一个重复对象"

    def test_approve_promotion_retry_after_dedup_merge_still_resolves_the_matched_object(
        self, repo
    ):
        """合并去重命中(base 已有等价对象,approve 不新建)时,新对象不会被打上
        source_candidate_id——这条线索只活在 promotion_candidates.base_match_id
        里。定位既有结果的查找不再要求 target_base_id 非空/有效(否则就是本类
        另外两个用例在修的那个 bug),但换成全局 source_candidate_id 查找之后,
        合并场景必须仍然只靠 base_match_id 就能找回原来那个对象,不能被漏掉。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        existing = repo._test_insert_object(
            base.id, "concept", {"name": "Gain", "definition": "增益"}
        )
        oid = self._concept(repo, a.id)  # 与 existing 同名同定义 → 命中去重
        cand = repo.propose_promotion(a.id, oid)
        first = repo.approve_promotion(cand["id"])
        assert first["base_object_id"] == existing
        assert first["merged_into"] == existing

        second = repo.approve_promotion(cand["id"])  # 重试

        assert second["base_object_id"] == existing
        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 1, "去重命中场景下 base 里应该始终只有那一个既有对象"

    def test_approve_memory_promotion_retry_succeeds_for_legacy_approved_row_with_empty_target(
        self, repo
    ):
        """Memory 侧对称覆盖:approve_memory_promotion_in_transaction 内部的
        幂等分支同样被放在了 require_live_promotion_target 之后,顺序问题与
        approve_promotion_in_transaction 完全一致。正常的重试路径已经被
        knowledge_governance.approve_promotion 的 facade 级
        "status=='approved' and existing_ids" 短路挡在前面,根本走不到这个
        函数内部,而这条短路以及更上层的 Memory 专属校验
        (validate_pinned_promotion_on 要求 promotion_state=='proposed')都与
        本条要修的顺序 bug 无关——真去拼一个完整、状态自洽的 Memory 反而会
        撞上那些无关校验,永远走不到这里。所以直接调 governance_store 这一层:
        这个函数本身只读 promotion_candidates/写 knowledge_objects,
        candidates/evidence 都是调用方传入的参数,不碰 memory_items/
        memory_provenance,绕开整个 Memory 子系统就能精确复现审查点名的
        那个函数。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        cand_id = "promo-legacy-memory-empty-target"
        base_object_id = "ko-legacy-memory-promoted"
        now = "2020-01-01T00:00:00Z"
        with repo._write() as db:
            db.execute(
                "INSERT INTO promotion_candidates "
                "(id, notebook_id, object_id, object_type, status, reason, "
                " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
                "VALUES (?,?,'memory-legacy-oid','memory','approved','','curator',"
                "'',?,?,'')",
                (cand_id, a.id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,"
                "source_candidate_id,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved','',?,'[]',?,'',?,?)",
                (
                    base_object_id, base.id,
                    '{"name": "Legacy Memory Concept", "definition": "遗留"}',
                    cand_id, now, now,
                ),
            )
            governance_store = repo._runtime.knowledge_governance.governance_store
            result = governance_store.approve_memory_promotion_in_transaction(
                db, cand_id, [], [], "curator", now
            )  # 重试(建库时就是 approved):不该抛

        assert result["base_object_ids"] == [base_object_id]
        assert result["base_notebook_id"] == base.id
        with repo._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
                (base.id,),
            ).fetchone()["c"]
        assert count == 1, "重试不该再写一个重复对象"


# ---------------------------------------------------------------------------
# codex 自动评审 PR#304 第 3 轮 P2 #1(2026-07-19):MOUNT_VALID_EXPR 只判 tier 与
# owner,没有排除 status='copying' 的深拷贝哨兵状态。notebook_sharing.copy_notebook
# 先插入一个 status='copying' 的目标笔记本,再跨多个事务往里灌数据(见该模块
# docstring);因为「同 owner」这一支落库那一刻就满足,另一个请求能在拷贝完成前
# 把它当参考库挂上并从中检索,读到跨事务写入中途的半成品——与本仓库既有的
# 「copying 状态尚不可用」不变量(notebook_catalog.NotebookSummaryQuery.get /
# notebook_store.get_row 等处同款排除,见 mount_sql.py 顶部本次同步补写的说明)
# 矛盾。追加在文件末尾,理由同前面几节:本文件不在 LINE_NUMBER_INSENSITIVE_FILES
# 白名单里,插入既有类中间会移动其后测试的行号。
# ---------------------------------------------------------------------------


class TestCopyingNotebookExcludedFromMounting:
    """status='copying' 必须挡住 MOUNT_VALID_EXPR 的两个 OR 分支——不管是靠
    tier='base' 满足还是靠同 owner 满足,拷贝未完成之前都不该生效。"""

    def test_copying_notebook_excluded_from_mountable_candidates(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        mid_copy = repo.create_notebook(NotebookCreate(name="拷贝中的库"))
        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET status='copying' WHERE id=?", (mid_copy.id,)
            )
        got = {n["id"] for n in repo.mountable_notebooks(a.id)}
        assert mid_copy.id not in got, (
            "深拷贝哨兵状态的库不该出现在可挂候选里(同 owner 分支)"
        )

    def test_copying_base_tier_notebook_excluded_from_mountable_candidates(self, repo):
        """tier='base' 分支同理必须被 status 挡住——当前 copy_notebook 只会产出
        tier='personal' 的拷贝目标(notebook_sharing.py 硬编码 tier="personal"),
        谓词本身不该依赖这个事实性前提才安全,两个 OR 分支都要独立测,不能只测
        更常见的同 owner 那支。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET status='copying' WHERE id=?", (base.id,)
            )
        got = {n["id"] for n in repo.mountable_notebooks(a.id)}
        assert base.id not in got, (
            "正在拷贝的公共知识库同样不该出现在可挂候选里(tier='base' 分支)"
        )

    def test_copying_notebook_does_not_participate_even_with_existing_edge(self, repo):
        """核心安全断言,比「可挂候选」更贴近真实攻击面:候选列表只挡得住*新*
        挂载,挡不住*已有*边的检索参与。有效性是解析时的实时判定(mount_sql.py
        模块文档),不问边何时插入的——直接造一条指向 copying 库的边,验证参与集
        解析依然把它挡在外面。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        mid_copy = repo.create_notebook(NotebookCreate(name="拷贝中的库"))
        with repo._write() as db:
            db.execute(
                "INSERT INTO notebook_bases"
                "(notebook_id, base_notebook_id, created_at, created_by)"
                " VALUES (?,?,?,?)",
                (a.id, mid_copy.id, "2026-07-19T00:00:00Z", "user-local"),
            )
            db.execute(
                "UPDATE notebooks SET status='copying' WHERE id=?", (mid_copy.id,)
            )
        with repo._connect() as db:
            ids = repo._runtime.notebook_store.participant_ids(db, a.id)
        assert ids == [a.id], "status='copying' 的库即便已有挂载边也不该进入参与集"

    def test_copying_notebook_becomes_eligible_once_copy_completes(self, repo):
        """对照组,呼应既有 test_demoted_public_base_is_skipped_then_restored 的
        「临时失效、条件恢复后自动生效」设计:copying → 非 copying 翻转后,已有的
        边应该立刻(无需重新挂载)重新参与解析,证明这不是把边写死拒绝,而是
        MOUNT_VALID_EXPR 的实时判定。"""
        a = repo.create_notebook(NotebookCreate(name="a"))
        mid_copy = repo.create_notebook(NotebookCreate(name="拷贝中的库"))
        with repo._write() as db:
            db.execute(
                "INSERT INTO notebook_bases"
                "(notebook_id, base_notebook_id, created_at, created_by)"
                " VALUES (?,?,?,?)",
                (a.id, mid_copy.id, "2026-07-19T00:00:00Z", "user-local"),
            )
            db.execute(
                "UPDATE notebooks SET status='copying' WHERE id=?", (mid_copy.id,)
            )
        with repo._connect() as db:
            assert repo._runtime.notebook_store.participant_ids(db, a.id) == [a.id]

        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET status='draft' WHERE id=?", (mid_copy.id,)
            )
        with repo._connect() as db:
            got = set(repo._runtime.notebook_store.participant_ids(db, a.id))
        assert got == {a.id, mid_copy.id}, "拷贝完成(status 翻回非 copying)后应立即恢复参与,无需重新挂载"
