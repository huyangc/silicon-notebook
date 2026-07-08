# KG Unicode 实体归一化修复（P0）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CJK/Unicode 实体名参与 KG 实体合并——修复「纯中文名跨文档全部塌缩进同一个 `K-` 簇、文档内永不合并」的确定性 bug，并加"空 seed 永不共簇"守卫。

**Architecture:** 两个归一化函数改为 NFKC + Unicode `\w` 字符类（纯 ASCII 名输出逐字节不变）；新增 `seed_or_unique` 守卫接入四个 seed 消费点（`cluster_objects` / `place_new_concepts` / `detect_bridge_candidates` / `_stream_seed_reps`）。全部是派生层（clusters 在 rebuild 时重算），无 schema 变更、无需重抽。

**Tech Stack:** Python 3.13, `unicodedata`（标准库）, pytest。测试解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

## Global Constraints

- **效率**：不新增任何 LLM/embed/DB 调用；归一化保持纯 regex + NFKC（C 实现）；`_stream_seed_reps` 热循环内不加额外分配（守卫仅 falsy 分支触发）。
- **ASCII 零扰动**：纯 ASCII 名字的 seed 必须与旧实现逐字节相同（测试固化）。这是"英文库 rebuild 结果不漂移"的承诺。
- **不改 schema**：无 `_migration_N`、无 SCHEMA_VERSION bump（concept_clusters 由 rebuild 全量重写）。
- **不碰真实库**：验证脚本一律只读打开 `file:...?mode=ro`；不启停任何服务。
- **测试运行**：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest <file> -q`。
- **提交规范**：`fix(kg): 中文摘要` 风格（对齐仓库近期提交）；每个 Task 一个 commit。
- **哨兵字符**：空 seed 回退 `f"~{object_id}"`——`~` 会被两个 `_norm` 清洗掉，真实名字的 seed 永远不可能以 `~` 开头，无碰撞。

---

### Task 1: `kg_merge._norm` Unicode-safe（跨文档 seed 归一化）

**Files:**
- Modify: `backend/app/services/kg_merge.py`（`_norm`，约 line 66-70；顶部 import）
- Test: `backend/tests/test_kg_merge.py`（文件已存在，追加）

**Interfaces:**
- Produces: `_norm(name: str) -> str` 语义变化——CJK/希腊/带音标字母保留；NFKC 折叠全角；纯 ASCII 输入输出不变。Task 3/4 依赖此语义。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_kg_merge.py` 末尾）

```python
# --- Unicode / CJK normalization (P0 fix) ------------------------------------

def test_norm_preserves_cjk_names():
    assert _norm("双重汇率制") == "双重汇率制"
    assert _norm("铸币平价") == "铸币平价"
    assert _norm("双重汇率制") != _norm("铸币平价")


def test_norm_keeps_cjk_tokens_in_mixed_names():
    # 年份+中文名绝不能塌缩到裸年份 seed（旧行为:"1903年国际汇兑委员会"->"1903"）
    assert _norm("1903年国际汇兑委员会") != "1903"
    assert _norm("1903年国际汇兑委员会") != _norm("1955年货币改革")


def test_norm_nfkc_folds_fullwidth_ascii():
    assert _norm("ＫＶ Ｃａｃｈｅ") == _norm("KV Cache")


def test_norm_greek_letters_survive():
    assert _norm("ΔΣ Modulator") == "δσ modulator"


def test_norm_symbol_only_name_is_empty():
    # 纯符号名归一化为空——由 seed_or_unique 守卫兜底（Task 3）
    assert _norm("→") == ""
    assert _norm("★☆") == ""


def test_norm_ascii_behavior_unchanged():
    # ASCII 零扰动回归网:与旧 [^a-z0-9+/ ] 字符类逐字节一致
    assert _norm("Miller Compensation") == "miller compensation"
    assert _norm("Op-Amp") == "op amp"        # 连字符→空格; "op amp" 是 _ALIASES 不动点
    assert _norm("I/O") == "i/o"              # + / 保留
    assert _norm("A_B") == "a b"              # 下划线经第二个 sub 归并为空格
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py -q`
Expected: 新增 6 个测试中前 4 个 FAIL（CJK 被清空 / 全角未折叠 / 希腊字母被清空）；`test_norm_symbol_only_name_is_empty` 与 `test_norm_ascii_behavior_unchanged` 本来就 PASS（基线行为）。

