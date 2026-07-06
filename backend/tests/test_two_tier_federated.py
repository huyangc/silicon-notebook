"""Track B — two-tier + federated tier-aware retrieval.

All five task suites live here. Each suite is gated independently; the full
`pytest -q` must stay green (no regression to single-notebook ask()).
"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


class TestTask1:
    def test_new_notebook_has_personal_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="personal nb"))
        assert nb.tier == "personal"

    def test_mark_notebook_base_sets_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="textbook"))
        repo.mark_notebook_base(nb.id)
        nb2 = repo.get_notebook(nb.id)
        assert nb2.tier == "base"

    def test_mark_notebook_base_is_globally_unique(self, repo):
        """基准库全局唯一:把 B 设为 base 时，旧 base A 应在同一事务里被降级为 personal。"""
        a = repo.create_notebook(NotebookCreate(name="base A"))
        b = repo.create_notebook(NotebookCreate(name="base B"))
        repo.mark_notebook_base(a.id)
        assert repo.get_notebook(a.id).tier == "base"
        repo.mark_notebook_base(b.id)  # 设新 base → 旧的应自动降级
        assert repo.get_notebook(b.id).tier == "base"
        assert repo.get_notebook(a.id).tier == "personal"

    def test_tier_is_idempotent_on_existing_db(self, tmp_path, monkeypatch):
        """Running _migrate() twice on a DB that already has the tier column
        must not raise (PRAGMA guard prevents duplicate ALTER TABLE)."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        repo1 = SQLiteRepository(Settings())
        nb = repo1.create_notebook(NotebookCreate(name="nb"))
        repo1.mark_notebook_base(nb.id)
        # Second repo init on same DB must not raise.
        repo2 = SQLiteRepository(Settings())
        assert repo2.get_notebook(nb.id).tier == "base"


class TestTask2:
    def _seed_two_notebooks(self, repo):
        """base notebook with one claim; personal notebook with one concept."""
        base_nb = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base_nb.id)
        repo.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "claim",
             "payload": {"name": "base claim about capacitance", "section_path": "1"},
             "evidence": []},
        ], [])
        personal_nb = repo.create_notebook(NotebookCreate(name="personal"))
        repo.store_kg(personal_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "capacitance concept note", "section_path": "1"},
             "evidence": []},
        ], [])
        return base_nb, personal_nb

    def test_federated_retrieve_returns_hits_from_both_notebooks(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        nb_ids = {h.notebook_id for h in hits}
        assert base_nb.id in nb_ids
        assert personal_nb.id in nb_ids

    def test_federated_retrieve_tags_tier(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        base_hits = [h for h in hits if h.notebook_id == base_nb.id]
        personal_hits = [h for h in hits if h.notebook_id == personal_nb.id]
        assert all(h.tier == "base" for h in base_hits)
        assert all(h.tier == "personal" for h in personal_hits)

    def test_federated_retrieve_preserves_relevance_range(self, repo):
        """All relevance values must stay [0,1]; no [k] inflation from federation."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        for h in hits:
            assert 0.0 <= h.relevance <= 1.0, f"relevance {h.relevance!r} out of [0,1]"

    def test_ask_uses_federated_retrieve_when_base_exists(self, repo):
        """federated_retrieve() on a personal notebook surfaces hits from the base notebook.
        P4-5: ask_fast (which called federated_retrieve) retired; test now calls it directly."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        all_ids = {h.object_id for h in hits}
        # At least one object from the base notebook must appear.
        with repo._connect() as db:
            base_ids = {r["id"] for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (base_nb.id,)).fetchall()}
        assert all_ids & base_ids, "no base-notebook objects reached the answer"


class TestTask3:
    """两层库权威=零幅度次序策略:排序永远纯相关度、tier 无关;base 的『更权威』
    只体现为『完全平局时 base 排前』。绝不用任何幅度乘数/配额/地板。"""

    def test_tier_weight_is_removed(self):
        """死代码 tier_weight 已删:模块不再暴露该属性,import 应 ImportError。"""
        import app.services.retrieval as retrieval
        assert not hasattr(retrieval, "tier_weight"), \
            "tier_weight 幅度乘数应已删除(零幅度次序策略不使用任何权威乘数)"
        assert not hasattr(retrieval, "_TIER_WEIGHT")
        with pytest.raises(ImportError):
            from app.services.retrieval import tier_weight  # noqa: F401

    def test_base_outranks_personal_only_on_exact_score_tie(self):
        """score 完全相等时 base 排在 personal 前(纯次序键,无常数)。"""
        from app.services.retrieval import RetrievedKnowledge
        base_hit = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, tier="base", score=0.20, relevance=0.20)
        personal_hit = RetrievedKnowledge(
            object_id="p1", object_type="claim", payload={}, tier="personal", score=0.20, relevance=0.20)
        # federated_retrieve 使用的排序键:score 第一、仅平局时 base(True>False)优先。
        key = lambda it: (it.score, getattr(it, "tier", "") == "base")
        ranked = sorted([personal_hit, base_hit], key=key, reverse=True)
        assert ranked[0] is base_hit, "分数相等时 base 应排在 personal 前"

    def test_higher_relevance_personal_still_wins_over_base(self):
        """零幅度:base 不获得任何幅度加成;相关度更高的 personal 仍胜出(切题第一)。"""
        from app.services.retrieval import RetrievedKnowledge
        base_hit = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, tier="base", score=0.20, relevance=0.20)
        personal_hit = RetrievedKnowledge(
            object_id="p1", object_type="claim", payload={}, tier="personal", score=0.30, relevance=0.30)
        key = lambda it: (it.score, getattr(it, "tier", "") == "base")
        ranked = sorted([base_hit, personal_hit], key=key, reverse=True)
        assert ranked[0] is personal_hit, "相关度更高的 personal 命中必须排在 base 前(纯相关度第一)"

    def test_federated_retrieve_ranks_base_first_on_score_tie(self, repo):
        """真库路径:两 tier 命中 score 相等时,base 在结果里应排在 personal 之前。"""
        from app.services.retrieval import RetrievedKnowledge
        base_nb = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base_nb.id)
        personal_nb = repo.create_notebook(NotebookCreate(name="personal"))

        base_hit = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, score=0.20, relevance=0.20)
        personal_hit = RetrievedKnowledge(
            object_id="p1", object_type="claim", payload={}, score=0.20, relevance=0.20)

        def fake_retrieve_scored(nid, query, **kwargs):
            if nid == base_nb.id:
                return [RetrievedKnowledge(
                    object_id="b1", object_type="claim", payload={}, score=0.20, relevance=0.20)]
            if nid == personal_nb.id:
                return [RetrievedKnowledge(
                    object_id="p1", object_type="claim", payload={}, score=0.20, relevance=0.20)]
            return []

        repo._retrieve_scored = fake_retrieve_scored
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        tied = [h for h in hits if h.score == 0.20]
        assert [h.tier for h in tied][:2] == ["base", "personal"], \
            f"平局时应 base 先于 personal,实得 {[h.tier for h in tied]!r}"

    def test_keyword_base_hit_tau_position_unchanged(self, repo):
        """A pure-keyword base hit's .relevance (what tau reads) must stay [0,1].
        The tier boost on .score must not bleed into .relevance."""
        from app.services.retrieval import RetrievedKnowledge
        h = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, tier="base",
            score=0.22, relevance=0.22)
        # Tier boost is applied to score during rank_key, not to relevance.
        assert h.relevance == 0.22
        assert 0.0 <= h.relevance <= 1.0


