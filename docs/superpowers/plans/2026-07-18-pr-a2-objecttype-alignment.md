# PR A2 · object_type 显示名对齐 Implementation Plan

> **⚠ PIVOT 2026-07-18(用户决定):迁移已回退,改前端为主。** 对齐 2 词(论断→结论/过程→步骤)
> 实测需 15 文件 schema 迁移 + 改备份校验器语义 + 重pin 行号锁 meta-test,为措辞精修 ROI 过低。
> **新范围**:后端 label 保持 `论断/过程` 不动(零迁移);只做前端一致性修复——
> Task 3(小表对齐当前后端)+ Task 2(跨栈守卫)+ Task 4(KnowledgeBrowser 用 API label)。
> Task 1(后端迁移)**作废**,含其「范围修订」段。下面正文的 `结论/步骤` 一律按 `论断/过程` 读。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让知识对象类型（object_type）在界面上显示对齐词汇表的中文名，消除「后端/前端两份 label 打架」和「同一 Knowledge 面板标签中文、条目英文」。

**Architecture:** object_type 的显示名真源在**后端** `OBJECT_TYPE_LABELS`（已通过 API `KnowledgeTypeCount.label` 下发，含自定义类型）。本 PR：①后端把 2 个词对齐词汇表（论断→结论、过程→步骤）并用条件式迁移更新存量库；②前端能拿到 API label 的调用点（KnowledgeBrowser）改用 API label；③拿不到 API label、只有 object_type 字符串的调用点（引用浮层、图节点）走一张前端小表，由跨栈守卫钉住 == 后端。

**Tech Stack:** Python（SQLite 迁移 + pytest）、TypeScript/React（Next.js）、`node --test`、跨栈契约脚本（照 `scripts/check_ask_modes_contract.py`）

设计依据：[2026-07-17-user-facing-vocabulary-design.md](../specs/2026-07-17-user-facing-vocabulary-design.md) §2.1

## Global Constraints

- **只改 2 个词**：`claim` `论断 Claim`→`结论 Claim`，`procedure` `过程 Procedure`→`步骤 Procedure`。`concept`（概念 Concept）、`formula`（公式 Formula）**一字不动**。
- **保留中英并排**（spec §2.1）：所有 object_type label 保持 `中文 English` 形态。英文半截**参与后端搜索匹配**（`query_store.py` 的 `needle not in f"{label} {headline} {body}"`），去掉会让搜 `Concept` 失效。前端小表也用同款「概念 Concept」以求全站一致。
- **迁移必须条件式**：`WHERE object_type=? AND label=<旧值> AND source='builtin'`。用户可以改内置类型的 label（`schema_registry.update_object_schema` 接受 `payload.label`，`UPDATE object_schemas SET label` 无 `source='builtin'` 守卫），迁移**绝不能**覆盖用户改过的 label。
- **迁移登记方式**：`migrate()` 用 `for version in range(current+1, SCHEMA_VERSION+1): getattr(self, f"_migration_{version}")()` 自动派发。加 `_migration_20` 方法 + `SCHEMA_VERSION = 19`→`20` 即可，**无需**显式注册。追加新迁移步，**不得**塞进已封版的 `_migration_1`（已部署库 user_version>=1 会短路 `_migration_1`，塞进去不执行）。
- **前端小表 == 后端**：`kg-type-mark.tsx` 的 `KG_TYPE_LABELS` 内置 4 项必须逐字等于后端 `OBJECT_TYPE_LABELS`，由 `scripts/check_object_type_labels_contract.py` 钉住。
- **kgTypeLabel 兜底 = 原 object_type**（自定义类型显示用户自己起的 id），**不再** TitleCase 成假英文（`evidence_tier`→`Evidence Tier` 那种泄漏）。用 `Object.hasOwn` 查表（PR A 的原型链教训：`map["constructor"]` 会返回函数）。
- **不碰 `vocabulary.ts`**：object_type 刻意不进 `vocabulary.ts`（真源在后端，spec §2.1）。
- **`OBJECT_TYPE_LABELS` 未进 LLM prompt**（已核 `prompts.py`/`kg/extract.py`），改词不影响抽取。
- 后端测试解释器：`PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python`。前端 `node_modules` 本 worktree 已装（PR A）。