- [ ] **Step 3: 最小实现**

`backend/app/services/kg_merge.py` 顶部 import 区加：

```python
import unicodedata
```

替换 `_norm`（原 line 66-70）：

```python
def _norm(name: str) -> str:
    # NFKC 先行:中文语料常见全角拉丁/数字/括号(（）ＡＢＣ１２３)折到 ASCII,
    # 让 acronym 剥离与 _ALIASES 能看见;纯 ASCII 输入是恒等变换。
    folded = unicodedata.normalize("NFKC", name or "")
    stripped = _strip_paren_acronym(folded)
    # Unicode \w 保留 CJK/希腊/带音标字母 —— 旧 [^a-z0-9+/ ] 把纯中文名清成空
    # seed,全库此类实体确定性塌缩进同一个 "K-" 簇(实测中文库 54% concept)。
    # 纯 ASCII 名输出与旧类逐字节相同(下划线两版都归并为空格)。
    cleaned = re.sub(r"[^\w+/ ]+", " ", stripped.strip().lower())
    cleaned = re.sub(r"[\s\-_]+", " ", cleaned).strip()
    return _ALIASES.get(cleaned, cleaned)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py -q`
Expected: 全 PASS（含既有 acronym/qualifier/contrast 测试——它们是 ASCII 回归网）。

- [ ] **Step 5: 顺带跑受 `_norm` 影响的邻近套件**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_doc_merge.py tests/test_unified_kg_repository.py tests/test_autotune_kg_merge.py -q`
Expected: 全 PASS。若有断言旧"清空 CJK"行为的测试失败，按新语义修正该测试并在 commit message 里说明。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "fix(kg): 跨文档 seed 归一化 Unicode-safe——CJK/希腊/全角不再清空(NFKC+\w),纯ASCII逐字节不变"
```

---

### Task 2: `kg/canonicalize._norm` Unicode-safe（同文档窗口间合并）

**Files:**
- Modify: `backend/app/services/kg/canonicalize.py`（`_norm`，line 7-8；顶部 import）
- Test: `backend/tests/kg/test_canonicalize.py`（文件已存在，追加；复用其 `_c(nid, name)` 帮手）

**Interfaces:**
- Consumes: 无（独立于 Task 1——两个 `_norm` 是不同函数，语义刻意分离：本函数删除标点、kg_merge 版替换为空格）。
- Produces: 同文档内同名 CJK Concept 跨窗口合并；空归一名维持既有"不合并"守卫（[canonicalize.py:17](../../backend/app/services/kg/canonicalize.py) 的 `and _norm(n.name)`）。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/kg/test_canonicalize.py`）

```python
def test_cjk_concepts_merge_within_doc():
    out, _ = canonicalize([_c("W0-0", "铸币平价"), _c("W1-0", "铸币平价")], [], doc_id="d")
    concepts = [n for n in out if n.type == "Concept"]
    assert len(concepts) == 1
    assert len(concepts[0].mentions) == 2


def test_cjk_distinct_concepts_not_merged():
    out, _ = canonicalize([_c("W0-0", "双重汇率制"), _c("W1-0", "组织策略")], [], doc_id="d")
    assert len([n for n in out if n.type == "Concept"]) == 2


def test_symbol_only_concepts_never_merge():
    # 空归一名走 else 分支原样保留(既有守卫),修复后必须维持
    out, _ = canonicalize([_c("W0-0", "→"), _c("W1-0", "→")], [], doc_id="d")
    assert len([n for n in out if n.type == "Concept"]) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_canonicalize.py -q`
Expected: `test_cjk_concepts_merge_within_doc` FAIL（返回 2 个 Concept——旧 `_norm` 清空中文名，走不合并分支）；另两个 PASS。

- [ ] **Step 3: 最小实现**

`backend/app/services/kg/canonicalize.py`：import 区加 `import unicodedata`，替换 `_norm`：

```python
def _norm(name: str) -> str:
    # NFKC + Unicode \w:CJK/希腊/带音标名参与同文档窗口间合并(旧类清成空名,
    # 中文概念每窗口留一份重复节点)。删除语义保持:非字母数字直接删除(不是替换
    # 空格),下划线显式删除 —— 纯 ASCII 输出与旧 [^a-z0-9 ] 类逐字节相同。
    folded = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]|_", "", folded)).strip()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/ -q`