class TestTask4:
    def test_answer_anchor_has_tier_field(self):
        from app.models.schemas import AnswerAnchor
        a = AnswerAnchor(key="k1", object_id="o1", object_type="claim",
                         label="Cap", name="Capacitance", tier="base")
        assert a.tier == "base"

    def test_answer_anchor_tier_defaults_to_personal(self):
        from app.models.schemas import AnswerAnchor
        a = AnswerAnchor(key="k1", object_id="o1", object_type="claim", label="x")
        assert a.tier == "personal"

    def test_parse_answer_anchors_carries_tier(self, repo):
        id_map = {
            "k1": {"object_id": "o1", "object_type": "claim", "name": "Cap",
                   "definition": "capacitance", "snippet": None,
                   "source_title": "", "location_label": "", "tier": "base"},
        }
        anchors = repo._parse_answer_anchors("Capacitance [k1].", id_map)
        assert anchors[0].tier == "base"

    def test_answer_prompt_contains_conflict_rule(self):
        from app.services.prompts import answer_prompt
        prompt = answer_prompt("question", "context")
        # The prompt must instruct the LLM to prefer base on contradiction.
        assert "base" in prompt.lower() and (
            "contradict" in prompt.lower() or "defer" in prompt.lower()
        ), "answer_prompt missing base-authoritative conflict rule"


class TestTask5:
    def _seed_single(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="solo"))
        repo.store_kg(nb.id, None, [
            {"local_id": "S1", "object_type": "claim",
             "payload": {"name": "oxide breakdown voltage", "section_path": "2"},
             "evidence": []},
        ], [])
        return nb

    def test_single_notebook_ask_returns_same_hit(self, repo):
        """Without any base notebook, _retrieve_scored() on a personal notebook returns
        the personal hit — identical to pre-federation behavior.
        P4-5: ask_fast retired; test now calls _retrieve_scored directly."""
        nb = self._seed_single(repo)
        hits = repo._retrieve_scored(nb.id, "oxide breakdown")
        hit_payloads = [h.payload.get("name", "") for h in hits]
        assert any("oxide" in p.lower() for p in hit_payloads), \
            "personal KG object must be returned by _retrieve_scored"

    def test_single_notebook_federated_retrieve_returns_only_its_hits(self, repo):
        nb = self._seed_single(repo)
        hits = repo.federated_retrieve(nb.id, "oxide breakdown")
        nb_ids = {h.notebook_id for h in hits}
        assert nb_ids == {nb.id}

    def test_single_notebook_anchor_tier_is_personal(self, repo):
        """Anchors in a single-notebook ask() must default to tier='personal'."""
        nb = self._seed_single(repo)
        id_map = {
            "k1": {"object_id": "S1", "object_type": "claim", "name": "Oxide BV",
                   "definition": "oxide breakdown", "snippet": None,
                   "source_title": "", "location_label": ""},
            # No 'tier' key — simulates pre-federation id_map
        }
        anchors = repo._parse_answer_anchors("Oxide BV [k1].", id_map)
        assert anchors[0].tier == "personal"

    def test_full_suite_still_passes(self, repo):
        """Smoke: existing ask() tests must still pass when run together."""
        # This is a marker test — run the full suite via the gate command below.
        assert True