---

### Task 0: 基线（改动前必须绿）

**Files:** 无（仅验证）

- [ ] **Step 1: 前端基线**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
npm run test 2>&1 | grep -E "^# (pass|fail)" && npx tsc --noEmit && echo "FE OK"
```

Expected: 全 pass，tsc 无错。

- [ ] **Step 2: 后端迁移测试基线（本 PR 首次跑后端，确认 harness 能起）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
PYTHONPATH=backend $PYTHON_BIN -m pytest backend/tests/test_schema_version_migration.py backend/tests/test_admin_users.py::test_migration_2_runs_on_already_v1_db -q 2>&1 | tail -5
```

Expected: 全 pass。若报缺依赖，先停下报告（环境问题，不是本 PR）。

---

### Task 1: 后端 label 对齐 + `_migration_20`（迁移风险隔离在此）

**Files:**
- Modify: `backend/app/services/extraction_profiles.py:88,90`（OBJECT_TYPE_LABELS 两词）
- Modify: `backend/app/repositories/sqlite/migrations.py`（加 `_migration_20`；`SCHEMA_VERSION` 19→20）
- Test: `backend/tests/test_object_type_label_migration.py`（新建）

**Interfaces:**
- Produces: 迁移后 `object_schemas` 中 builtin 的 `claim` label = `结论 Claim`、`procedure` = `步骤 Procedure`；`SCHEMA_VERSION == 20`。

- [ ] **Step 1: 写失败的迁移测试**

创建 `backend/tests/test_object_type_label_migration.py`：

```python
"""_migration_20: object_type 显示名对齐词汇表(论断→结论、过程→步骤)。
条件式,只动仍是旧默认值的 builtin 行——用户改过的 label 不覆盖。"""
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))


def test_schema_version_is_20():
    assert SCHEMA_VERSION == 20


def test_fresh_db_seeds_new_labels(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    repo = SQLiteRepository(Settings())  # fresh:_seed() 用新常量 INSERT
    with repo._connect() as db:
        rows = dict(db.execute(
            "SELECT object_type, label FROM object_schemas "
            "WHERE object_type IN ('claim','procedure','concept','formula')").fetchall())
    assert rows["claim"] == "结论 Claim"
    assert rows["procedure"] == "步骤 Procedure"
    assert rows["concept"] == "概念 Concept"
    assert rows["formula"] == "公式 Formula"


def test_deployed_db_old_builtin_labels_get_updated(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r1 = SQLiteRepository(Settings())
    # 模拟停在 v19、object_schemas 仍是旧 label 的已部署库
    with r1._write() as db:
        db.execute("UPDATE object_schemas SET label='论断 Claim' WHERE object_type='claim'")
        db.execute("UPDATE object_schemas SET label='过程 Procedure' WHERE object_type='procedure'")
        db.execute("PRAGMA user_version = 19")
    r2 = SQLiteRepository(Settings())  # 重开:_migration_20 跑
    with r2._connect() as db:
        rows = dict(db.execute(
            "SELECT object_type, label FROM object_schemas "
            "WHERE object_type IN ('claim','procedure')").fetchall())
        ver = db.execute("PRAGMA user_version").fetchone()[0]
    assert ver == SCHEMA_VERSION
    assert rows["claim"] == "结论 Claim"
    assert rows["procedure"] == "步骤 Procedure"


def test_migration_preserves_user_customized_label(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r1 = SQLiteRepository(Settings())
    # 用户把 claim 的 label 改成了自己的叫法(source 仍是 builtin)
    with r1._write() as db:
        db.execute("UPDATE object_schemas SET label='我的叫法' WHERE object_type='claim'")
        db.execute("PRAGMA user_version = 19")
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        label = db.execute(
            "SELECT label FROM object_schemas WHERE object_type='claim'").fetchone()[0]
    assert label == "我的叫法"  # WHERE label='论断 Claim' 不匹配 → 未覆盖
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
PYTHONPATH=backend $PYTHON_BIN -m pytest backend/tests/test_object_type_label_migration.py -q 2>&1 | tail -8
```

