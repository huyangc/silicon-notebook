# Knowhow 表：版本管理（变更流水 + 回退）

- 日期：2026-07-22
- 状态：设计定稿，待实现
- 关联：`2026-07-15-knowhow-tables-design.md`（格子级节点模型）、`2026-07-16-knowhow-anchor-grouping-display.md`（合并格批量写）
- 触发需求：用户希望能看到上一次的编辑、整体变化情况，并能回退

---

## 1. 背景与动机

knowhow 表是用户手工精心维护的知识资产：逐格填写长富文本、LLM 优化/规整回填、批量追加导入、行列结构调整。当前这些变更**全部就地覆盖，无任何痕迹**：

- 改错了一格，原文永久丢失
- LLM 优化/规整改了什么，回填后无从对照（`scripts/backfill_knowhow_md.py` 一次改 25 格，只能靠离线备份 JSON 兜底）
- 多人协作时无法回答"这格是谁什么时候改的"
- 结构性误操作（删列/删行）不可逆，`ON DELETE CASCADE` 会带走整列/整行的所有格子和代码附件

本设计给 knowhow 表加版本管理：自动记录每次变更、可命名里程碑、可查看单次改动与任意两版对比、可整表回退到任意历史点、可单格恢复到历史某值。

## 2. 用户决策（已拍板，实现时不要再问）

| 议题 | 决策 |
|---|---|
| 版本单位 | 自动流水（每次保存一条）**+** 可命名里程碑，两者都要 |
| 回退形态 | 整表回到某时间点/里程碑 **+** 单格恢复到历史某值；**不做**"撤销中间某一条" |
| 覆盖范围 | 全盖：格子内容 + 行增删 + 列增删改名改类型 + 表标题描述 + 行标题列设置 + 代码附件 |
| 查看形态 | 时间线流水（点开看单次 diff）+ 任意两版整表对比 + 单格历史时间线；**不做**主网格上的"最近改过"标注 |
| 保留策略 | 永久保留 + 手动"清理 N 天前"入口，**不自动裁剪** |
| 回退后旧历史 | 保留（回退只是新增一条），可"回退的回退" |
| 单格历史位置 | 格子浮窗第三态页签（预览 / 编辑 / 历史） |
| 历史引用的图片 | 保护，不被孤儿清扫器回收（代价：图片进过格子就基本不再自动回收，靠"清理历史"释放） |
| 表复制/移动 | 历史**都不带**，目标表只记一条来源流水 |
| 存储方案 | 方案 A：变更流水（delta）+ 反向重放 + 指纹守卫；里程碑零存储 |

## 3. 方案选型与理由

### 3.1 候选

- **A（选中）**：变更流水存受影响实体的 before/after + 每条记变更后的整表指纹；回退 = 从当前逆序重放 before；里程碑 = 给流水序号起名的纯标签
- **B**：整表快照式（照抄既有 `memory_revisions`），每次变更存一份 `snapshot_table()` JSON
- **C**：混合，流水 + 每 N 条自动打整表快照，回退 = 最近快照 + 前向重放

### 3.2 选 A 的理由

1. **A 的头号风险已有现成解药。** delta 方案最怕"某条写路径漏挂钩 → 重放静默错位"。仓库里已有 `KnowhowTransferStore.table_fingerprint()` / `_fingerprint_on(db, table_id)`——为 `move_table` 的并发编辑守卫所写、扛过四轮评审的 SHA-256 整表指纹，其覆盖范围**恰好等于**本设计的"全盖"范围（见 §4.3）。这让 A 从"需要小心翼翼"降级为"有硬校验兜底"。
2. **空间比 B 省一到两个数量级。** 一张百行表的整表快照约 400KB（压缩后 ~100KB），B 方案下"改一个格子写 400KB"，几千次编辑累积到几百 MB/表，违反效率一等约束。A 只存受影响格子。
3. **三个查看诉求里两个在 A 下是直接查询**（时间线、单格历史），在 B 下反而要现算快照 diff。
4. **里程碑在 A 下零存储**（给序号起名），B/C 下则是又一份快照。
5. C 最稳但两套机制要互相保持一致，实现量约为 A 的 1.6 倍，收益不抵成本。

### 3.3 已纠正的错误假设

`knowhow_tables.mutation_seq` **不是**完备版本号：`add_knowhow_column` / `rename_knowhow_column` / `set_knowhow_column_kind` / `delete_knowhow_column` **故意不 bump 它**（它只服务投影调度，只关心内容变化）。因此流水必须用自己的 `seq`，不能复用 `mutation_seq`。

