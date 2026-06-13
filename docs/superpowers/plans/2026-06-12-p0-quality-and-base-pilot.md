# P0 质量收口 + P0.5 base 库演练 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 nb-012（5 本模拟电路教材）收口成第一个真实 base 库：清除 claim 元叙述/前言噪声、修复陈旧聚簇，并完整演练两层知识库机制（强审门/联合检索/晋升/冲突/推导问答），产出演练报告。

**Architecture:** 三阶段交织。Phase 1 在 worktree 改代码（frontmatter/TOC 窗口过滤 + 元叙述 claim 入库过滤 + 评测探针动词表修正），TDD；Phase 2 在 root master 做数据运维（标 base → 重抽 Gray/RF → rebuild 聚簇 → A/B 验收）；Phase 3 起服务演练两层机制并记录报告。**顺序关键：先标 base 再重抽**——这样重抽自动获得 base_filter prompt 规则，且新对象落 `reviewed`（正好成为强审门演练的真实材料）。

**Tech Stack:** Python/FastAPI、SQLite、pytest；运维脚本 `scripts/denoise_reextract_nb.py`；评测 `backend/app/eval/probes.py`。

**对应 spec:** `docs/superpowers/specs/2026-06-12-two-tier-roadmap-design.md`（P0 + P0.5 节）

---

## 背景事实（2026-06-12 实测，执行者必读）

写计划前已对主库（`/Users/hzf/workspace/silicon_notebook/.local/silicon_notebook.db`，notebook `nb-012fb94249`）做过诊断，**spec 里的 P0 范围据此修正**：

1. **过度合并已在代码层修复**：`rebuild_unified_kg`（sqlite_repository.py:2406-2423）已用 `hi=0.94` 默认值 + 全量 auto_candidates LLM 预审 + `_CONTRAST_GROUPS` 护栏（含 drain/source/gate）。烂簇（如 `K-bulk` 簇 20 个成员混入 Channel Length/diffusion constant 等）是**旧算法的数据残留**；`concept_merge_candidates` 表只有 334 条 pending、零 confirmed/rejected，旧误并**没有固化成决策**，重跑 rebuild 即可打散。→ 本计划不改聚类代码，只重跑+验证。
2. **claim 重复率仅 0.6%**（16,887 总数 vs 16,782 唯一规范名）——"去重"不是问题。**claim degraded 率 11–21% 才是问题**（probes 实测：Allen 11.1%/14.5%、Razavi 19.8%、Gray 14.1%、RF 21.3%），构成是前言/教学指南元叙述（"This book deals with…"）、目录标题（"Relation Between Frequency Response and Time Response"）、以及**探针误杀**（`_VERB_RE` 动词表缺 become/continue/detect/fall 等，把真断言"MOS transistors continue to become faster…"判为降质）。
3. **窗口过滤缺 frontmatter**：`filters.py` 的 `_BACKMATTER_SEGMENTS` 只有 index/glossary/references/bibliography，preface/目录页照常进抽取——RF 的教学指南 claim 重抽也拦不住，必须补。
4. **doc_type 缺口确认**：`src-34f4e77e21`（Gray）与 `src-eafffde53a`（RF Razavi）的 doc_type 为空（denoise 脚本跑过之后才上传的），习题节未跳。
5. **聚簇陈旧确认**：nb-012 概念 7,985、入簇 6,868（缺 1,117）；`unified_kg_state.dirty=1`。
6. **base 强审门语义**（sqlite_repository.py:2010-2020）：base notebook 的 `store_kg` 落 `reviewed` 而非 `approved`——**`reviewed` 仍在 USABLE_STATUSES、可检索**，只是"待 curator 确认为 canonical"的弱标记，不阻塞使用。
7. 抽取 prompt（kg/extract.py:31-35）的 `base_filter` 规则只在 notebook tier='base' 时启用（sqlite_repository.py:1406）——nb-012 当前是 personal，所以必须**先标 base 再重抽**。

---