Expected: 全 PASS（`tests/kg/` 整目录，覆盖 extract/windowing/relink 等间接消费者）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/canonicalize.py backend/tests/kg/test_canonicalize.py
git commit -m "fix(kg): 同文档 canonicalize 归一化 Unicode-safe——中文概念跨窗口能合并,ASCII行为不变"
```

---

### Task 3: `seed_or_unique` 空 seed 守卫（kg_merge 内三个消费点）

**Files:**
- Modify: `backend/app/services/kg_merge.py`（新增函数；改 `cluster_objects` line ~385、`place_new_concepts` line ~497、`detect_bridge_candidates` line ~466）
- Test: `backend/tests/test_kg_merge.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_norm`（符号-only 名 → `""`）。
- Produces: `seed_or_unique(seed: str, object_id: str) -> str`——空 seed 回退 `f"~{object_id}"`。Task 4 在 `_stream_seed_reps` 里复用**同一个函数**。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_kg_merge.py`）

```python
# --- empty-seed guard: degenerate names must never co-cluster -----------------

def test_seed_or_unique():
    from app.services.kg_merge import seed_or_unique
    assert seed_or_unique("cascode", "o1") == "cascode"
    assert seed_or_unique("", "o1") == "~o1"
    assert seed_or_unique("", "o1") != seed_or_unique("", "o2")


def test_symbol_only_concepts_get_unique_clusters():
    got = cluster_concepts([_concept("o1", "→"), _concept("o2", "★")], {}, set(), set())
    cm = got["cluster_map"]
    assert cm["o1"] != cm["o2"]
    assert "K-" not in (cm["o1"], cm["o2"])   # 旧行为:两者都塌缩进 "K-"


def test_same_symbol_name_still_isolated():
    # 无真实名 seed 时宁可不并:两个都叫 "→" 的对象也各自独立成簇
    got = cluster_concepts([_concept("o1", "→"), _concept("o2", "→")], {}, set(), set())
    assert got["cluster_map"]["o1"] != got["cluster_map"]["o2"]


def test_place_new_concepts_empty_seed_unique():
    from app.services.kg_merge import place_new_concepts, _norm
    rows = place_new_concepts(
        [{"object_id": "n1", "name": "→"}, {"object_id": "n2", "name": "☆"}],
        {}, {}, seed_fn=lambda o: _norm(o.get("name", "")))
    cids = {r["canonical_id"] for r in rows}
    assert len(cids) == 2 and "K-" not in cids


def test_detect_bridge_empty_name_never_emits_bare_K():
    from app.services.kg_merge import detect_bridge_candidates
    out = detect_bridge_candidates(
        [{"object_id": "n1", "name": "→"}], {"n1": [1.0, 0.0]},
        [{"object_id": "e1", "name": "cascode"}], {"e1": [1.0, 0.0]},
        {"e1": "K-cascode"}, set())
    assert len(out) == 1                        # 向量桥接仍然发生
    assert all("K-" not in (c["canonical_a"], c["canonical_b"]) for c in out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py -q -k "seed_or_unique or symbol or empty_seed or bare_K"`
Expected: `test_seed_or_unique` FAIL（ImportError/AttributeError——函数不存在）；其余 4 个 FAIL（塌缩为同一 canonical / 出现 "K-"）。

- [ ] **Step 3: 最小实现**

`backend/app/services/kg_merge.py`，放在 `_norm` 之后：

```python
def seed_or_unique(seed: str, object_id: str) -> str:
    """空/退化 seed(符号-only 名)绝不共簇:回退为按对象唯一的哨兵 seed。
    "~" 会被 _norm 清洗掉,真实名字的 seed 不可能以 "~" 开头 → 无碰撞。
    宁可不并,不可全并(旧行为:全部空 seed 共享 canonical "K-")。"""
    return seed if seed else f"~{object_id}"
```