## 4. 数据模型

### 4.1 `knowhow_changes` —— 变更流水

```sql
CREATE TABLE IF NOT EXISTS knowhow_changes (
  id            TEXT PRIMARY KEY,
  table_id      TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  actor         TEXT NOT NULL DEFAULT '',
  origin        TEXT NOT NULL DEFAULT 'user',
  payload_json  TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  UNIQUE(table_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_knowhow_changes_table
  ON knowhow_changes(table_id, seq DESC);
```

- `seq`：本表内单调递增，从 1 开始，在写事务内以 `COALESCE(MAX(seq),0)+1` 计算（写锁保证不重复）
- `kind`：`table_create` / `table_meta` / `anchor_set` / `column_add` / `column_rename` / `column_kind` / `column_delete` / `row_add` / `row_delete` / `cell_update` / `cell_code_put` / `cell_code_delete` / `import_append` / `revert`
- `origin`：`user` / `llm_optimize` / `llm_reformat` / `import` / `agent` / `revert` / `backfill`——时间线上一眼看出"这批是 LLM 改的"
- `fingerprint`：本次变更 COMMIT 后的整表指纹（§4.3）
- `note`：回退时写「回退到 #12」；传输时写「由 <源表标题> 复制而来」（§7.3）

表删除时流水随 `ON DELETE CASCADE` 一并消失——表都没了，历史无意义。

### 4.2 `knowhow_milestones` —— 命名里程碑（零快照）

```sql
CREATE TABLE IF NOT EXISTS knowhow_milestones (
  id          TEXT PRIMARY KEY,
  table_id    TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  name        TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  UNIQUE(table_id, name)
);
CREATE INDEX IF NOT EXISTS idx_knowhow_milestones_table
  ON knowhow_milestones(table_id, seq DESC);
```

里程碑只是给某个 `seq` 起名，不存任何内容。`seq` 不设 FK 到 `knowhow_changes`（流水被"清理历史"删除后，里程碑保留为"已失效标记"，UI 上灰显并提示不可回退；不要级联删掉用户命过名的东西）。

迁移：`_migration_24`，`SCHEMA_VERSION` 23 → 24。两张全新表，无存量数据顾虑，采用与 `_migration_16`/`_migration_17` 相同的两层写法（只新建表，不改 `_migration_1` baseline）。

### 4.3 指纹：复用既有 `_FINGERPRINT_SQL`

`KnowhowTransferStore._FINGERPRINT_SQL` 在**一条 SELECT** 内算出：

| 分量 | 内容 |
|---|---|
| 表元 | `title`、`description` |
| 列 | `id ∥ name ∥ role ∥ position`，按 `id` 排序 |
| 行 | `id ∥ position`，按 `id` 排序 |
| 格子 | `row_id ∥ column_id ∥ content_md` |
| 代码附件 | `row_id ∥ column_id ∥ code_text ∥ language ∥ cell_content_hash ∥ updated_by` |

五个分量以 `\x1d` 连接后 SHA-256。

两个关键性质：

1. **覆盖范围恰好等于本设计的"全盖"范围**（行标题列设置 = 列的 `role='anchor'`，已含）。
2. **不含 `updated_at` 时间戳**。这是后置守卫能成立的前提——回退会写新的时间戳，若指纹覆盖时间戳，守卫将永远失败。**任何将来修改 `_FINGERPRINT_SQL` 的人必须知道它现在同时是回退正确性的判据**，需在该常量旁加注释说明这层新依赖。

### 4.4 payload 形状（按 kind）

可逆性是唯一不能省的约束：**删除类变更必须存够重建的全部信息**。

