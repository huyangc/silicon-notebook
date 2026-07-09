# P2 共提桥接层（mention bridge）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 claim 文本确定性提取「claim→跨源概念」mention 边与概念共提对，替换 `expand_community` 的兄弟数据源并给 PPR 图补跨文档导通——治对比题坍缩，零 LLM、零重抽。

**Architecture:** 两张派生表（`mention_edges`/`concept_comentions`，`_migration_9`）随 rebuild 重算（`mention_seq` 闸，fail-open）；匹配用 rebuild 作用域临时 trigram FTS + Latin `\b` 后校验 + DF 上限门；消费两路——`sibling_peers`（先共提后社区回退）与三个图构建点的 extra edges。Spec: `docs/superpowers/specs/2026-07-09-kg-mention-bridge-design.md`（决策以 spec 为准）。

**Tech Stack:** Python/SQLite(FTS5 trigram)/pytest。测试解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。前端零改动。

## Global Constraints

- **零 LLM/embed**：全链路确定性；rebuild 新增一次 FTS 建表 + 每别名一次 MATCH（seq 闸防重复）。
- **schema-migration-convention**：`_migration_1` baseline 双写 + `_migration_9` + `SCHEMA_VERSION = 9`；已部署 v8 库升级测试必须有。
- **pydantic-env-alias 坑**：新 Settings 字段必须用 `validation_alias="ENV_NAME"`（`Field(env=...)` 无效）。
- **匹配质量门**：Latin 别名 len≥4 且命中做 `\b` 后校验；CJK 别名 len≥3；括号缩写 3-8 位字母数字；DF 门 `mention_alias_df_cap`（命中 claim 占比默认 0.02）丢弃泛词并事件计数（不静默）。
- **fail-open**：rebuild 两接线点 try/except + 事件 `mention_bridge_rebuild_failed`；`sibling_peers` 任何异常回退 `community_peers`。
- **派生隔离**：mention 边不进 knowledge_relations/边审查/viz。
- **测试运行**：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest <path> -q`；提交 `feat(kg): 中文摘要`，每 Task 一个 commit；不碰真实 `.local/`。

---

### Task 1: Schema——两张表 + `mention_seq` 列（`_migration_9`, SCHEMA_VERSION=9）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`SCHEMA_VERSION` ~:252 → 9；`_migration_1` 内 `canonical_relations` CREATE 之后 + `_add_column_if_missing` 块 `canonical_rel_seq` 行之后；`_migration_8` 之后新增 `_migration_9`）
- Test: `backend/tests/test_mention_bridge.py`（新建）

**Interfaces:**
- Produces: 表 `mention_edges(notebook_id, claim_object_id, concept_canonical_id, matched_alias, PK(nb,claim,concept))`、`concept_comentions(notebook_id, canonical_a, canonical_b, bridge_claims, PK(nb,a,b))`；`unified_kg_state.mention_seq INTEGER NOT NULL DEFAULT -1`。

- [ ] **Step 1: 失败测试**（新建 `backend/tests/test_mention_bridge.py`；fixture 抄 `backend/tests/test_canonical_relations.py` 的 `repo`/`_mk_src` 帮手——那个文件是本特性的姊妹先例）

```python
import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_mention_bridge_tables(repo):
    assert {"notebook_id", "claim_object_id", "concept_canonical_id", "matched_alias"} <= _cols(repo, "mention_edges")
    assert {"notebook_id", "canonical_a", "canonical_b", "bridge_claims"} <= _cols(repo, "concept_comentions")
    assert "mention_seq" in _cols(repo, "unified_kg_state")


def test_deployed_v8_db_gets_backfilled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE mention_edges")
    raw.execute("DROP TABLE concept_comentions")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN mention_seq")
    raw.execute("PRAGMA user_version = 8")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())
    assert "claim_object_id" in _cols(r2, "mention_edges")
    assert "bridge_claims" in _cols(r2, "concept_comentions")
    assert "mention_seq" in _cols(r2, "unified_kg_state")


