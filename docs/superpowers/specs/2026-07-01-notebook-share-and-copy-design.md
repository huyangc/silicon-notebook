# Notebook 分享与拷贝 — 设计文档

**日期**: 2026-07-01
**状态**: Phase 1 待评审
**范围**: 用户可分享自己的 notebook,他人凭分享码把它拷贝到自己的空间(小库);大库改为只读共享(Phase 2)。

---

## 1. 背景与目标

当前 notebook 严格按 `notebooks.created_by = user.id` 做 owner 隔离,**无任何分享/复制/导出**能力。目标:让用户把自己的 notebook 分享给同实例的其他登录用户。

按库大小分两条路(阈值可配):

- **小库 → 拷贝**:接收者得到一份**完全独立、归自己所有**的副本。
- **大库 → 只读共享**:不复制数据,接收者只读访问同一个库(浏览 + 问答)。

理由:拷贝要深复制全部数据(GB 级向量),大库复制成本不可接受;只读共享零复制。

## 2. 两期规划

| 期 | 内容 | 是否碰隔离层 |
|---|---|---|
| **Phase 1(本文档)** | 分享码 + **小库拷贝**(size 门 + 深拷贝 + id 重映射) | **否**(拷贝=新建独立自有库) |
| **Phase 2(后续独立 spec)** | **大库只读共享**(成员表 + 拆 `require_notebook_access` 为读/写两守卫 + `list_notebooks` 合并 + 撤销踢人) | 是(横切、安全敏感) |

**size 门连接两期**:预览接口按大小返回 `mode`。Phase 1 上线后,大库预览返回 `too_large`,拷贝接口拒绝;Phase 2 再补 `readonly` 加入路径。本文档只交付 Phase 1。

---

## 3. Phase 1 数据模型变更

`notebooks` 表加两列(`ALTER TABLE ... ADD COLUMN`,幂等迁移,沿用现有迁移风格):

```sql
is_shared    INTEGER NOT NULL DEFAULT 0
share_token  TEXT             DEFAULT NULL   -- 唯一;NULL=未分享
```

- 加唯一索引 `CREATE UNIQUE INDEX IF NOT EXISTS idx_notebooks_share_token ON notebooks(share_token) WHERE share_token IS NOT NULL`。
- `share_token` 由 `secrets.token_urlsafe(16)` 生成(约 22 字符,不可枚举),前缀 `shr-` 便于识别。

Phase 1 **不新增**成员表(那是 Phase 2)。

---

## 4. API(4 个端点)

所有端点都要求登录(`Depends(get_current_user)`)。

### 4.1 `POST /notebooks/{id}/share` — 开启分享(仅 owner)
- 守卫:现有 `Depends(require_notebook_access)`(Phase 1 语义仍是 owner-only,天然够用)。
- 行为:若未分享,生成 `share_token` 并置 `is_shared=1`;已分享则原样返回(幂等)。
- 响应:`{ "share_token": "shr-...", "copyable": bool, "size": {"bytes": N, "chunks": N, "nodes": N} }`
  - `copyable` = 当前大小是否在拷贝阈值内,供 owner UI 提示「可被拷贝」/「太大(Phase 2 才支持共享)」。

### 4.2 `DELETE /notebooks/{id}/share` — 取消分享(仅 owner)
- 置 `is_shared=0`、`share_token=NULL`。旧码立即失效(预览/拷贝都会 404)。

### 4.3 `GET /shared/{token}` — 预览(任意登录用户)
- 按 `share_token=? AND is_shared=1` 查;查不到 → **404**(未分享/已撤销/错码统一 404,不泄露存在性)。
- 响应(**只给元信息,不给正文**):
  ```json
  {
    "name": "...", "owner_display": "a00123456",
    "source_count": N, "node_count": N, "edge_count": N,
    "source_titles": ["...", "... (最多前 50)"],
    "mode": "copy" | "too_large",
    "size": {"bytes": N, "chunks": N, "nodes": N}
  }
  ```
- `mode`:小库=`copy`;大库=`too_large`(Phase 2 会变 `readonly`)。不返回 chunk/向量/KG 正文。

### 4.4 `POST /shared/{token}/copy` — 拷贝到当前用户空间(任意登录用户)
- 校验:`is_shared=1`(否则 404);大小 ≤ 阈值(否则 **409** `{"error":"too_large", ...}`)。
- **同步**执行深拷贝(小库,毫秒~秒级),返回新 `NotebookSummary`(归当前用户所有)。
- 幂等性:不做去重——同一用户可多次拷贝(各得独立副本)。允许 owner 拷贝自己的库。

---

## 5. 拷贝机制(核心)