```jsonc
// cell_update —— 合并格批量写 / 批量规整就是多个条目，仍是一条流水
{"cells": [{"row_id": "...", "column_id": "...",
            "before": "旧 md 或 null(格子当时不存在)", "after": "新 md"}]}

// row_add / import_append —— before 为空；created_at 是这行真实的创建时间
{"rows": [{"row_id": "...", "position": 3, "created_at": "...",
           "cells": {"<column_id>": "md"},
           "code":  [{"column_id": "...", "code_text": "...", "language": "py",
                      "cell_content_hash": "...", "updated_by": "..."}]}]}

// row_delete —— after 为空；必须存整行所有格子 + 代码附件（CASCADE 会带走它们）
// + created_at（同 row_add，重建时原样恢复，不是重建那一刻的 now()）
{"rows": [ /* 同 row_add 形状 */ ]}

// column_add —— before 为空
{"column": {"id": "...", "name": "...", "role": "attribute", "position": 4}}

// column_delete —— 必须存列定义 + 该列所有格子内容 + 该列所有代码附件
{"column": { /* 同上 */ },
 "cells":  [{"row_id": "...", "content_md": "..."}],
 "code":   [{"row_id": "...", "code_text": "...", "language": "...",
             "cell_content_hash": "...", "updated_by": "..."}]}

// column_rename / column_kind
{"column_id": "...", "before": "旧值", "after": "新值"}

// anchor_set —— 行标题列切换会同时改两列的 role
{"columns": [{"column_id": "...", "before": "anchor", "after": "attribute"},
             {"column_id": "...", "before": "attribute", "after": "anchor"}]}

// table_meta
{"before": {"title": "...", "description": "..."},
 "after":  {"title": "...", "description": "..."}}

// cell_code_put / cell_code_delete
{"row_id": "...", "column_id": "...",
 "before": {"code_text": "...", "language": "...", "cell_content_hash": "...",
            "updated_by": "..."} /* 或 null */,
 "after":  { /* 同形状，或 null */ }}

// table_create —— 初始结构（导入建表则含所有初始行）
{"table": {"title": "...", "description": "..."},
 "columns": [ ... ], "rows": [ ... ]}

// revert —— 它自己也必须可逆，所以 payload 是"这次回退所改动的一切"的
// before/after，形状 = 上述各类的并集，外加 target_seq 元信息。
// 注意 before = 回退前（即 head）的状态，after = 回退后（即 T）的状态。
{"target_seq": 12,
 "cells":   [{"row_id": "...", "column_id": "...", "before": "...", "after": "..."}],
 "rows_removed": [ /* row_add 形状：回退删掉的行，含全部格子+代码，供再回退时重建 */ ],
 "rows_added":   [ /* row_add 形状：回退重建的行 */ ],
 "columns_removed": [ /* column_delete 形状 */ ],
 "columns_added":   [ /* column_delete 形状 */ ],
 "columns_changed": [{"column_id": "...", "before": {"name","role","position"},
                      "after": {"name","role","position"}}],
 "table_meta": {"before": {...}, "after": {...}},
 "code": [{"row_id","column_id","before","after"}]}
```

`payload_json` 的**大小上界**：一次 `column_delete` 会存整列内容（百行表约 50KB），一次 `import_append` 会存所有新行。这是可逆性的必要代价，且这两类操作低频。

## 5. 挂钩机制

### 5.1 `record_change`

**模块级函数**，住在 `backend/app/repositories/sqlite/knowhow_history_store.py`：

```python
def record_change(db, *, new_id, now, table_id, kind, payload,
                  actor="", origin="user", note="") -> int:
    """在调用方已开的写事务内追加一条流水，返回 seq。

    必须是写事务的最后一步——fingerprint 要反映本次变更 COMMIT 后的状态。
    不自己开事务：接调用方的 db 连接，与变更本体同生共死。
    """
```

职责：算 `seq = COALESCE(MAX(seq),0)+1` → 调 `knowhow_fingerprint.fingerprint_on(db, table_id)` 算指纹 → INSERT。

**为什么是模块级函数而不是 `KnowhowStore` 的方法**：它必须在调用方已开的写事务里跑。做成类/方法就要在组合根接线并让 `KnowhowStore` 持有引用；模块级函数零状态零接线，把 `new_id`/`now` 当参数传进去即可。自带事务的操作（查询/里程碑/prune/回退）才归同文件的 `KnowhowHistoryStore` 类，它在组合根构造，与 `knowhow_store` **共用同一对 `new_id`/`now` 可调用对象**。

`actor` / `origin` 由 service 层（`app/services/knowhow/api.py`）传参进来，**不在 store 层读 ContextVar**——这是既有约定（`create_knowhow_table(created_by=…)` 就是这么传的）。store 方法签名新增可选 `actor` / `origin` / `note` 参数，默认值保证既有调用点不破。

### 5.2 挂钩覆盖与豁免白名单

`knowhow_store.py` 现有 24 个 `with self.database.write() as db:` 块。分类：