def test_schema_version_is_9():
    assert SCHEMA_VERSION == 9
```

- [ ] **Step 2: 确认失败** — Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mention_bridge.py -q` → 3 FAIL。

- [ ] **Step 3: 实现** — 完全照 `canonical_relations`/`_migration_8` 的两层写法（该迁移就在文件里，作范本）：`_migration_1` baseline 加两个 CREATE + `_add_column_if_missing(db, "unified_kg_state", "mention_seq", "INTEGER NOT NULL DEFAULT -1")`；新增 `_migration_9`（CREATE ×2 + 守卫 ALTER，`self._connect()`）；`SCHEMA_VERSION = 9`。若 `tests/test_sqlite_write_optimization.py` 的 ALLOW-list 与 `tests/fixtures/schema_contract.txt` 黄金文件因此变红，按 `_migration_8` 当时的先例机械更新并入本 commit。

- [ ] **Step 4: 确认通过** — Run: `... -m pytest tests/test_mention_bridge.py tests/test_legacy_db_compat.py tests/test_canonical_relations.py -q` → 全 PASS。

- [ ] **Step 5: Commit** — `git commit -m "feat(kg): mention_edges/concept_comentions 表 + mention_seq(_migration_9, SCHEMA_VERSION=9)"`

---

### Task 2: 匹配核（纯函数模块 `kg/mention_scan.py`）

**Files:**
- Create: `backend/app/services/kg/mention_scan.py`
- Test: `backend/tests/kg/test_mention_scan.py`（新建）

**Interfaces:**
- Produces（Task 3 依赖，签名精确）:
  - `build_alias_table(clusters: list[tuple[str, str]], *, latin_min: int = 4, cjk_min: int = 3) -> dict[str, set[str]]` — 输入 `[(canonical_id, canonical_name)]`，输出 `{canonical_id: {alias_lower}}`；别名 = 全名 + 去括号头名 + 括号缩写（3-8 位字母数字），按 Latin/CJK 长度门过滤。
  - `is_latin(alias: str) -> bool` — 全 ASCII 判定。
  - `boundary_hit(alias: str, text_lower: str) -> bool` — Latin 别名用 `\b` regex 校验；CJK 别名用子串。

- [ ] **Step 1: 失败测试**

```python
from app.services.kg.mention_scan import build_alias_table, boundary_hit, is_latin


def test_alias_table_full_head_acronym():
    at = build_alias_table([("K-gqa", "Grouped-query attention (GQA)")])
    assert {"grouped-query attention (gqa)", "grouped-query attention", "gqa"} <= at["K-gqa"]


def test_alias_length_gates():
    at = build_alias_table([("K-a", "RoPE"), ("K-b", "V2"), ("K-c", "铸币平价"), ("K-d", "汇率")])
    assert "rope" in at.get("K-a", set())          # Latin 全名 len==4 通过
    assert at.get("K-b", set()) == set()            # 全名 len<4 且非括号缩写 → 不入表
    assert "铸币平价" in at.get("K-c", set())        # CJK len>=3
    assert at.get("K-d", set()) == set()            # CJK len==2 放弃


def test_acronym_bypasses_latin_min():
    # 括号缩写(3-8位,来自显式 "(ACR)" 模式,precision 高)绕过 latin_min=4 的长度门:
    # GQA/MQA/SFT 这类 3 位缩写正是共提桥最有价值的别名。
    at = build_alias_table([("K-gqa", "Grouped-query attention (GQA)")])
    assert "gqa" in at["K-gqa"]


def test_boundary_hit_latin_word_boundary():
    assert boundary_hit("rope", "we use rope embeddings")
    assert not boundary_hit("rope", "in europe the model")   # 子串不算
    assert boundary_hit("gqa", "gqa reduces kv cache")


def test_boundary_hit_cjk_substring():
    assert boundary_hit("铸币平价", "在金本位下铸币平价决定汇率")
    assert is_latin("rope") and not is_latin("铸币平价")
```