## Phase 1 — 代码改动（在 worktree `claude/laughing-torvalds-22fb2d`，TDD）

### Task 1: frontmatter / TOC 窗口过滤

**Files:**
- Modify: `backend/app/services/kg/filters.py`
- Test: `backend/tests/kg/test_filters.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/kg/test_filters.py` 的 `# ---- should_extract_window ----` 区段（`test_problem_skip_only_for_textbook` 之后）追加：

```python
def test_skips_frontmatter_sections():
    for path in ["Preface", "PREFACE", "To the Instructor", "Foreword",
                 "Acknowledgments", "前言", "目录"]:
        keep, reason = should_extract_window(
            path, [_el("This book deals with the analysis of RF circuits.")], "textbook")
        assert keep is False and reason == "frontmatter_section", path


def test_frontmatter_segment_exact_match_no_false_positive():
    # 段内含 "preface" 词但不是 frontmatter 段名 → 不拦
    keep, _ = should_extract_window(
        "3 > 3.2 Preface to the Noise Model",
        [_el("Thermal noise arises from random carrier motion.")], "textbook")
    assert keep is True


def test_skips_toc_like_window():
    els = [_el("1.1 General Considerations 7"),
           _el("1.2 Costs of Integration 9"),
           _el("2.1 General Considerations 15")]
    keep, reason = should_extract_window("Contents", [_el("x")], "textbook")
    assert keep is False  # 段名 Contents 直接拦
    keep2, reason2 = should_extract_window("1 Overview", els, "textbook")
    assert keep2 is False and reason2 == "toc_like_window"


def test_toc_like_not_triggered_by_body_prose():
    els = [_el("The gain of the amplifier is set by the ratio of resistors."),
           _el("2.1 The small-signal model applies at low frequencies.")]
    keep, _ = should_extract_window("2 > 2.1 Body", els, "textbook")
    assert keep is True
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/laughing-torvalds-22fb2d
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/test_filters.py -x -q
```
Expected: FAIL（`frontmatter_section` 未实现）

- [ ] **Step 3: 实现**

`backend/app/services/kg/filters.py`，在 `_BACKMATTER_CJK` 行后加：

```python
_FRONTMATTER_SEGMENTS = {
    "preface", "foreword", "acknowledgments", "acknowledgements",
    "contents", "table of contents", "brief contents",
    "about the author", "about the authors",
    "to the instructor", "to the student", "suggestions for instructors",
}
_FRONTMATTER_CJK = ("前言", "目录", "致谢", "序言")
# 目录行: "1.1 General Considerations 7" — 节号 + 标题 + 行尾页码
_TOC_LINE_RE = re.compile(r"^\d+(\.\d+)*\s+\S.{0,90}?\s+\d{1,4}$")


def _toc_like_ratio(elements: Sequence[SourceElementQ]) -> float:
    texts = [(e.text or "").strip() for e in elements if (e.text or "").strip()]
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if _TOC_LINE_RE.match(t))
    return hits / len(texts)


def _is_frontmatter(section_path: str) -> bool:
    """Exact-segment match like _is_backmatter (avoids 'Preface to the Noise
    Model' false positives). CJK terms match as substring."""
    for seg in (section_path or "").split(">"):
        if seg.strip().lower() in _FRONTMATTER_SEGMENTS:
            return True
    return any(t in (section_path or "") for t in _FRONTMATTER_CJK)
```

`should_extract_window` 在 `_is_backmatter` 分支后加：

```python
    if _is_frontmatter(path):
        return False, "frontmatter_section"
    if _toc_like_ratio(elements) >= 0.6:
        return False, "toc_like_window"
```