**必须挂钩（改用户可见内容）**：`create_knowhow_table`、`update_knowhow_table_meta`、`set_knowhow_anchor_column`、`add_knowhow_column`、`rename_knowhow_column`、`set_knowhow_column_kind`、`delete_knowhow_column`、`add_knowhow_row`、`delete_knowhow_row`、`update_knowhow_cell`、`update_knowhow_cells`、`update_knowhow_cells_bulk_guarded`、`update_knowhow_cells_guarded_atomic`、`upsert_knowhow_cell_code`、`delete_knowhow_cell_code`

**豁免白名单（不改用户可见内容）**：
- `bump_knowhow_mutation_seq`——纯投影调度计数器
- `set_knowhow_row_projection` / `set_knowhow_row_projection_if_table_seq`——投影状态机
- `set_knowhow_hidden_source`——隐藏合成源接线
- `insert_notebook_asset` / `delete_source_asset_rows`——资产表，不属于 knowhow 表内容
- `delete_knowhow_table`——表连同流水一起 CASCADE 消失，记了也读不到

### 5.3 架构守卫

新增测试，扫 `knowhow_store.py` 里所有 `with self.database.write() as db:` 块，断言每块**要么**在其所属方法内调用了 `record_change`，**要么**方法名在显式豁免白名单里。白名单意味着**将来新增写方法默认报红**。

该守卫必须做变异验证（见 §9）：删掉一处 `record_change` 要真红；把一个写块**移动**到别的方法里也要真红（只做删除变异不够——源码断言的 `[\s\S]*?` 会越过块的收尾大括号，须先 slice 到具体方法体再断言）。

### 5.4 批量操作记一条不记 N 条

- `commit_append`（导入追加几十行）→ 一条 `import_append`，payload 含所有新行
- 合并格批量写（`update_knowhow_cells`）→ 一条 `cell_update`，`cells` 数组含所有行
- 批量规整（`update_knowhow_cells_guarded_atomic`）→ 一条 `cell_update`，`origin='llm_reformat'`

否则时间线会被刷屏，且"上次编辑改了什么"失去意义。

## 6. 回退

### 6.1 整表回退到 `seq = T`

全部在**一个写事务**内：

1. **权限**：写权限（owner 或 `notebook_members` write）。
2. **陈旧校验**：请求体带 `expected_head_seq`（前端看到的最新 seq）。与库内实际 head 不符 → **409** `knowhow_history_stale`。
3. **前置指纹守卫**：`_fingerprint_on(db, table_id) == 最新一条流水的 fingerprint`。不等 → **400** `knowhow_history_inconsistent`，中止。这同时挡住"有写路径漏挂钩"和"有人直接改过库"。**必须在同一事务内做**，不能先查后写（TOCTOU）。
4. **逆序重放**：从 head 倒着走到 `T+1`，逐条应用 `payload.before`：

   | kind | 逆操作 |
   |---|---|
   | `cell_update` | 每格写回 `before`；`before` 为 null → 删该格 |
   | `row_add` / `import_append` | 删这些行（CASCADE 带走格子和代码） |
   | `row_delete` | **用原 `row_id` / `position`** 重建行 + 所有格子 + 所有代码附件 |
   | `column_add` | 删该列 |
   | `column_delete` | **用原 `column_id` / `position` / `name` / `role`** 重建列 + 该列所有格子 + 代码附件 |
   | `column_rename` / `column_kind` | 写回 `before` |
   | `anchor_set` | 两列 role 都写回 `before` |
   | `table_meta` | 写回 `before` |
   | `cell_code_put` / `cell_code_delete` | 写回 `before`；`before` 为 null → 删附件 |
   | `table_create` | 不可跨越（`T ≥ 1` 保证不会走到这里） |
   | `revert` | 它自身也是普通流水，payload 记录了它改动的一切的 before（§4.4），同样机械可逆 |

5. **后置指纹守卫**：重算指纹，必须 `== T 那条流水的 fingerprint`。不等 → **整事务回滚** + **500** `knowhow_revert_verify_failed`，并记一条事件便于排查。这是重放正确性的硬证明。
6. 同一事务追加一条 `kind='revert'` 的新流水（`origin='revert'`，`note='回退到 #T'`），其 `fingerprint` 自然等于 T 那条的 fingerprint（表状态已相同）。所有行标 `projection_status='pending'`，bump `mutation_seq`。
7. **事务提交后**触发重投影：调 `knowhow_api.get_scheduler(repo).schedule(table_id)`——与 `POST .../reproject` 逃生口**完全同一个入口**。

   两点澄清（实现时容易搞错）：`KnowhowProjector.project_table` 本身**永远是全量确定性重投影**，不存在需要绕开的"增量投影函数"；而"所有改动 knowhow 内容的路径都必须走 `ProjectionScheduler`、绝不直接 `background_jobs.submit` 或直调 `project_table`"是本仓库反复申明的约定——绕过调度器会丢掉 per-table 防抖与单飞，和同一张表上并发的编辑请求打架。