- [ ] **Step 2: 确认失败** — Run: `... -m pytest tests/kg/test_mention_scan.py -q` → ImportError。

- [ ] **Step 3: 实现**

```python
"""Mention-bridge 匹配核:别名表构建 + 命中校验。纯函数、零 IO。

trigram FTS 召回候选后,Latin 别名必须过 \\b 词边界后校验(trigram 是子串
语义,rope 会命中 europe);CJK 无词边界概念,子串即命中。"""
from __future__ import annotations
import re
from typing import Dict, List, Set, Tuple

_PAREN_ACRONYM_RE = re.compile(r"^(.*\S)\s*\(([^)]+)\)\s*$")
_ACR_RE = re.compile(r"^[A-Za-z0-9]{3,8}$")
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def is_latin(alias: str) -> bool:
    return bool(_ASCII_RE.match(alias))


def _long_enough(alias: str, latin_min: int, cjk_min: int) -> bool:
    return len(alias) >= (latin_min if is_latin(alias) else cjk_min)


def build_alias_table(clusters: List[Tuple[str, str]], *, latin_min: int = 4,
                      cjk_min: int = 3) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for cid, name in clusters:
        nm = (name or "").strip()
        gated, exempt = set(), set()
        if nm:
            gated.add(nm.lower())
            m = _PAREN_ACRONYM_RE.match(nm)
            if m:
                head, acr = m.group(1).strip(), m.group(2).strip()
                gated.add(head.lower())
                if _ACR_RE.match(acr):
                    # 括号缩写绕过 latin_min:显式 "(ACR)" 模式 precision 高,
                    # GQA/MQA/SFT 等 3 位缩写是共提桥最有价值的别名;
                    # 长度下限由 _ACR_RE 的 {3,8} 承担(trigram 最短查询=3)。
                    exempt.add(acr.lower())
        kept = {a for a in gated if _long_enough(a, latin_min, cjk_min)} | exempt
        if kept:
            out[cid] = kept
    return out


def boundary_hit(alias: str, text_lower: str) -> bool:
    if is_latin(alias):
        return re.search(r"\b" + re.escape(alias) + r"\b", text_lower) is not None
    return alias in text_lower
```

注意 `test_alias_length_gates` 期待 `at.get("K-b", set()) == set()`——空集合的 canonical 不入 dict（实现里 `if kept` 已保证，用 `.get(..., set())` 断言）。

- [ ] **Step 4: 确认通过** — Run: `... -m pytest tests/kg/test_mention_scan.py -q` → 全 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(kg): mention_scan 匹配核——别名表(全名/头名/缩写)+词边界后校验(纯函数)"`

---

### Task 3: 构建器 `rebuild_mention_bridge` + rebuild 接线 + Settings 旋钮

**Files:**
- Modify: `backend/app/core/config.py`（community_layer_enabled 附近加 4 个旋钮）
- Modify: `backend/app/services/sqlite_repository.py`（新方法放 `rebuild_canonical_relations` 之后；接线两处与 `rebuild_canonical_relations` 并排——跳过分支 + 全量尾部 force=True）
- Test: `backend/tests/test_mention_bridge.py`（追加）

**Interfaces:**
- Consumes: Task 1 表、Task 2 `build_alias_table`/`boundary_hit`/`is_latin`。
- Produces: `rebuild_mention_bridge(notebook_id: str, force: bool = False) -> int`（返回 mention_edges 行数）。Settings:

```python
    # 共提桥接层(P2):claim 文本确定性提取 mention 边/共提对,治对比题坍缩。默认开(零 LLM,有界)。
    mention_bridge_enabled: bool = Field(True, validation_alias="MENTION_BRIDGE_ENABLED")
    # 别名 DF 双门:命中 claim 数 > max(floor, cap×总claims) 判泛词整体丢弃(如 "model"),事件计数。
    # 比例门管大库("model" 8% vs GQA 0.3%);绝对下限 floor 防小库误杀真实高频概念
    # (200 claims 的库里 GQA 命中 10 条是正常信号,2%×200=4 会误杀)。
    mention_alias_df_cap: float = Field(0.02, validation_alias="MENTION_ALIAS_DF_CAP")
    mention_alias_df_floor: int = Field(20, validation_alias="MENTION_ALIAS_DF_FLOOR")
    # mention 边在 PPR 图中的权重(与 variant 边同量级)。
    mention_edge_weight: float = Field(0.5, validation_alias="MENTION_EDGE_WEIGHT")
    # sibling_peers 的最小桥 claim 数(低于此不算同类伙伴)。
    sibling_min_bridge: int = Field(2, validation_alias="SIBLING_MIN_BRIDGE")
```

- [ ] **Step 1: 失败测试**（追加；`_mk_src` 帮手从 `test_canonical_relations.py` 抄）

```python
def _seed_bridge_nb(repo):
    """3 源:GQA/MQA 各跨 2 源(跨源簇);2 条对比 claim 同提两者;1 条只提 GQA。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for i in (1, 2, 3):
        _mk_src(repo, nb.id, f"s{i}")
    objs = lambda src, names, t="concept": [
        {"local_id": f"{src}-{j}", "object_type": t,
         "payload": {"name": n, "section_path": "1"}, "evidence": []}
        for j, n in enumerate(names)]
    repo.store_kg(nb.id, "s1", objs("s1", ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)"]), [])
    repo.store_kg(nb.id, "s2", objs("s2", ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)"]), [])
    repo.store_kg(nb.id, "s3", objs("s3", [
        "GQA uses fewer KV heads than MQA while keeping quality.",
        "GQA halves KV cache compared with MQA in practice.",
        "GQA is adopted by many recent models."], t="claim"), [])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_rebuild_extracts_mention_edges_and_comentions(repo):
    nb = _seed_bridge_nb(repo)
    with repo._connect() as db:
        me = db.execute("SELECT COUNT(*) c FROM mention_edges WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
        cm = db.execute("SELECT * FROM concept_comentions WHERE notebook_id=?", (nb.id,)).fetchall()
    assert me >= 5                       # 3条claim×命中(2+2+1)
    assert len(cm) == 1                  # GQA↔MQA 一对
    assert cm[0]["bridge_claims"] == 2   # 两条对比claim
    a, b = sorted(("K-grouped-query attention", "K-multi-query attention"))
    assert (cm[0]["canonical_a"], cm[0]["canonical_b"]) == (a, b)


def test_df_cap_drops_generic_alias(repo):
    nb = _seed_bridge_nb(repo)
    repo.settings.mention_alias_df_floor = 0      # 关掉绝对下限,让比例门生效
    repo.settings.mention_alias_df_cap = 0.0001   # 人为压到全部超限
    assert repo.rebuild_mention_bridge(nb.id, force=True) == 0


def test_seq_gate_and_flag(repo):
    nb = _seed_bridge_nb(repo)
    n1 = repo.rebuild_mention_bridge(nb.id)       # 未变 → 跳过,返回现有行数
    assert n1 >= 5
    repo.settings.mention_bridge_enabled = False
    assert repo.rebuild_mention_bridge(nb.id, force=True) == 0   # flag 关 → 清空/不建
```

- [ ] **Step 2: 确认失败** — `AttributeError: rebuild_mention_bridge` 等。

- [ ] **Step 3: 实现**（结构完全镜像 `rebuild_canonical_relations`——seq 闸/单写事务/fail-open 调用点；以下为核心骨架，锚点按当前代码微调）