Expected: FAIL — `test_schema_version_is_20` 断言 20 但当前是 19；`test_deployed_..._get_updated` 拿到旧 label。

- [ ] **Step 3: 改 OBJECT_TYPE_LABELS 两词**

`backend/app/services/extraction_profiles.py` 第 88、90 行：

```python
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "concept": "概念 Concept",
    "claim": "结论 Claim",
    "formula": "公式 Formula",
    "procedure": "步骤 Procedure",
}
```

- [ ] **Step 4: 加 `_migration_20` + bump SCHEMA_VERSION**

`backend/app/repositories/sqlite/migrations.py` 顶部：`SCHEMA_VERSION = 19` → `SCHEMA_VERSION = 20`。

在 `_migration_19` 方法**之后**追加（grep `def _migration_19` 定位其结束，紧随其后加；缩进与 `_migration_19` 一致，都是类方法）：

```python
    def _migration_20(self) -> None:
        """object_type 显示名对齐词汇表:claim 论断→结论、procedure 过程→步骤。

        object_schemas 由 _seed()(每次构造跑,INSERT OR IGNORE)用 OBJECT_TYPE_LABELS.get()
        seed。改常量只影响 fresh 库(_seed 首次 INSERT 取新值);已部署库的行已存在,
        _seed 的 INSERT OR IGNORE 永不更新它 → label 永远停在旧值,必须靠本迁移 UPDATE。
        条件式:只动仍是旧默认值且 source='builtin' 的行——用户改过 label(schema_registry
        允许)的不覆盖。可重入:fresh 库上 WHERE label=<旧值> 匹配不到(已是新值)→ 安全空转。"""
        with self._connect() as db:
            db.execute(
                "UPDATE object_schemas SET label = ? "
                "WHERE object_type = 'claim' AND label = ? AND source = 'builtin'",
                ("结论 Claim", "论断 Claim"),
            )
            db.execute(
                "UPDATE object_schemas SET label = ? "
                "WHERE object_type = 'procedure' AND label = ? AND source = 'builtin'",
                ("步骤 Procedure", "过程 Procedure"),
            )
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
PYTHONPATH=backend $PYTHON_BIN -m pytest backend/tests/test_object_type_label_migration.py -q 2>&1 | tail -6
```

Expected: 4 个测试全 pass。