### 6.2 id 稳定性（硬约束）

重建行/列时**写回原 `row_id` / `column_id`，绝不 `new_id()`**。引用跳转的 chunk anchor 回填、代码附件的 `(row_id, column_id)` 主键、KG 投影的 `_cell_ko_id(table_id, column_name, value_key)` 全都依赖 id 稳定；换新 id 等于回退完引用全断。

（`_cell_ko_id` 用列名而非列 id，故列改名会让 KO id 变——这是既有设计，回退列名同样会让它变回去，一致。）

### 6.3 历史只增不减

`T` 之后的流水**不删**。回退本身是新的一条，所以"回退的回退"天然成立，误操作救得回来。

### 6.4 单格恢复

**它不是回退**。就是一次普通的格子保存，内容取自历史，走既有 `update_knowhow_cell`（含既有的 `require_assets` 校验、合并格扇写判定），自动产生一条 `origin='revert'` 的 `cell_update` 流水。零特殊逻辑，也不受 §6.1 的指纹守卫约束——它本来就不依赖重放。

### 6.5 两版对比（`seq A` → `seq B`）

纯计算，不碰数据库状态：聚合区间 `(A, B]` 的流水——

- 每格取"区间内第一条的 `before`"和"最后一条的 `after`"作为净变化；两者相等则不显示
- 行/列增删取净集合（区间内新增又删除的相互抵消，不显示）
- 表元/anchor 取首 `before` 与末 `after`

## 7. 边界与已知代价

### 7.1 孤儿图片清扫器（行为改变）

`MaintenanceStore.sweep_orphan_assets` 现在只扫**活着的** `knowhow_cells.content_md` 里的 `asset://<id>` 子串。历史里的旧内容同样引用图片——不处理的话，图片一从格子里删掉就被回收，回退回去全是裂图，回退功能半废。

**修法**：把存活引用集扩展到 `knowhow_changes.payload_json`（在现有 per-asset LIKE 扫描里加一个 `OR EXISTS (SELECT 1 FROM knowhow_changes ch JOIN knowhow_tables t ON t.id = ch.table_id WHERE t.notebook_id = ? AND ch.payload_json LIKE ?)`）。

**代价（用户已知悉并接受）**：图片一旦进过任何格子就基本不再被自动回收，磁盘只增不减。释放路径 = "清理历史"之后再跑清扫。因此：
- 「清理历史」的确认文案要写明「同时会释放这些历史所引用的图片」
- 「清理历史」执行完后**主动触发一次该 notebook 的资产清扫**（绕过节流）

### 7.2 存量表没有创世流水（上线断层）

迁移时**不补造历史**——造不出真实的 `before`。存量表的第一条流水是上线后的第一次编辑。

- 历史抽屉在这类表上显示：「该表创建于版本管理上线前，更早的变更未记录」
- 回退最早只能到上线后的第一条
- 指纹守卫在这类表上依然成立（第一条流水的 fingerprint 就是那次编辑后算的），无需特殊处理

### 7.3 表复制/移动不带历史

`copy_table` / `move_table` 会 `_remap` 全部 id，带历史需要把 payload 里每个 `row_id` / `column_id` 也重映射，实现量和出错面都不小。决策：**都不带**，目标表写一条 `table_create` 流水，`note` 记「由 <源表标题> 复制/移动而来」，`fingerprint` = 目标表当前指纹。移动表会丢历史，这是已接受的代价。

notebook 整本深拷贝（`notebook_sharing.copy_notebook`）同理不带历史；其 knowhow 表在副本中各写一条 `table_create`。

### 7.4 代码附件新鲜度自动回正

`knowhow_cell_code.cell_content_hash` 与格子当前净文本 hash 的比对推导出 fresh/stale。回退格子内容后 hash 变回旧值 → 代码自动从 stale 变回 fresh。这是**正确行为**（内容回到当时，代码就重新对上了）。不特殊处理，但要写测试把它钉死。