注意：`test_skips_toc_like_window` 里 "Contents" 段名走 `frontmatter_section` 分支返回（断言只查 `keep is False`，不查 reason），目录行窗口走 `toc_like_window`。

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/test_filters.py -x -q
```
Expected: PASS（全部，含既有用例）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/filters.py backend/tests/kg/test_filters.py
git commit -m "feat(kg): 窗口过滤新增 frontmatter/目录页跳过

preface/instructor-guide/目录页此前照常进抽取，是教材元叙述 claim
的主要来源（probes 实测 claim degraded 11-21% 的大头）。
精确段匹配防 'Preface to the Noise Model' 误伤，目录行按比例判定。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 2: 元叙述 claim 入库过滤

**Files:**
- Modify: `backend/app/services/kg/filters.py`
- Modify: `backend/app/services/kg/models.py:45`（`concepts_dropped` 后加字段）
- Modify: `backend/app/services/kg_ingest.py:136-192`
- Modify: `backend/app/services/sqlite_repository.py:1429-1430`（run notes 串）
- Test: `backend/tests/kg/test_filters.py`、`backend/tests/kg/test_meta_claim_drop.py`（新建）

- [ ] **Step 1: 写 is_meta_claim 失败测试**

`backend/tests/kg/test_filters.py` 末尾追加：

```python
# ---- is_meta_claim ----

from app.services.kg.filters import is_meta_claim


def test_meta_claims_dropped():
    for s in [
        "This book deals with the analysis and design of RF integrated circuits",
        "CMOS technology is the subject of this text.",
        "Chapter 9 presents the topic of switched capacitor circuits.",
        "This chapter forms the foundation for synthesizers.",
        "Section 1.1 gave a definition of signals in analog circuits",
        "I wanted to teach design in addition to analysis",
        "In this chapter, we will see the noise model of the MOSFET",
    ]:
        hit, reason = is_meta_claim(s)
        assert hit is True and reason == "meta_narrative", s


def test_technical_claims_kept():
    for s in [
        # 真断言不得误杀——含 chapter/section 词但指涉技术对象或他文引用
        "The input section of the op amp dominates the noise budget",
        "MOS transistors continue to become faster, but at the cost of their 'analog' properties.",
        "The slew rate is set by the compensation capacitor.",
        "Thermal noise increases with temperature",
    ]:
        assert is_meta_claim(s)[0] is False, s
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/test_filters.py -x -q
```
Expected: FAIL（ImportError: is_meta_claim）

- [ ] **Step 3: 实现 is_meta_claim**

`backend/app/services/kg/filters.py` 末尾追加：

```python
# --- meta-narrative claim filter ---
# 口径刻意比 eval 探针(app/eval/probes.py _META_RE)窄: 只拦明确指涉文档自身的
# 句式, 零误杀优先; 评测侧保持宽口径作"疑似信号"。
_META_CLAIM_RE = re.compile(
    r"\b(this (book|chapter|text|section|paper)\b"
    r"|in this (book|chapter|text|section)\b"
    r"|(next|previous|preceding|following) chapter\b"
    r"|chapter \d+ (presents?|covers?|deals?|discuss(es)?|provides?|forms?"
    r"|introduces?|can be|may (not )?fit|is relatively)"
    r"|section \d+(\.\d+)* (gave|gives?|presents?|covers?|discuss(es)?)"
    r"|i wanted to\b"
    r"|we will (see|discuss|cover|return)\b)",
    re.IGNORECASE)


def is_meta_claim(name: str) -> Tuple[bool, str]:
    """元叙述/导航类 Claim(讲文档自身而非技术内容) → 确定性丢弃。"""
    if _META_CLAIM_RE.search((name or "").strip()):
        return True, "meta_narrative"
    return False, ""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/test_filters.py -x -q
```
Expected: PASS

- [ ] **Step 5: 写 drop_meta_claims 管线失败测试**

新建 `backend/tests/kg/test_meta_claim_drop.py`：

```python
from app.services.kg.models import Node, Edge
from app.services.kg_ingest import drop_meta_claims


def _n(nid, typ, name):
    return Node(id=nid, type=typ, name=name)   # evidence 等字段均有默认值