- [ ] **Step 6: 跑既有 schema 测试确认没打破版本闸不变量**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
PYTHONPATH=backend $PYTHON_BIN -m pytest backend/tests/test_schema_version_migration.py -q 2>&1 | tail -5
```

Expected: 全 pass（fresh 库盖章到 20、up-to-date 走快路径等不变量仍成立）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/extraction_profiles.py backend/app/repositories/sqlite/migrations.py backend/tests/test_object_type_label_migration.py
git commit -m "feat(be): object_type label 对齐词汇表(论断→结论/过程→步骤)+ _migration_20

改常量只影响 fresh 库;已部署库靠条件式 _migration_20 更新存量 builtin 行
(WHERE label=旧值 AND source='builtin',不覆盖用户改过的 label)。bump SCHEMA_VERSION
19→20,getattr 循环自动派发。label 参与后端搜索匹配故保留中英并排,只换中文词。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 跨栈守卫 `check_object_type_labels_contract.py`

**Files:**
- Create: `scripts/check_object_type_labels_contract.py`
- Modify: `scripts/check.sh`（挂上，照 `check_ask_modes_contract.py` 那行）
- Modify: `frontend/app/kg-type-mark.tsx`（Task 3 会把小表改成中文；本 task 的守卫针对改后的值——**Task 3 先于本 task 的实现，但两者同 PR**。若按序执行，先做 Task 3 再做本 task；若本 task 先跑，守卫会红，属预期）

**Interfaces:**
- Consumes: 后端 `OBJECT_TYPE_LABELS`（Task 1 改后）；前端 `KG_TYPE_LABELS`（Task 3 改后）。
- Produces: `scripts/check_object_type_labels_contract.py`，`check.sh` 硬失败门。

> 执行顺序提示：**先做 Task 3**（把前端小表改成中文），再做本 Task 2（守卫），这样守卫一写就是绿的。计划把守卫单列成 task 是因为它是独立的可评审交付物。

- [ ] **Step 1: 写守卫脚本**

创建 `scripts/check_object_type_labels_contract.py`（照 `scripts/check_ask_modes_contract.py` 的结构：import 后端常量 + 正则解析前端 TS + 比对）：

```python
#!/usr/bin/env python3
"""跨栈契约:前端 kg-type-mark.tsx 的 KG_TYPE_LABELS 内置 4 项必须逐字等于后端
OBJECT_TYPE_LABELS。任一侧改了 object_type 的显示名而另一侧没跟,这里失败。
由 scripts/check.sh 运行。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.services.extraction_profiles import OBJECT_TYPE_LABELS  # noqa: E402


def frontend_labels() -> dict[str, str]:
    text = (ROOT / "frontend/app/kg-type-mark.tsx").read_text(encoding="utf-8")
    m = re.search(r"const KG_TYPE_LABELS[^{]*\{(.*?)\};", text, re.S)
    if not m:
        raise SystemExit("kg-type-mark.tsx: KG_TYPE_LABELS 对象字面量未找到")
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', m.group(1)))


def main() -> int:
    backend = dict(OBJECT_TYPE_LABELS)
    frontend = frontend_labels()
    if backend != frontend:
        print("object_type label 跨栈契约 MISMATCH", file=sys.stderr)
        print(f"  backend : {backend}", file=sys.stderr)
        print(f"  frontend: {frontend}", file=sys.stderr)
        only_b = {k: backend[k] for k in backend if backend.get(k) != frontend.get(k)}
        print(f"  差异(以 backend 为准): {only_b}", file=sys.stderr)
        return 1
    print(f"object_type label 契约 OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 跑守卫（Task 3 已完成时应绿）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
PYTHONPATH=backend $PYTHON_BIN scripts/check_object_type_labels_contract.py
```

Expected: `object_type label 契约 OK: ['claim', 'concept', 'formula', 'procedure']`。

- [ ] **Step 3: 挂进 check.sh**

在 `scripts/check.sh` 里 `check_ask_modes_contract.py` 那一行**之后**加一行同款：

```bash
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/check_object_type_labels_contract.py"
```

- [ ] **Step 4: 验证守卫真能抓漂移**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
# 临时把前端 claim 改回旧值,守卫应变红
sed -i.bak 's/claim: "结论 Claim"/claim: "论断 Claim"/' app/kg-type-mark.tsx
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check_object_type_labels_contract.py; echo "退出码=$? (应非0)"
mv frontend/app/kg-type-mark.tsx.bak frontend/app/kg-type-mark.tsx  # 恢复
```

Expected: MISMATCH + 退出码非 0；恢复后再跑绿。

- [ ] **Step 5: 提交**

```bash
git add scripts/check_object_type_labels_contract.py scripts/check.sh
git commit -m "test(guard): 跨栈守卫钉住前端 KG_TYPE_LABELS == 后端 OBJECT_TYPE_LABELS

照 check_ask_modes_contract.py:任一侧改 object_type 显示名另一侧没跟就失败。
补上 severity 那次暴露的教训——object_type 有两份真源(前后端),必须钉。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 前端小表对齐 + 兜底不泄漏（**先于 Task 2 实现**）

**Files:**
- Modify: `frontend/app/kg-type-mark.tsx`（`KG_TYPE_LABELS` 英文→中英并排；`kgTypeLabel` 兜底 + Object.hasOwn）

**Interfaces:**
- Produces: `kgTypeLabel(type)` — 内置类型返回「概念 Concept」等（== 后端）；未知/自定义返回原 `type`。

**背景：** 现状 `KG_TYPE_LABELS` 是纯英文（`concept:"Concept"`），且 `kgTypeLabel` 兜底把未知 snake_case TitleCase 成假英文（`evidence_tier`→`Evidence Tier`）——泄漏。这张小表服务「只有 object_type 字符串、拿不到 API label」的调用点（引用浮层 `answer-panel.tsx`、图节点/图例 `page.tsx`）。

- [ ] **Step 1: 确认现状**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
sed -n '/const KG_TYPE_LABELS/,/^}/p' frontend/app/kg-type-mark.tsx
sed -n '/export function kgTypeLabel/,/^}/p' frontend/app/kg-type-mark.tsx
```

Expected: 看到英文表 + TitleCase 兜底。

- [ ] **Step 2: 改小表 + 兜底**

`frontend/app/kg-type-mark.tsx`，把 `KG_TYPE_LABELS` 与 `kgTypeLabel` 换成：

```ts
// 内置类型显示名——逐字等于后端 OBJECT_TYPE_LABELS(extraction_profiles.py),
// 由 scripts/check_object_type_labels_contract.py 钉住。中英并排是刻意的:
// 后端同款 label 参与搜索匹配,前端保持一致以求全站统一(spec §2.1)。
const KG_TYPE_LABELS: Record<string, string> = {
  concept: "概念 Concept",
  claim: "结论 Claim",
  formula: "公式 Formula",
  procedure: "步骤 Procedure",
};

export function kgTypeLabel(type: string): string {
  // 内置类型走小表;自定义/未知类型显示原 object_type(用户自己起的 id)——诚实,
  // 不再 TitleCase 成假英文(evidence_tier → "Evidence Tier" 那种泄漏)。能拿到
  // API label 的调用点(KnowledgeBrowser,Task 4)走 API label 覆盖自定义类型中文名。
  // Object.hasOwn 而非 KG_TYPE_LABELS[type]:后者走原型链,map["constructor"] 会返回函数。
  return Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;
}
```

（`KG_TYPE_STYLE`、`KgTypeMark` 组件不动。）

- [ ] **Step 3: 验证 tsc + build（.tsx 不可 node --test,由守卫 + 构建把关）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
npx tsc --noEmit && echo "tsc OK"
```

Expected: tsc 无错。（`kg-type-mark.tsx` 是 `.tsx`，`node --test` 无法 import——表值正确性由 Task 2 守卫覆盖，兜底行为由 Task 5 的 build + 人工核。）

- [ ] **Step 4: 提交**

```bash
git add frontend/app/kg-type-mark.tsx
git commit -m "fix(fe): kg-type-mark 小表对齐后端(中英并排)+ 兜底显示原 type 不再泄漏假英文

英文表 concept:\"Concept\" → \"概念 Concept\"(逐字 == 后端 OBJECT_TYPE_LABELS)。
TitleCase 兜底(evidence_tier→Evidence Tier)改成显示原 object_type,并用 Object.hasOwn
避免原型链(PR A 教训)。服务只有 object_type 字符串的调用点(引用浮层/图节点)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: KnowledgeBrowser 条目用 API label（修「标签中文/条目英文」bug）

**Files:**
- Modify: `frontend/app/page.tsx`（`KnowledgeBrowser` 内，条目类型名从 `kgTypeLabel(...)` 改用 `types` 里的 API label）

**Interfaces:**
- Consumes: `KnowledgeBrowser` 已有的 props `types: KnowledgeTypeCount[]`（含 `.label`）和 `kind`（当前选中类型）。

**背景（现存 bug）：** KnowledgeBrowser 顶部 tab 用 `t.label`（API label = 中文「概念 Concept」），但每个知识条目用 `kgTypeLabel(item.object_type ?? kind)`（前端小表，改前是英文）——**同一面板标签中文、条目英文**。且自定义类型只有 API label 有正确中文名，小表没有。改用 API label 一并解决。

- [ ] **Step 1: 定位（行号已因合并漂移，以 grep 为准）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
grep -nF 'kgTypeLabel(item.object_type ?? kind)' frontend/app/page.tsx
grep -n 'function KnowledgeBrowser' frontend/app/page.tsx
# 确认 KnowledgeBrowser 的 props 有 types 和 kind:
grep -nE 'types: KnowledgeTypeCount|kind: KnowledgeKind|types,|kind,' frontend/app/page.tsx | head
```

Expected: 找到条目渲染行 `<span>{kgTypeLabel(item.object_type ?? kind)}</span>`，且 KnowledgeBrowser 收 `types` 和 `kind`。

- [ ] **Step 2: 在 KnowledgeBrowser 渲染体顶部建 label 查找表**

在 `KnowledgeBrowser` 函数体内、`return (` 之前（`const tabs = types.map(...)` 附近）加一行：

```ts
  const typeLabelBy = new Map(types.map((t) => [t.object_type, t.label]));
```

- [ ] **Step 3: 条目改用 API label，兜底回 kgTypeLabel**

把 `<span>{kgTypeLabel(item.object_type ?? kind)}</span>` 改为：

```tsx
                <span>{typeLabelBy.get(item.object_type ?? kind) ?? kgTypeLabel(item.object_type ?? kind)}</span>
```

（有 API label（含自定义类型中文名）就用；万一 `types` 里没有该类型，兜底回小表——内置中文 / 未知原 type。`kgTypeLabel` 仍需 import，保留。）

- [ ] **Step 4: 验证 tsc**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
npx tsc --noEmit && echo "tsc OK"
```

Expected: 无错。（page.tsx 是 `.tsx`，Task 5 用 build + bundle grep + 人工核验证。）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "fix(fe): KnowledgeBrowser 条目类型名改用 API label,修标签中文/条目英文

顶部 tab 用 t.label(中文),条目却用 kgTypeLabel(英文)——同一面板两套。改用 types
里的 API label(含自定义类型正确中文名),兜底回小表。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 收口验证 + PR

**Files:** 无改动，仅验证

- [ ] **Step 1: 全量 check.sh（含新守卫 + 后端 pytest + 前端三件套）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh 2>&1 | tail -15
```

Expected: 全绿，含 `object_type label 契约 OK`。

- [ ] **Step 2: 编译产物验证对齐落地（照 PR A 的 bundle grep）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
grep -rl "结论 Claim\|步骤 Procedure" .next/static/chunks/ 2>/dev/null | head -1 && echo "→ 新词进了 bundle"
grep -rlo '"Concept"\|"Claim"' .next/static/chunks/*.js 2>/dev/null | head -1 && echo "⚠ 仍有裸英文类型名" || echo "→ 无裸英文类型名"
rm -rf .next
```

Expected: 新词在 bundle；无裸 `"Concept"`/`"Claim"` 英文类型名。

- [ ] **Step 3: 确认没碰禁区**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
git diff --stat origin/master...HEAD -- frontend/app/vocabulary.ts
```

Expected: **无输出**——object_type 不进 vocabulary.ts（spec §2.1）。

- [ ] **Step 4: rebase + 开 PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
git fetch origin master --quiet
git rebase origin/master
git push -u origin claude/pr-a2-objecttype
gh pr create --base master --title "feat: object_type 显示名对齐词汇表(论断→结论/过程→步骤)+ 跨栈守卫" --body "$(cat <<'BODY'
## 做了什么

「面向用户的表达层」整改 PR A2。object_type(知识对象类型)的显示名对齐词汇表,并消除两处不一致。

- **后端**:`OBJECT_TYPE_LABELS` 论断→结论、过程→步骤(concept/formula 不动)。`_migration_20`
  条件式更新存量 builtin 行(`WHERE label=旧值 AND source='builtin'`,不覆盖用户改过的 label);
  bump SCHEMA_VERSION 19→20。label 参与后端搜索匹配,故保留中英并排,只换中文词。
- **跨栈守卫**:`check_object_type_labels_contract.py` 钉住前端小表 == 后端(severity 那次暴露
  的教训——object_type 有前后端两份真源)。
- **前端**:引用浮层/图节点用的小表英文→中英并排(逐字 == 后端)+ 兜底显示原 type 不再泄漏
  假英文;KnowledgeBrowser 条目改用 API label,修「标签中文/条目英文」bug + 自定义类型中文名。

## 验证

`scripts/check.sh` 全绿(含新守卫 + 迁移测试:存量更新/用户自定义不覆盖/fresh 直接 seed)。
编译产物确认新词进 bundle、无裸英文类型名。

设计:`docs/superpowers/specs/2026-07-17-user-facing-vocabulary-design.md` §2.1

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

---

## Self-Review

**Spec §2.1 覆盖对照：**

| §2.1 要求 | 本计划 task |
|---|---|
| OBJECT_TYPE_LABELS 论断→结论、过程→步骤 | Task 1 Step 3 |
| 条件式迁移更新存量 builtin 行 | Task 1 Step 4（`WHERE label=旧值 AND source='builtin'`）|
| 迁移测试：存量更新 / 用户改过不覆盖 / fresh seed | Task 1 Step 1（4 个测试）|
| bump SCHEMA_VERSION，追加 `_migration_N` 不塞旧迁移 | Task 1 Step 4 + Global Constraints |
| 保留中英并排（label 参与搜索） | Global Constraints + Task 1/3 用「概念 Concept」形态 |
| 删前端重复英文表，改用 API label / 小表 | Task 3（小表对齐）+ Task 4（KnowledgeBrowser 用 API label）|
| TitleCase 兜底改掉 | Task 3 Step 2 |
| 跨栈守卫钉住小表 | Task 2 |
| OBJECT_TYPE_LABELS 未进 prompt，改词不影响抽取 | Global Constraints（已核）|

**超出 §2.1 但正当的：** §2.1 未点名「KnowledgeBrowser 标签中文/条目英文」这个现存 bug——探索时发现（tab 用 `t.label`、条目用 `kgTypeLabel`），Task 4 顺带修掉，因为它正是 object_type 显示不一致的一部分。

**Placeholder / 类型一致性：** 迁移用真实 harness idiom（`SQLiteRepository(Settings())` + `_write()`/`_connect()` + `PRAGMA user_version`，取自 `test_admin_users.py:72`）；守卫照 `check_ask_modes_contract.py`；`KG_TYPE_LABELS`/`OBJECT_TYPE_LABELS`/`typeLabelBy` 命名前后一致。无 TBD/TODO。

**测试可达性说明（不是缺陷，是约束）：** `page.tsx` / `kg-type-mark.tsx` 是 `.tsx`，`node --test` 无法 import（PR A 已证 `ERR_UNKNOWN_FILE_EXTENSION`）。故 Task 3/4 无单测——表值正确性由 Task 2 跨栈守卫覆盖，渲染由 Task 5 build + bundle grep + 人工核。这与 PR A 对 page.tsx 改动的验证方式一致。

**执行顺序注意：** Task 2（守卫）依赖 Task 3（前端小表已对齐）才绿。建议实现顺序 Task 1 → **Task 3 → Task 2** → Task 4 → Task 5；计划按逻辑分组编号，执行时按此依赖序。