### 7.5 并发

- 流水写在变更本体的同一写事务内 → SQLite 写锁天然串行化，`seq` 不会重复
- 回退整体在一个写事务内，与其它写互斥
- 前置指纹校验必须在该事务内（§6.1 第 3 步）
- 用户在抽屉里看到 seq=50、点回退时已是 53 → `expected_head_seq` 409 拦截

### 7.6 离线 CLI 写路径

`scripts/backfill_knowhow_md.py` 走 `update_knowhow_cells_bulk_guarded`，在挂钩范围内，回填会产生 `origin='backfill'` 的流水——这正是"看到 LLM 改了什么"的价值所在。CLI 需要传 `actor`（按 notebook 所有者解析，与既有 `set_request_user` 一致）。

注意既有坑：任何离线 CLI 启动会跑 `_recover_interrupted_jobs`，把 `pending` 行刷成 `failed`。本设计的回退路径提交后**主动触发全量重投影**，不依赖行状态，故不受此坑影响。

### 7.7 清理历史只能删前缀，绝不能挖洞

反向重放要求流水链**从 head 起连续**。若"清理 N 天前"删掉了链条中间的一段，重放走到缺口就断了，而前置指纹守卫看的是 head，**发现不了这个洞**——会一路重放到错误状态再被后置守卫拦下（能拦住，但报错时机很晚且原因难懂）。

因此 prune 的语义严格定义为**删最老的连续前缀**：

- 删除所有 `created_at < now - N 天` 的流水，且它们必然是 `seq` 最小的一段（`created_at` 与 `seq` 同序，因为都在写事务内单调产生）
- 实现上按 `seq` 而非 `created_at` 执行删除：先算出 `cutoff_seq = MAX(seq) WHERE created_at < 截止时间`，再 `DELETE WHERE seq <= cutoff_seq`。这样即便时钟回拨导致 `created_at` 局部乱序，删的也一定是前缀
- 若 `cutoff_seq` 等于当前 head，保留 head 那一条（否则前置指纹守卫失去参照，整表回退功能直接不可用）
- 删除后，最早可回退点 = 剩余最小 `seq`；指向已删 `seq` 的里程碑保留但灰显为"已失效"（§4.2）

### 7.8 Agent API 不暴露历史

`/api/agent/knowhow/*` 与 MCP 工具**不新增**历史相关接口（YAGNI，无此需求）。将来若需要，`knowledge:read` scope 下只读时间线是自然扩展点。

## 8. API 与前端

### 8.1 端点（全部在 `app/api/knowhow_routes.py`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/notebooks/{nb}/knowhow/{table}/history` | 读 | 时间线分页；`?limit`/`?before_seq`/`?milestones_only` |
| GET | `/notebooks/{nb}/knowhow/{table}/history/{seq}` | 读 | 单条变更详情（含 payload 渲染所需的前后文） |
| GET | `/notebooks/{nb}/knowhow/{table}/history/diff?from={a}&to={b}` | 读 | 两版净变化（纯计算） |
| GET | `/notebooks/{nb}/knowhow/{table}/rows/{row}/cells/{col}/history` | 读 | 单格历史时间线 |
| POST | `/notebooks/{nb}/knowhow/{table}/revert` | 写 | body: `{"target_seq": int, "expected_head_seq": int}` |
| POST | `/notebooks/{nb}/knowhow/{table}/milestones` | 写 | body: `{"seq": int, "name": str, "note": str}` |
| DELETE | `/notebooks/{nb}/knowhow/{table}/milestones/{id}` | 写 | |
| POST | `/notebooks/{nb}/knowhow/{table}/history/prune` | 写 | body: `{"before_days": int}`；删前缀（§7.7），执行后触发资产清扫 |

单格恢复**不需要新端点**——前端拿到历史值后走既有的格子保存端点（§6.4）。但既有格子保存端点需要**新增一个可选 body 字段 `origin`**（缺省 `"user"`，允许值即 §4.1 的 `origin` 枚举），否则时间线上分不清"手动改的"与"从历史恢复的"。这是一处跨栈 wire 变更，落在 §8.3 的契约测试范围内。同理，LLM 优化 / 规整两条既有回填路径也要开始传各自的 `origin`。

错误全部走 `user_error()` 打 header 标记的中文用户文案，不暴露 `str(exc)`：