notebook 下各表主键是**全局唯一 id**,且有大量**列级**与 **JSON 内嵌**的交叉引用。拷贝 = 为每个实体生成新 id、建映射、逐表插入时重写所有引用。

### 5.1 拷贝的表 + 列级 id 引用

| 表 | 新 id | 需重写的引用列 |
|---|---|---|
| `sources` | `id` | `notebook_id`;`file_path`(见 5.4) |
| `source_elements` | `id` | `source_id` |
| `chunks` | `id` | `notebook_id`, `source_id`;**`element_ids`(JSON 数组,见 5.2)** |
| `chunk_embeddings` | — | `chunk_id`, `notebook_id` |
| `element_embeddings` | — | `element_id`, `source_id`, `notebook_id` |
| `knowledge_objects` | `id` | `notebook_id`, `source_id`, `source_candidate_id`;**`evidence`/`payload`(JSON,见 5.2)** |
| `knowledge_relations` | `id` | `notebook_id`, `source_id`, `source_object_id`, `target_object_id`;**`evidence`(JSON)** |
| `knowledge_embeddings` | — | `object_id`, `notebook_id` |
| `relation_embeddings` | — | `relation_id`, `notebook_id` |
| `concept_clusters` | `id` | `notebook_id`, `canonical_id`, `member_object_id` |
| `object_schemas` | — | 仅拷 `WHERE notebook_id=<原>` 的自定义 schema,`notebook_id`→新(builtin 全局的 `notebook_id=''` 不拷) |

**新 id 生成规则**:保留原 id 第一个 `-` 前的前缀,`-` 后替换为新 `uuid4().hex[:10]`(如 `src-abc…`→`src-新`)。无需硬编码每种前缀,稳健。

### 5.2 JSON 内嵌 id 引用(易漏,必须重写)

这些字段里嵌了功能性 id(渲染引用、relink、按 element 取文本都依赖),不重写→副本悬空引用、引用块渲染错:

- `chunks.element_ids`:`["el-..","el-.."]` → 逐个按 element 映射重写。
- `knowledge_objects.evidence` / `knowledge_relations.evidence`:list of `{element_id, source_id?, quoted_span, ...}` → 重写其中的 `element_id`、`source_id`(存在则映射;`quoted_span` 等文本原样)。
- `knowledge_objects.payload`:某些 object_type(如 procedure-steps)在 payload 里带 `element_id` → 同样重写。
  - 实现取「宽松重写」:递归遍历 payload/evidence 的 JSON,遇到值命中 element/source/object 映射表的键(`element_id`/`source_id`/`object_id`)就替换。附录 A 列出已知携带 id 的字段;实现按映射表做值替换,未命中的值原样。

### 5.3 算法(依赖序)

在**单个写事务**内:

1. 建目标 `notebooks` 行:新 `id`、`name = 原名 + " (副本)"`、`created_by = 当前用户`、`tier='personal'`、`is_shared=0`、`share_token=NULL`、其余元数据照抄。
2. 依次建立映射(dict 旧id→新id):`sources` → `source_elements` → `chunks` → `knowledge_objects` → `knowledge_relations`。
3. 按 5.1 顺序逐表 `SELECT * WHERE notebook_id=<原>`(或经 source_id/object_id 关联),用映射重写列 + 5.2 的 JSON,`INSERT` 到新 id/新 notebook。
4. 磁盘文件拷贝(5.4)。
5. **完整性自检**(5.5)。
6. 提交事务;失败则整体回滚(不留半个库)+ 清理已拷贝的磁盘文件。

### 5.4 磁盘文件

源文件在 `storage_dir/notebooks/{notebook_id}/`(见 `sqlite_repository.py:1688`)。拷贝时把原目录整树复制到 `storage_dir/notebooks/{新id}/`,每个 source 的 `file_path` 改写指向新目录下对应文件(用 source 映射 + basename)。用 `shutil.copy2`。

### 5.5 完整性自检(拷贝后、提交前)

断言副本内**无悬空引用**:
- 每个 `chunks.source_id`、`knowledge_relations.source_object_id/target_object_id`、`concept_clusters.member_object_id/canonical_id`、各 `*_embeddings` 的外键都能在副本对应表内找到。
- 副本各表**行数 == 原库对应表行数**。
- 任一断言失败 → 抛错 → 回滚(拷贝要么全对要么不留痕)。

### 5.6 不拷贝的表(明确排除)

`conversations`、`answers`、`feedback`(接收者的问答从零开始,不带原主的历史)、`extraction_runs`(运行日志)、`concept_merge_candidates`(评审 scratch,重建时再生)、`scale_index`/`kg_index` 磁盘产物(base 库派生索引;副本 `personal` 不需要,如需可后续「刷新图谱」重建)、原库的 `is_shared`/`share_token`(副本默认不分享)。