```python
    def rebuild_mention_bridge(self, notebook_id: str, force: bool = False) -> int:
        """从 claim 文本提取 claim→跨源概念 mention 边与共提对(确定性,零 LLM)。

        流程:跨≥2源 concept 簇 → 别名表(mention_scan.build_alias_table) →
        rebuild 作用域临时 trigram FTS(claim 名) → 每别名 phrase MATCH 召回
        候选 → Latin \\b 后校验 → DF 门(超 mention_alias_df_cap 丢弃+事件) →
        mention_edges + concept_comentions 全量重写。mention_seq 闸同
        canonical_rel_seq;flag 关时清表返回 0。"""
        self.get_notebook(notebook_id)
        if not self.settings.mention_bridge_enabled:
            with self._write() as db:
                db.execute("DELETE FROM mention_edges WHERE notebook_id=?", (notebook_id,))
                db.execute("DELETE FROM concept_comentions WHERE notebook_id=?", (notebook_id,))
            return 0
        # seq 闸(照抄 rebuild_canonical_relations 的读法,列名换 mention_seq) ...
        # 1) 跨源簇 + 别名表
        from app.services.kg.mention_scan import build_alias_table, boundary_hit, is_latin
        # SELECT c.canonical_id, c.canonical_name, k.source_id ... GROUP 后取 len(srcs)>=2
        # 2) 临时 FTS(单写事务外建,用完 DROP):
        #    CREATE VIRTUAL TABLE IF NOT EXISTS mention_scan_fts USING fts5(text, tokenize='trigram')
        #    DELETE FROM mention_scan_fts; INSERT (rowid=枚举序, text=claim名lower)
        #    并维护 seq→(claim_id, text_lower) python 列表
        # 3) 每别名: SELECT rowid FROM mention_scan_fts WHERE mention_scan_fts MATCH '"<alias>"'
        #    候选做 boundary_hit 后校验;记录 alias→命中集合
        # 4) DF 双门: len(hits) > max(df_floor, df_cap*len(claims)) 的别名丢弃,计数进事件
        #    self.event_log.emit({"kind":"mention_alias_df_dropped", "notebook_id":..., "dropped": n})
        # 5) 聚合 claim→{canonical};写 mention_edges;组合两两计 concept_comentions;
        #    单写事务 DELETE×2 + executemany 批量 + UPDATE unified_kg_state SET mention_seq=?
        # 6) DROP TABLE mention_scan_fts
        ...
```

实现要求（评审会核对）：
- FTS MATCH 的别名要转义双引号；MATCH 短语用 `'"' + alias.replace('"','""') + '"'`。
- 别名与 claim 文本统一 **NFKC 折叠 + `lower()`**（`unicodedata.normalize("NFKC", t).lower()`——与 mention_scan.build_alias_table 的别名折叠对齐，全角/半角互通）；命中校验一律走 `boundary_hit`（alnum-lookaround 统一边界，Task 2 修复后语义）。FTS 建表插入的文本也用折叠后的版本。
- claim 集合 = `object_type='claim' AND status!='deprecated'`，文本 = payload.name，len<10 跳过。
- comention 计数按 claim 去重（同一 claim 对一对只计 1）。
- 接线两处（与 `rebuild_canonical_relations` 调用并排、同款 try/except，事件 `mention_bridge_rebuild_failed`；尾部 force=True）。

- [ ] **Step 4: 确认通过** — Run: `... -m pytest tests/test_mention_bridge.py tests/test_canonical_relations.py tests/test_rebuild_cache.py -q` → 全 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(kg): rebuild_mention_bridge——trigram FTS 提取 mention 边/共提对(DF门+seq闸+fail-open)"`

---

### Task 4: `sibling_peers` + 改接 expand_community / ask_chunk（共提优先、社区回退）

**Files:**
- Modify: `backend/app/services/communities.py`（新函数放 `community_peers` 之后）
- Modify: `backend/app/services/reasoning_retrieval.py`（`expand_community` 分支 ~:531-576）
- Modify: `backend/app/services/sqlite_repository.py`（`ask_chunk` 对比路径 ~:11803-11814）
- Test: `backend/tests/test_mention_bridge.py`（追加）+ 既有 `tests/test_communities.py` 回归