def test_drop_meta_claims_removes_meta_and_dangling_edges():
    nodes = [
        _n("c1", "Claim", "This book deals with the analysis of RF circuits"),
        _n("c2", "Claim", "Thermal noise increases with temperature"),
        _n("k1", "Concept", "thermal noise"),
    ]
    edges = [
        Edge(id="e1", type="about", source_id="c1", target_id="k1"),
        Edge(id="e2", type="about", source_id="c2", target_id="k1"),
    ]
    kept_nodes, kept_edges, dropped = drop_meta_claims(nodes, edges)
    assert dropped == 1
    assert {n.id for n in kept_nodes} == {"c2", "k1"}
    assert len(kept_edges) == 1 and kept_edges[0].source_id == "c2"


def test_drop_meta_claims_only_touches_claims():
    nodes = [_n("k1", "Concept", "this chapter")]  # Concept 不受 claim 过滤影响
    kept_nodes, _, dropped = drop_meta_claims(nodes, [])
    assert dropped == 0 and len(kept_nodes) == 1
```

（`Node`/`Edge` 字段已对照 `backend/app/services/kg/models.py:20-35` 核实：`Edge` 必填 `id/type/source_id/target_id`，`evidence` 有默认值。）

- [ ] **Step 6: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/test_meta_claim_drop.py -x -q
```
Expected: FAIL（ImportError: drop_meta_claims）

- [ ] **Step 7: 实现管线接线**

`backend/app/services/kg/models.py:45`（`concepts_dropped: int = 0` 后）加：

```python
    claims_dropped: int = 0
```

`backend/app/services/kg_ingest.py`：import 行把 `is_noise_concept` 处补上 `is_meta_claim`；在 `drop_noise_concepts` 函数后加：

```python
def drop_meta_claims(nodes: List[Node], edges: List[Edge]) -> Tuple[List[Node], List[Edge], int]:
    """丢弃元叙述 Claim(讲文档自身的断言), 并移除悬空边。仅对 Claim 生效。"""
    kept_ids = set()
    kept_nodes: List[Node] = []
    dropped = 0
    for nd in nodes:
        if nd.type == "Claim" and is_meta_claim(nd.name)[0]:
            dropped += 1
            continue
        kept_ids.add(nd.id)
        kept_nodes.append(nd)
    kept_edges = [e for e in edges if e.source_id in kept_ids and e.target_id in kept_ids]
    return kept_nodes, kept_edges, dropped
```

`extract_graph` 末段（`drop_noise_concepts` 调用后、`canonicalize` 前）改为：

```python
    nodes, edges, concepts_dropped = drop_noise_concepts(nodes, edges, whitelist)
    nodes, edges, claims_dropped = drop_meta_claims(nodes, edges)
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes,
                          edges=edges, total_windows=len(pairs),
                          failed_windows=failed, windows_skipped=windows_skipped,
                          concepts_dropped=concepts_dropped,
                          claims_dropped=claims_dropped)
```

`backend/app/services/sqlite_repository.py:1429-1430` 的 run notes f-string 在 `concepts_dropped={graph.concepts_dropped}` 后追加 ` claims_dropped={graph.claims_dropped}`。

- [ ] **Step 8: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/kg/ -x -q
```
Expected: PASS（全部）

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/kg/filters.py backend/app/services/kg/models.py \
  backend/app/services/kg_ingest.py backend/app/services/sqlite_repository.py \
  backend/tests/kg/test_filters.py backend/tests/kg/test_meta_claim_drop.py
git commit -m "feat(kg): 元叙述 Claim 入库前确定性过滤

claim degraded 的主要构成是讲文档自身的元叙述('This book deals
with…'); 抽取 prompt 的软约束拦不全。窄口径正则零误杀优先,
与 drop_noise_concepts 对称, 丢弃计数进 extraction_run notes。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 3: 评测探针动词表修正（验收数字真实化）

**Files:**
- Modify: `backend/app/eval/probes.py:65-82`（`_VERB_RE`）
- Test: `backend/tests/eval/`（先 `ls backend/tests/eval/` 找既有探针测试文件；若无则新建 `backend/tests/eval/test_probes_claims.py`）

- [ ] **Step 1: 写失败测试**

```python
from app.eval.probes import claim_degraded