`cluster_objects`（line ~385）改一行：

```python
    seed_of = {c["object_id"]: seed_or_unique(_seed_with_alias(c, seed_fn, alias_map),
                                              c["object_id"]) for c in objects}
```

`place_new_concepts` 的 for 循环体（line ~495-500）改：

```python
    for o in new_objects:
        name = o.get("name", "")
        seed = seed_or_unique(_seed_with_alias(o, seed_fn, alias_map), o["object_id"])
        cid = f"{id_prefix}{seed}"
        canon_name = existing_canon_names.get(cid, name) if cid in existing_cids else name
        rows.append({"canonical_id": cid, "member_object_id": o["object_id"],
                     "canonical_name": canon_name})
```

`detect_bridge_candidates`（line ~466）改一行：

```python
        my_cid = "K-" + seed_or_unique(_norm(it.get("name", "")), it["object_id"])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py tests/test_cross_doc_merge.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "fix(kg): 空seed守卫 seed_or_unique——退化名回退按对象唯一哨兵,宁可不并不可全并"
```

---

### Task 4: `_stream_seed_reps` 接守卫 + 仓储层集成测试

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_stream_seed_reps` 流式循环，line ~6493；同函数 import 行 ~6448）
- Test: `backend/tests/test_cross_doc_merge.py`（追加，复用其 `repo` fixture 与 `_ids_by_name`）

**Interfaces:**
- Consumes: Task 3 的 `seed_or_unique(seed, object_id)`。
- Produces: rebuild / 增量融合全链路无 `K-` 巨簇；跨文档同名 CJK concept 正确共簇（canonical_id 形如 `K-铸币平价`）。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_cross_doc_merge.py`）

```python
# --- Unicode/CJK cross-doc clustering (P0 fix) --------------------------------

def _all_concept_ids(repo, nb_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
            (nb_id,)).fetchall()
    return [r["id"] for r in rows]


def test_rebuild_merges_same_cjk_concept_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    ids = _all_concept_ids(repo, nb.id)
    vals = {cm.get(i) for i in ids}
    assert len(ids) == 2 and vals == {"K-铸币平价"}


def test_rebuild_no_empty_seed_mega_cluster(repo):
    # 互不相关的中文名+纯符号名必须各自独立成簇;旧行为全部塌缩进 canonical "K-"
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for i, nm in enumerate(["双重汇率制", "组织策略", "→"]):
        repo.store_kg(nb.id, f"s{i}", [{"local_id": f"C{i}", "object_type": "concept",
            "payload": {"name": nm, "section_path": "1"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    vals = [cm.get(i) for i in _all_concept_ids(repo, nb.id)]
    assert len(set(vals)) == 3
    assert "K-" not in vals


def test_incremental_fuse_cjk_joins_existing_cluster(repo):
    # Tier1 名种子 append:新源中文 concept 落进已有簇(不必等全量 rebuild)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "1"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "2"}, "evidence": []}], [])
    repo.incremental_fuse_source(nb.id, "s2")
    cm = repo.cluster_map(nb.id)
    vals = {cm.get(i) for i in _all_concept_ids(repo, nb.id)}
    assert vals == {"K-铸币平价"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_doc_merge.py -q -k cjk_or_mega or true` （用 `-k "cjk or mega"`）
Expected: 前两个测试在 Task 1 之后其实已 PASS（`_norm` 已修，中文 seed 非空）；**`test_rebuild_no_empty_seed_mega_cluster` 里 "→" 仍与其它对象隔离依赖本 Task 的 `_stream_seed_reps` 守卫**——若 Task 3 已完成而这里未接线，"→" 的 seed 为空 → canonical "K-" 出现 → FAIL。以实际失败集为准，至少 `test_rebuild_no_empty_seed_mega_cluster` FAIL。

- [ ] **Step 3: 最小实现**

`backend/app/services/sqlite_repository.py` `_stream_seed_reps` 内（line ~6448）import 行改为：

```python
        from app.services.kg_merge import (build_acronym_alias_map, _seed_with_alias,
                                           _norm, seed_or_unique)
```

流式循环内（line ~6493）：