**Interfaces:**
- Consumes: Task 3 的 `concept_comentions`、`sibling_min_bridge`。
- Produces: `sibling_peers(repo, notebook_id, focal_name, *, top_k=8) -> list[tuple[str, int]]`（[(canonical_name, bridge_claims)] 降序；解析失败/无数据返回 `[]`）。

- [ ] **Step 1: 失败测试**

```python
def test_sibling_peers_returns_comention_partner(repo):
    from app.services.communities import sibling_peers
    nb = _seed_bridge_nb(repo)
    peers = sibling_peers(repo, nb.id, "Grouped-query attention (GQA)", top_k=5)
    assert peers and "Multi-Query Attention" in peers[0][0]
    assert peers[0][1] == 2


def test_sibling_peers_respects_min_bridge(repo):
    from app.services.communities import sibling_peers
    nb = _seed_bridge_nb(repo)
    repo.settings.sibling_min_bridge = 3
    assert sibling_peers(repo, nb.id, "Grouped-query attention (GQA)") == []
```

- [ ] **Step 2: 确认失败** — ImportError。

- [ ] **Step 3: 实现**

`communities.py` 新函数（focal 解析逻辑与 `community_peers` 同款——先读它再写，共享一个内部 `_resolve_focal` 若易抽）：

```python
def sibling_peers(repo, notebook_id, focal_name, *, top_k: int = 8):
    """共提兄弟:focal → canonical → concept_comentions 两侧按 bridge_claims 降序。
    P2 数据源,替换 Louvain community_peers(实测其不含同类);任何异常返回 []
    由调用方回退社区路径。"""
    try:
        min_b = int(getattr(repo.settings, "sibling_min_bridge", 2))
        with repo._connect() as db:
            row = db.execute(
                "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? "
                "AND object_type='concept' AND lower(canonical_name)=lower(?) LIMIT 1",
                (notebook_id, (focal_name or "").strip())).fetchone()
            if row is None:
                return []
            cid = row["canonical_id"]
            rows = db.execute(
                "SELECT canonical_a, canonical_b, bridge_claims FROM concept_comentions "
                "WHERE notebook_id=? AND (canonical_a=? OR canonical_b=?) AND bridge_claims>=? "
                "ORDER BY bridge_claims DESC LIMIT ?",
                (notebook_id, cid, cid, min_b, top_k)).fetchall()
            out = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == cid else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? "
                    "AND canonical_id=? LIMIT 1", (notebook_id, other)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out
    except Exception:
        return []
```

注意：focal 解析先读 `community_peers` 的现行解析（可能是 LIKE/成员名匹配）——**与它保持一致**（对齐失败模式），必要时抽公共 helper；上面的精确 lower= 匹配是底线实现，若 community_peers 用更宽松匹配则复用之。

改接①——`reasoning_retrieval.py` `expand_community` 分支：在调用 `community_peers` 之前先 `sibling_peers(...)`；非空则用其名单（summary 文案 `"横向对比(共提):纳入 N 个同类实体,新增候选 M"`），空则原路 `community_peers`（summary 保持原文案）。trace `step_type` 不变（前端零改动）。

改接②——`ask_chunk` 对比路径（~:11805）：同样先 `sibling_peers`、空则 `community_peers`；沿用现有 flag 检查与去重逻辑。

```python
def test_expand_community_prefers_comention_siblings(repo, monkeypatch):
    # 直接调分支太深:用 sibling_peers 非空时 ask_chunk 对比路径把兄弟名并进 sub_queries 来断言。
    # 构造: _seed_bridge_nb + 假 expand_query 返回 comparison 字段(monkeypatch query_rewrite.expand_query)。
    ...  # 实现者按 ask_chunk 现行测试(tests/test_ask_modes.py 或邻近)的既有 mock 模式写;
         # 断言: sub_queries 包含 "Multi-Query Attention (MQA)"(来自共提而非社区——本fixture未建社区数据)。
```