def test_real_assertions_not_degraded():
    # 此前因动词表缺词被误杀的真断言(主库实测样本)
    for s in [
        "MOS transistors continue to become faster, but at the cost of their 'analog' properties.",
        "Digital transition detector detects pulses and activates error detectors",
        "The oft-used Bode method falls short in some common systems",
        "Error signals filtered and converted to adjust VGA gain and VCO frequency",
        "The write precompensation circuitry delays the writing of the second 'one' to counter the shift",
    ]:
        assert claim_degraded(s) is False, s


def test_toc_titles_and_meta_still_degraded():
    for s in [
        "Relation Between Frequency Response and Time Response",  # 目录标题, 无动词
        "Effect of Negative Feedback on Distortion",
        "This book deals with the analysis and design of analog CMOS integrated circuits",  # 元叙述
        "Study of FinFETs",  # <4 词
    ]:
        assert claim_degraded(s) is True, s
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/eval/test_probes_claims.py -x -q
```
Expected: FAIL（`continue to become` 等被判 degraded）

- [ ] **Step 3: 补全动词表**

`probes.py` `_VERB_RE` 最后一个 alternation（`enable|model|models`）后追加：

```python
    r"|become(s)?|became|becoming|continue(s|d)?|detect(s|ed)?|activate(s|d)?"
    r"|fall(s)?|fell|converted|filtered|adjust(s|ed)?|delay(s|ed)?"
    r"|counter(s|ed)?|anticipate(s|d)?"
```

（插在 `...|leads?|enable|model|models)\b` 的 `models` 与 `)\b` 之间，保持单一 raw-string 拼接风格。）

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/eval/ -x -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/probes.py backend/tests/eval/test_probes_claims.py
git commit -m "fix(eval): claim 探针动词表补全, 消除真断言误杀

become/continue/detect/fall/converted 等缺失导致真技术断言被判
degraded(主库样本实测), 验收数字虚高。目录标题/元叙述仍正确命中。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 4: 全量验证 + 提 PR

- [ ] **Step 1: 跑全量检查**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/laughing-torvalds-22fb2d
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```
Expected: 全绿（py_compile + hermetic smoke + tsc）

- [ ] **Step 2: 3-way 合 master 后提 PR**

```bash
git fetch origin master && git merge origin/master   # 冲突则按语义解
git push -u origin claude/laughing-torvalds-22fb2d
gh pr create --base master --title "P0: KG 抽取去噪(frontmatter/元叙述claim) + 探针修正" \
  --body "$(cat <<'EOF'
## Summary
- 窗口过滤新增 frontmatter(preface/instructor指南/目录页)与 TOC-like 跳过
- 元叙述 Claim 入库前确定性过滤(窄口径零误杀), 计数进 run notes
- 评测探针 _VERB_RE 补全, 消除真断言误杀

对应 spec: docs/superpowers/specs/2026-06-12-two-tier-roadmap-design.md (P0)
为重抽 Gray/RF 与首个 base 库演练(P0.5)做代码准备。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: 等用户合并 PR 后再进 Phase 2**（Phase 2 在 root master 操作真实库，必须用合并后的代码）

---

## Phase 2 — 数据运维（在 `/Users/hzf/workspace/silicon_notebook` root master；操作真实库）

> ⚠️ 通用前提：每个 task 开始前确认后端已停（`pgrep -fl uvicorn`，有则停掉）——重抽脚本要求单写者。
> ⚠️ LLM 成本预警：Task 6 重抽两本大教材（Gray/RF 合计约 9,500 个现有 claim 的来源体量），按历史速率窗口数百个、墙钟约 1–2 小时、真实 LLM 费用。开跑前向用户确认一次。

### Task 5: 标 base + 新基线留档

- [ ] **Step 1: root master 更新代码**

```bash
cd /Users/hzf/workspace/silicon_notebook && git checkout master && git pull
pgrep -fl uvicorn   # 确认无后端进程; 有则按 README 停掉
```

- [ ] **Step 2: nb-012 标记为 base**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -c "
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
repo = SQLiteRepository(Settings())
repo.mark_notebook_base('nb-012fb94249')
print('tier=', repo.get_notebook('nb-012fb94249').tier)
"
```
Expected: `tier= base`