---

## 6. size 阈值(可配)

新增 Settings(pydantic-settings v2,用 `validation_alias`,见 [[pydantic-env-alias-gotcha]]):

- `notebook_copy_max_bytes`(`NOTEBOOK_COPY_MAX_BYTES`,默认 `50 * 1024 * 1024` = 50MB):`SUM(sources.file_size)` 上限。
- `notebook_copy_max_rows`(`NOTEBOOK_COPY_MAX_ROWS`,默认 `5000`):`chunk 行数 + knowledge_objects 行数` 上限(向量是拷贝重头,行数是其代理)。

`copyable = total_bytes ≤ max_bytes AND (chunk_count + node_count) ≤ max_rows`。两项都便宜查(COUNT/SUM)。

## 7. 前端触点(最小)

- notebook 内新增「分享」入口:调 `POST .../share` 得码,展示可复制的分享码/链接 + `copyable` 提示;「取消分享」调 `DELETE`。
- 打开分享码的落地页/弹窗:调 `GET /shared/{token}` 显示预览元信息;`mode==copy` 显示「拷贝到我的空间」按钮(调 `POST .../copy` 后跳转到新库);`mode==too_large` 显示「此库过大,暂不支持(共享访问即将支持)」。
- 遵循 [[ui-polish-bar]]:对齐、精致,改完给视觉验证。

## 8. 安全 / 边界

- 4 端点全要求登录;`share`/`unshare` 仅 owner(复用现有守卫)。
- `share_token` 不可枚举;预览/拷贝错码或已撤销统一 **404**,不泄露存在性。
- 预览只暴露元信息(名称、owner 用户名、计数、来源标题)——都是 owner 主动分享的库的信息,可接受;**不**暴露 chunk 正文/向量/KG 明细。
- 拷贝每次校验 `is_shared`(撤销后旧码立即失效)。
- 副本与原库**完全独立**:各自 owner,原库后续增删改不影响副本,反之亦然。
- 接收者拿不到原库的 conversations/answers,也无法通过 Phase 1 任何端点改动原库。
- 拷贝走当前用户身份(`created_by=当前用户`);模型配置是 per-user 的,副本自然用接收者自己的模型,无需迁移。

## 9. 测试计划

- **分享**:`share` 生成 token 且幂等;`unshare` 置空 token;仅 owner 能 share/unshare(非 owner 403/404)。
- **预览**:返回元信息;错码/未分享/已撤销 → 404;未登录 → 401;`mode` 随大小正确(小=copy,大=too_large)。
- **拷贝(核心)**:
  - 副本归拷贝者、出现在其 `list_notebooks`,**不影响**原库(原库计数/owner 不变)。
  - **id 重映射正确性**:副本内 relations 指向副本内 objects;`chunk_embeddings.chunk_id`、`knowledge_embeddings.object_id`、`concept_clusters.member_object_id` 等全部指向副本实体;**无悬空引用**;JSON 内 `element_ids`/`evidence.element_id` 已重写到副本 element。
  - **行数一致**:副本各拷贝表行数 == 原库。
  - **conversations 不被带走**;磁盘文件已复制且 `file_path` 指向新目录。
  - 大库(超阈值)→ 409 拒绝。
  - 事务回滚:构造中途失败 → 副本 notebook 与磁盘文件都不残留。
- **端到端**:A 用户分享 → B 用户预览 → B 拷贝 → B 能在副本里问答且引用块正常(证明 element 重映射生效)。

## 10. 非目标(留给 Phase 2)

- 大库只读共享 / 成员表 / `require_notebook_access` 拆分 / `list_notebooks` 合并 / 撤销踢人。
- 公开画廊、指定用户分享、协作读写编辑。
- 异步拷贝(Phase 1 拷贝只对小库,同步即可)。

---

## 附录 A:已知携带 id 的 JSON 字段(重映射清单)

- `chunks.element_ids`:element id 数组。
- `knowledge_objects.evidence[*].element_id` / `.source_id`。
- `knowledge_objects.payload`:procedure-steps 等类型的 `steps[*].element_id`(按 object_type 而定;实现用宽松值替换覆盖)。
- `knowledge_relations.evidence[*].element_id` / `.source_id`。

实现策略:构造 `{element,source,object}` 三张映射表后,对上述 JSON 做递归重写——按键名路由到对应映射(`element_id`/`element_ids`→element 表,`source_id`→source 表,`object_id`→object 表),**标量值直接替换、数组值逐元素替换**;映射表里没有的值保持原样(如 `source_candidate_id` 命中 source 映射才换,否则原样)。完整性自检(5.5)兜底,确保无悬空。