（第三个测试按仓库现有 ask_chunk 测试的 mock 惯例落地；找不到先例就测 `sibling_peers` 优先逻辑的最小可测单元——把优先/回退逻辑抽成小函数 `resolve_comparison_peers(repo, nb, focal)` 供两处调用并直接单测它。**推荐直接抽这个小函数**，两处调用点共享，测试免 mock LLM。）

- [ ] **Step 4: 确认通过** — Run: `... -m pytest tests/test_mention_bridge.py tests/test_communities.py tests/test_ask_modes.py -q`（文件名以 ls 实际为准）→ 全 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(kg): sibling_peers 共提兄弟——expand_community/ask_chunk 对比路径改共提优先·社区回退"`

---

### Task 5: PPR 图注入 mention 边（三个构建点）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_ppr_graph` 的 extra_edges 组装处、`_federated_rx_graph` 对应处、scale index 图构建的 emb_synonym 注入点旁——三处都以 `emb_synonym_edges` 调用为锚点）
- Test: `backend/tests/test_mention_bridge.py`（追加）

**Interfaces:**
- Consumes: `mention_edges` 表、`mention_edge_weight`。
- Produces: 三个图构建把 `(claim_object_id, f"cluster:{concept_canonical_id}", weight)` 追加进 extra/synonym 边集合。cluster router 节点由既有 cluster_groups 机制生成（build_ppr_graph 里 `cluster:` 前缀）。

- [ ] **Step 1: 失败测试**

```python
def test_ppr_graph_contains_mention_edges(repo):
    nb = _seed_bridge_nb(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    # 桥 claim 节点应连到 GQA/MQA 的 cluster router
    with repo._connect() as db:
        row = db.execute("SELECT claim_object_id, concept_canonical_id FROM mention_edges "
                         "WHERE notebook_id=? LIMIT 1", (nb.id,)).fetchone()
    a = key_to_idx.get(row["claim_object_id"])
    b = key_to_idx.get(f"cluster:{row['concept_canonical_id']}")
    assert a is not None and b is not None
    assert G.has_edge(a, b)
```

- [ ] **Step 2: 确认失败**。

- [ ] **Step 3: 实现** — 新小 helper `_mention_extra_edges(notebook_id) -> list[tuple[str, str, float]]`（一次 SELECT,flag 关或表空返 []），三个构建点在 emb_synonym 注入之后 `extra_edges = extra_edges + self._mention_extra_edges(nb)`。注意：
  - `_ppr_graph`/`_federated_rx_graph` 是多 notebook participants——按各 participant 分别读其 mention_edges。
  - 图版本缓存 key 需纳入 mention 数据版本：把 `mention_seq`（或 mention_edges COUNT/MAX 简化为 `unified_kg_state.mention_seq`）加进两处 version tuple（照 emb_synonym 的 settings 入 key 先例，把 `mention_bridge_enabled`/`mention_edge_weight` 也入 key）。
  - scale index 构建点同样追加（以 `emb_synonym_edges(` 在 build_scale_index 路径的调用为锚点；scale 图的节点键空间与 build_ppr_graph 一致才加——**先读清楚**，若 scale CSR 节点空间不含 cluster router 则该点跳过并在报告说明，由评审裁决）。

- [ ] **Step 4: 确认通过** — Run: `... -m pytest tests/test_mention_bridge.py tests/test_ppr*.py -q`（以 ls 实际为准）→ 全 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(kg): PPR/联邦/scale 图注入 mention 边——claim→concept 跨文档导通(版本入缓存键)"`

---

### Task 6: 全量回归 + 沙箱复跑 + PR（controller 亲自）

- [ ] Step 1: `... -m pytest -q` 全量 → 全 PASS。
- [ ] Step 2: 沙箱复跑（p2-sandbox/copy.db 已在）：force rebuild 后打印 mention_edges/comentions 规模、`sibling_peers(GQA)` 实际返回（应含 MQA）、DF 门丢弃数——进 PR body。
- [ ] Step 3: rebase origin/master → push → `gh pr create`（body：P2-A 验证结论摘要、方案、沙箱数字、生效方式、非目标）。