- [ ] **Step 3: 用新探针跑基线留档（A 侧）**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -c "
import json
from app.eval.probes import run_quality
res = run_quality('.local/silicon_notebook.db', 'nb-012fb94249')
open('/tmp/p0-baseline-probes.json','w').write(json.dumps(res, ensure_ascii=False, indent=1))
for book, types in sorted(res.items()):
    cl = types.get('claim', {})
    print(book[:36], 'claim_degraded_rate=', cl.get('degraded_rate'))
"
```
记录五本书的 `claim degraded_rate`（探针修正后的真实基线，预期略低于旧值 11.1%–21.3%）。

### Task 6: 重抽 Gray + RF（textbook + base_filter 双过滤生效）

- [ ] **Step 1: 启动重抽（后台 + 日志）**

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHONPATH=backend nohup /opt/homebrew/Caskroom/miniconda/base/bin/python \
  scripts/denoise_reextract_nb.py --sources src-34f4e77e21,src-eafffde53a \
  > /tmp/reextract-gray-rf.log 2>&1 &
```

说明：脚本硬编码 `NB='nb-012fb94249'` 正是目标；`--sources` 模式只替换这两源的 KG（其余三本不动），开头会把全 notebook 源 `doc_type` 统一置 `textbook`（顺带修复缺口），末尾自动 `rebuild_unified_kg`。

- [ ] **Step 2: 轮询直到完成**

```bash
tail -f /tmp/reextract-gray-rf.log   # 看到 "done: ok=2 failed=0" + "rebuild clusters: N" 为成功
```
若某源 failed（LLM 抖动）：重跑同命令、`--sources` 只写失败的那个源 id。

### Task 7: 验收（A/B 对比 + 烂簇打散 + 聚簇覆盖）

- [ ] **Step 1: probes B 侧复测**

跑 Task 5 Step 3 同款命令（输出存 `/tmp/p0-after-probes.json`）。
**验收线：Gray 与 RF 的 claim `degraded_rate` ≤ 0.10 且相对 A 侧降幅 ≥ 40%**；其余三本不回归（±2pp 内）。

- [ ] **Step 2: K-bulk 烂簇打散验证**

```bash
sqlite3 .local/silicon_notebook.db "
SELECT cc.canonical_id, COUNT(*), GROUP_CONCAT(json_extract(o.payload,'\$.name'),' | ')
FROM concept_clusters cc JOIN knowledge_objects o ON o.id=cc.member_object_id
WHERE cc.notebook_id='nb-012fb94249'
  AND LOWER(json_extract(o.payload,'\$.name')) IN ('drain','source','gate','bulk')
GROUP BY cc.canonical_id;"
```
Expected: drain/source/gate/bulk 各自归属**不同** canonical_id（不再共簇）；不应再出现 20 成员混合簇。

- [ ] **Step 3: 聚簇覆盖验证**

```bash
sqlite3 .local/silicon_notebook.db "
SELECT (SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id='nb-012fb94249'
        AND object_type='concept' AND status!='deprecated') AS concepts,
       (SELECT COUNT(DISTINCT member_object_id) FROM concept_clusters
        WHERE notebook_id='nb-012fb94249') AS clustered,
       (SELECT dirty FROM unified_kg_state WHERE notebook_id='nb-012fb94249') AS dirty;"
```
Expected: `clustered == concepts`、`dirty=0`。

- [ ] **Step 4: 强审门落库抽查**