| 场景 | HTTP | error_code | 文案 |
|---|---|---|---|
| 前置指纹不一致 | 400 | `knowhow_history_inconsistent` | 表的当前内容与变更历史对不上，回退已中止 |
| head 已变 | 409 | `knowhow_history_stale` | 这张表刚被改过，请刷新后重试 |
| 后置校验失败 | 500 | `knowhow_revert_verify_failed` | 回退结果校验失败，已放弃本次回退 |
| 目标 seq 不存在/不属于本表 | 404 | — | 统一无 oracle |

### 8.2 前端（三个入口，全挂既有结构）

**① 表工具栏「历史」按钮 → 历史抽屉**（与既有行详情抽屉 / 矩阵抽屉同款形态，`knowhow-panel.tsx` 顶层状态机新增一个 modal 槽）

- 时间线一条一行：`07-22 14:30 · 张三 · 修改了 3 个格子` + origin 徽章（LLM 优化 / 规整 / 导入 / 回退）
- 点开展开红绿 diff（markdown 渲染，复用 `KnowhowMarkdown`）
- 每条右侧：「设为里程碑」（起名）、「回到这里」（二次确认，确认框列出将影响多少行/列）
- 顶部对比模式：选两条看区间净变化
- 里程碑以旗标显示，可筛选「只看里程碑」
- 存量表在时间线末尾显示上线断层提示（§7.2）

**② 格子浮窗第三态页签**：现有「预览态 ↔ 编辑态」加「历史」。列这一格历次值（尤其 LLM 优化/规整前后），每条可「恢复此版本」。

**③ 表管理面板「清理历史」**：清理 N 天前，二次确认，文案写明「清理后将无法回退到该时间点之前，同时会释放这些历史引用的图片」。

**权限**：`canEdit=false` 的只读成员看得到时间线和 diff，**看不到**回退 / 里程碑 / 清理按钮（后端同样守，前端只是不给入口）。

### 8.3 跨栈 wire 契约

按既有教训（纯前端单测只证明前端自洽），新增契约测试锁住：
- 回退请求体字段名 `target_seq` / `expected_head_seq`
- 里程碑请求体 `seq` / `name` / `note`
- 清理请求体 `before_days`
- 既有格子保存端点新增的可选 `origin` 字段（§8.1）——前端确实传了、后端确实读了、非法值 400
- 四个 error_code 字面量
- 时间线响应的 `seq` / `kind` / `origin` / `actor` / `created_at` 字段名

后端对未知/缺失字段**返回 400，不做宽容默认**——`_columns_with_anchor` 的宽容默认把本该 400 的 wire 错误降级成静默失败，导致整个 anchor 特性上线后从未生效（PR#281 → PR#286）。

## 9. 测试策略

### 9.1 核心：往返不变量测试

随机（固定种子）生成 N 次混合变更——改格 / 加删行 / 加删列 / 改名 / 改内容类型 / 切 anchor / 改表元 / 代码附件增删改 / 导入追加——每步记下 `_fingerprint_on` 的结果；然后从末态**逐条**回退到每一个历史点，断言：

1. 每一步回退后的指纹 `==` 当时记录的 `fingerprint`
2. 每一步回退后 `snapshot_table()` 与当时的快照**逐字段相等**（含 id、position）

delta 完备性只能这么证明——挑几个点做单点测试证不出来。

### 9.2 架构守卫 + 变异验证

- 24 个写块全覆盖，白名单外必须调 `record_change`
- 变异一：删掉一处 `record_change` → 守卫必须红
- 变异二：把一个写块**移动**到别的方法里 → 守卫必须红（源码断言须先 slice 到具体方法体，`[\s\S]*?` 会越过块的收尾大括号）
- 变异前先 `grep -c` 确认真的改到了目标（避免"替字面量而代码用常量""按行号插入而行号已漂"这类打空）

### 9.3 其余

- **资产联动**：格子里的图片被删出格子 → 清扫器因历史引用而不删；「清理历史」后再清扫 → 删掉
- **id 稳定性**：删行 → 回退 → `row_id` 逐字相同、代码附件跟着回来、引用跳转仍能命中
- **列删除可逆**：删列（CASCADE 掉整列格子）→ 回退 → 列定义与全部格子内容逐字恢复
- **代码附件新鲜度**：回退格子内容后 stale → fresh 的行为被钉死
- **并发串行化**：回退事务中途另一写入 → 断言串行化，指纹守卫不误报
- **存量表**：无创世流水的表首次编辑后，前置守卫成立、可回退到该点
- **prune 只删前缀**：构造时钟回拨导致 `created_at` 局部乱序的流水，断言删除后剩余 `seq` 连续、head 保留、从 head 到最早剩余点仍可完整回退；指向已删 `seq` 的里程碑仍在且标记失效
- **权限**：只读成员 GET 通、POST revert 403
- **前端纯函数单测**：diff 计算、区间聚合、时间线分组、origin 徽章映射
- **契约测试**：§8.3 的字段名与 error_code