```python
                    seed = seed_or_unique(
                        _seed_with_alias({"name": name, "payload": pay}, seed_fn, alias_map),
                        r["id"])
```

- [ ] **Step 4: 全量验证接线完整性**

Run: `cd backend && grep -n "K-\" *+\|'K-' *+\|\"K-\" *+\|id_prefix}{" app/services/*.py app/services/kg/*.py | grep -v test`
Expected: 所有从名字构造 canonical id 的位置（`place_new_concepts` / `detect_bridge_candidates` / `_stream_seed_reps` 经 `cluster_seeds`）都已走 `seed_or_unique`。若 grep 出计划外的第 5 处消费点，同样接上守卫并补一个对应测试。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_doc_merge.py tests/test_unified_kg_repository.py tests/test_rebuild_streaming.py tests/test_kg_incremental_fusion.py -q`
Expected: 全 PASS（`test_kg_incremental_fusion.py` 若不存在则跳过该文件，以 `ls backend/tests | grep incremental` 实际名为准）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_cross_doc_merge.py
git commit -m "fix(kg): rebuild流式seed接空seed守卫——中文库不再产出K-巨簇,同名CJK跨文档正确共簇"
```

---

### Task 5: 全量回归 + 真实库只读验证 + PR

**Files:**
- Create: 验证脚本放 scratchpad（不入库）
- 无产品代码改动

**Interfaces:**
- Consumes: Task 1-4 全部落地。
- Produces: 全量测试绿 + 真实库前后对照数据（进 PR body）+ PR。

- [ ] **Step 1: 全量后端测试**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q 2>&1 | tail -5`
Expected: 全 PASS（近期基线 ~1000+ passed）。任何失败先按 systematic-debugging 处理，不许跳过。

- [ ] **Step 2: 真实库只读模拟（PR 证据）**

写脚本到 scratchpad：只读打开 `/Users/hzf/workspace/silicon_notebook/.local/silicon_notebook.db`，`sys.path.insert(0, "<worktree>/backend")` 后 import **修复后的** `app.services.kg_merge._norm/seed_or_unique`，对两个库计算新 seed 分布：

```python
# 关键断言(打印,不写库):
# 中文库 nb-cae6fcc1e1: 旧空seed 207 -> 新空seed 0;
#   预测簇数 = len({seed_or_unique(_norm(nm), oid) for oid,nm in concepts})
# 基准库 nb-b37185f4ae: seed 变化的 concept 数(预期 = 3,即原空seed的3个中文名;
#   其余纯ASCII名 seed 逐字节不变 -> 数出精确值进 PR body)
```

Expected: 中文库空 seed 0、`K-` 巨簇消失（207 成员打散成 ~唯一名数个簇）；基准库 seed 变化数 = 个位数（只有含非 ASCII 字符的名字）。

- [ ] **Step 3: rebase 到 master + push + PR**

```bash
git fetch origin && git rebase origin/master
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q 2>&1 | tail -3 && cd ..
git push -u origin claude/kg-entity-merge-baseline-e71f5d
gh pr create --base master --title "fix(kg): Unicode 实体归一化——修中文实体 K- 巨簇塌缩与同文档不合并" --body "<按下方要点写>"
```

PR body 要点（必含）：
1. 根因与实测：`_norm` 清空非 `[a-z0-9]` → 中文库 54%（207/386）concept 现已塌缩在同一个 `K-` 簇（真实库只读实测）；同文档中文概念永不合并；混合名剥离出年份 seed（`1903年国际汇兑委员会`→`1903`）。
2. 修复：两个 `_norm` NFKC+Unicode `\w`（纯 ASCII 逐字节不变，测试固化）+ `seed_or_unique` 空 seed 守卫四处接线。
3. 生效方式：**已部署库需点「刷新图谱」（force rebuild）**；增量融合对新源立即生效。英文库 rebuild 结果不漂移（真实库模拟：seed 变化数 N=个位数，附数字）。
4. 已决合并对（confirmed/rejected）按旧 seed 存储，非 ASCII 相关的极少数会失配转休眠——量化数字（预期 ~0）。
5. 不改 schema、无新增 LLM/embed 调用。