```bash
sqlite3 .local/silicon_notebook.db "
SELECT status, COUNT(*) FROM knowledge_objects
WHERE notebook_id='nb-012fb94249' AND source_id IN ('src-34f4e77e21','src-eafffde53a')
GROUP BY status;"
```
Expected: 全部 `reviewed`（base 强审门生效）。其余三本仍 `approved`（历史数据，不动）。

- [ ] **Step 5: 把 A/B 数字记入演练报告草稿**（见 Task 13 模板的"P0 验收"节）

---

## Phase 3 — P0.5 两层机制演练（root master，起服务）

> 起服务按 README（root master、后端不带 `--reload`、`2>&1` 重定向）。演练问答用真实 LLM。每步把观察记入 Task 13 的报告草稿——**演练的产出就是报告**，记录格式：做了什么 / 期望 / 实际 / 问题编号。

### Task 8: 强审门 curator 确认流

- [ ] **Step 1: 起服务**

```bash
cd /Users/hzf/workspace/silicon_notebook/backend
nohup /opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 > /tmp/sn-backend.log 2>&1 &
cd ../frontend && nohup npm run dev > /tmp/sn-frontend.log 2>&1 &
curl -s http://127.0.0.1:8000/api/health
```

- [ ] **Step 2: 经 API 把一小批 reviewed 对象确认为 approved**

```bash
# 取 3 个 reviewed 对象 id
sqlite3 .local/silicon_notebook.db "SELECT id FROM knowledge_objects
WHERE notebook_id='nb-012fb94249' AND status='reviewed' LIMIT 3;"
# 逐个确认 (PATCH /api/notebooks/{nb}/knowledge/{id})
curl -s -X PATCH http://127.0.0.1:8000/api/notebooks/nb-012fb94249/knowledge/<id> \
  -H 'Content-Type: application/json' -d '{"status":"approved"}'
```
Expected: 200 + 状态翻转。**预期发现并记录**：上万 reviewed 对象无批量审批工具——记入报告"待办输入（P2 治理）"。

### Task 9: 联合检索演练

- [ ] **Step 1: 从 personal notebook 发问**

用 `nb-59ce4f4923`（personal）问一个 base 库才有的问题：

```bash
curl -s -X POST http://127.0.0.1:8000/api/notebooks/nb-59ce4f4923/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"两级运放的 Miller 补偿为什么会引入右半平面零点？"}' | \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m json.tool
```

- [ ] **Step 2: 验证并记录**

检查响应：`anchors[].tier` 出现 `base`；`grounded=true`；引用对象确属 nb-012。前端打开同一 notebook 问一次，确认引用上渲染 base 标记。记录命中质量与延迟（联合检索后候选池约 3.7 万对象）。

### Task 10: 晋升演练（personal → base）

- [ ] **Step 1: 在 personal notebook 选一条 claim 提晋升**

```bash
sqlite3 .local/silicon_notebook.db "SELECT id, json_extract(payload,'\$.name')
FROM knowledge_objects WHERE notebook_id='nb-59ce4f4923' AND object_type='claim' LIMIT 5;"
curl -s -X POST http://127.0.0.1:8000/api/notebooks/nb-59ce4f4923/knowledge/<id>/promote
curl -s http://127.0.0.1:8000/api/promotion-queue | /opt/homebrew/Caskroom/miniconda/base/bin/python -m json.tool
```

- [ ] **Step 2: 批准并验证去重入库**

```bash
curl -s -X POST http://127.0.0.1:8000/api/promotion-queue/<candidate_id>/approve
# 验证: base 库出现新对象(或证据并入既有对象), source_candidate_id 可追溯
sqlite3 .local/silicon_notebook.db "SELECT id, status, source_candidate_id
FROM knowledge_objects WHERE notebook_id='nb-012fb94249' AND source_candidate_id IS NOT NULL;"
```
记录：去重是否按预期（若 personal claim 与 base 既有 claim 等价，应合并证据而非新建）。

### Task 11: 冲突仲裁 baseline（P1 矛盾检测的对照组）

- [ ] **Step 1: 造一条与 base 相悖的 personal claim**