## 10. 派生物连带清单（本仓库反复踩）

实现完成后必须逐项检查。

**⚠️ 2026-07-22 复核订正**：本仓库过去那套「行号钉死」的守卫（`test_repository_surface_manifest.py` / `test_repository_callers_static.py`，含 `EXPECTED_PATCH_DELTAS` / `LINE_NUMBER_INSENSITIVE_FILES`）**已在 commit `8866a67e`（#307）中删除**，替换为语义化的 `test_repository_surface_contract.py` + `test_repository_dependency_contract.py`。新守卫的 consumer site 只记 `{path, scope, kind, target}`，有一条测试专门断言**不含源码位置**（`test_surface_sites_use_semantic_identity_without_source_positions`）。所以「新增方法导致行号移位」这个连带**不再存在**——其它测试文件里残留的 `EXPECTED_PATCH_DELTAS` 注释是过时文本。

| 连带 | 触发条件 | 处理 |
|---|---|---|
| `facade_surface.json` + `ownership_manifest.py` | 新增 facade 成员或新增/移动 consumer 调用点 | `PYTHONPATH=backend python3 scripts/generate_repository_contract_fixtures.py --rebaseline-surface` |
| `caller_boundaries.json` | 在 `repositories/sqlite/` 之外新写裸 SQL，或新文件访问 `repo._runtime` | `--rebaseline-callers`，并在 `tests/architecture/repository_callers.py` 手写越界理由 |
| `api_contract.json` | **新增任何 HTTP 端点**（`test_openapi_contract_is_byte_semantically_frozen` 会立刻红） | 默认模式 `PYTHONPATH=backend python3 scripts/generate_repository_contract_fixtures.py`（`_assert_baseline_sources` 只守 `--rebaseline`，默认路径不受限） |
| `MIGRATION_MANIFEST`（在 `scripts/verify_repository_snapshot.py`，**手工维护**） | `SCHEMA_VERSION` bump | 加 `(23,24)` hop + 字典推导式把历史 `(X,23)` 重基到 `(X,24)`；SQL 文本须与 `_migration_24` 逐字节一致但**去掉 `IF NOT EXISTS`**（`sqlite_master.sql` 会剥掉它） |
| `test_repository_snapshot_verifier.py` | 同上 | 3 个既有回放测试（v13/v20/v21）的 rollback 各补 `DROP`；新增一个 `test_deployed_v23_...` |
| 硬编码 `== 23` 的断言 | 同上 | `test_legacy_db_compat.py`、`test_memory_kg_schema.py`、`test_multi_domain_bases.py`、`test_sqlite_migrator_component.py`、`test_source_asset_migration.py`、`test_repository_v9_fixture.py` |
| `schema_contract.txt` golden | 同上 | `cd backend && UPDATE_SCHEMA_GOLDEN=1 pytest tests/test_legacy_db_compat.py -k contract` |
| 文档版本号（有 documentation guard） | 同上 | `README.md:45-46`、`README_zh.md:45`、`AGENTS.md:159-160`、`architecture.md:47` 的「schema 版本 23」与「v10–v23」范围，句尾补一句 v24 做什么 |
| knowhow 章节散文 | 本特性 | `architecture.md` / `AGENTS.md` 补版本管理段落 |

facade 新成员必须是**纯一跳委托**（`return self._runtime.knowhow_store.xxx(...)`，AST 强校验），`RUNTIME_COMPONENT_OWNERS` 已登记 `knowhow_store`，无需碰 owner 映射。

本特性需**重启后端**（SCHEMA_VERSION bump）。无新 CLI，但 README/README_zh 因版本号仍需改。

## 11. 不做（YAGNI）

- 撤销历史中的某一条（git revert 中间提交语义）——冲突处理 UI 成本高，用户明确不要
- 主网格上标注"最近改过"的角标/底色
- 自动裁剪历史（按天/按条）
- Agent API 暴露历史
- 分支/合并语义
- 表复制/移动时重映射并携带历史