新建一个 md 文件上传到 `nb-59ce4f4923`（走真实抽取管线）：

```bash
cat > /tmp/conflict-note.md <<'EOF'
# 设计笔记

在饱和区，MOSFET 的漏极电流随 V_DS 线性增大，因此输出阻抗可以忽略沟道长度调制效应。
EOF
curl -s -X POST http://127.0.0.1:8000/api/notebooks/nb-59ce4f4923/sources \
  -F "files=@/tmp/conflict-note.md"
# 轮询 GET /api/sources/{id} 到 extracted
```

- [ ] **Step 2: fast 与 graph 两模式各问一次**

```bash
for MODE in fast graph; do
curl -s -X POST http://127.0.0.1:8000/api/notebooks/nb-59ce4f4923/ask \
  -H 'Content-Type: application/json' \
  -d "{\"question\":\"饱和区 MOSFET 漏极电流与 V_DS 是什么关系？\",\"mode\":\"$MODE\"}"
done
```
记录：答案是否以 base 为准并指出 personal 笔记的偏差（fast=prompt 软约束的真实表现；graph=`base_override` 是否触发）。**这组记录是 P1"矛盾检测硬化"的验收对照组，原样存入报告。**

### Task 12: 推导链问答 + 断链记录（P1 输入）

- [ ] **Step 1: graph 模式跑 3 道推导题**

```bash
for Q in "增益带宽积与相位裕度的关系如何推导?" \
         "为什么级联放大器的噪声系数主要由第一级决定? 给出推导链" \
         "从沟道长度调制推导共源放大器的本征增益上限"; do
curl -s -X POST http://127.0.0.1:8000/api/notebooks/nb-59ce4f4923/ask \
  -H 'Content-Type: application/json' \
  -d "{\"question\":\"$Q\",\"mode\":\"graph\"}" >> /tmp/graph-mode-runs.jsonl
echo >> /tmp/graph-mode-runs.jsonl
done
```

- [ ] **Step 2: 记录链路质量**

对每题记录：种子命中、多跳子图大小、`chain_trust` 值、答案里哪些跳有 KG 边支撑/哪些是 LLM 自由发挥（断链点）。**断链实例直接成为 P1"断链推断桥 + 缺口队列"的真实需求输入。**

### Task 13: 演练报告 + 收尾 PR

- [ ] **Step 1: 写报告** `docs/two-tier-pilot-report-2026-06.md`，结构：

```markdown
# 首个 base 库演练报告（nb-012, 2026-06）
## P0 验收数字（A/B）        ← Task 5/7 的探针对比、烂簇打散、聚簇覆盖
## 强审门                    ← Task 8 观察 + 批量审批缺失问题
## 联合检索                  ← Task 9 命中/标记/延迟
## 晋升                      ← Task 10 去重行为
## 冲突仲裁 baseline          ← Task 11 fast/graph 原始记录(P1 对照组)
## 推导链与断链实例           ← Task 12 记录(P1 需求输入)
## 问题清单与 roadmap 校准建议 ← 编号问题 → 归属 P1/P2/P3
```

- [ ] **Step 2: 提交 + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook
git checkout -b docs/two-tier-pilot-report && git add docs/two-tier-pilot-report-2026-06.md
git commit -m "docs: 首个 base 库演练报告(P0 验收 + P0.5 五项演练)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -u origin docs/two-tier-pilot-report
gh pr create --base master --title "docs: 首个 base 库演练报告" --body "P0 A/B 验收数字 + 两层机制五项演练记录, P1 需求输入(断链/冲突对照组)。

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## 验证基线（贯穿）

- Phase 1 每 task：`PYTHONPATH=backend ... pytest backend/tests/kg/ backend/tests/eval/ -q` 通过；Task 4 跑全量 `scripts/check.sh`。
- Phase 2/3 操作真实库前确认后端进程状态；重抽期间不起服务。
- 报告里的每个"问题"必须有编号和 roadmap 归属（P1/P2/P3 或新增），避免发现即丢失。
