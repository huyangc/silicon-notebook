# Notebook 分享 Phase 2 — 大库只读共享 设计文档

**日期**: 2026-07-01
**状态**: 待评审
**前置**: Phase 1(分享码 + 小库拷贝)PR#127。本期加**大库只读共享**——不复制数据,被分享者只读访问同一个库。
**关联设计**: `docs/superpowers/specs/2026-07-01-notebook-share-and-copy-design.md`(§2 两期规划)。

---

## 1. 背景与目标

Phase 1 按库大小分流:**小库→拷贝**(独立副本)。大库拷贝成本不可接受(GB 级向量),Phase 1 暂对大库返回 `too_large`。本期兑现大库路径:**只读共享**——被分享者凭同一个分享链接**加入**为只读成员,能打开/浏览/问答该库,但**不能改库**(不能加源/删/改 KG/rebuild/分享)。

这是个 **ACL 子系统**,横切现有「单 owner(`created_by`)隔离」层,安全敏感。核心是把「访问」拆成**读(owner ∪ 成员)**与**写(仅 owner)**两级,并用「默认最严」原则兜底防漏。

## 2. 成员模型

新表(幂等迁移,沿用现有 `ALTER/CREATE IF NOT EXISTS` 风格):
```sql
CREATE TABLE IF NOT EXISTS notebook_members (
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES users(id),
  role        TEXT NOT NULL DEFAULT 'reader',   -- 目前仅 'reader'
  added_at    TEXT NOT NULL,
  PRIMARY KEY (notebook_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_members_user ON notebook_members(user_id);
```

**最小成员管理**(用户已定):加入(凭链接)+ owner 取消分享一键踢全员 + 成员自己退出。owner 的「已分享总览」**只读展示**成员用户名(见 §5.5),但**不做逐个移除 / 按用户名邀请**(移除靠 unshare 一键踢全员;YAGNI)。

## 3. 访问控制拆分(安全命门)

现状:`/notebooks/{id}/...` 全部用**同一个** `Depends(require_notebook_access)`(owner-only,`deps.py:52`)守读+写。加成员必须拆。

### 3.1 两个守卫 + 默认最严原则
- 保留现 owner-only 逻辑为 **`require_notebook_write`**(仅 owner)。
- 新增 **`require_notebook_read`** = owner **∪** `notebook_members` 里的成员。
- **默认最严兜底(关键安全设计)**:**只把明确是「读」的路由改挂 `read`;其余一律保持 `write`(owner-only)**。漏改任何一个 → 它默认仍是 owner-only → 成员顶多读不到,**绝不会误得写权**。失败方向永远偏安全。
- 仓库层(**避免误放宽写权**):**`user_can_access_notebook` 保持 owner-only 不动**——`require_notebook_write` 继续用它(写判定不变);**新增** `user_can_read_notebook(nb, user)` = **owner ∪ 成员**,只供 `require_notebook_read`。**绝不「扩」共享的老函数**(它同时被写守卫依赖,一扩就把成员放进写权)——加读权只加新函数。

### 3.2 路由分类(逐条,实现照此改挂)
**改挂 `require_notebook_read`(owner ∪ 成员):**
`GET /notebooks/{id}`、`/analytics`、`/sources`、`/knowledge`、`/graph`、`/search`、`/duplicates`、`/unified-kg`、`/unified-kg/status`、`/unified-kg/pending-merges`、`/concepts/{cid}/detail`、`/objects/{oid}/context`、`/objects/{oid}/neighbors`、`/kg/conflicts/pending`、`/edge-review-queue`(读队列);`POST /ask`、`POST /ask/stream`(问答=读,产的对话/答案归调用者);`GET /notebooks/{id}/conversations`。

**保持 `require_notebook_write`(仅 owner):**
`PATCH /notebooks/{id}`、`DELETE /notebooks/{id}`、`POST /sources/import`、`/sources/url`、`/sources`、`PATCH /knowledge/{kid}`、`POST /knowledge/{kid}/merge`、`POST /relations/{rid}/review`、`POST /tier`、`POST /share`、`DELETE /share`、`POST /kg/build`、`/kg/rebuild`、`/kg/relink`、`/unified-kg/rebuild`、`/unified-kg/merges/{cid}/confirm`、`/reject`、`/unified-kg/merges/review`、`/kg/conflicts/resolve`、`/kg/conflicts/{cid}/confirm`、`/reject`。

### 3.3 子资源守卫(非 `/notebooks/{id}` 前缀,单独 owner 检查,须一并改)
- **`GET /sources/{id}`、`GET /sources/{id}/elements`(读)**:改为「调用者是父 notebook 的 owner ∪ 成员」放行。新增 `user_can_read_source(source_id, user)`(经 `sources.notebook_id` 判读权)。
- **`POST /sources/{id}/parse`、`DELETE /sources/{id}`(写)**:保持仅 owner(现 `source_owner(sid)==user.id`,等价于父 nb `user_can_access_notebook`(owner-only),不变)。
- **`GET/PATCH/DELETE /conversations/{id}`**:`conversation_owner` 现返回 **notebook owner**——**改为按对话创建者 `conversations.created_by` 判**:成员管自己的对话,**谁都不能碰别人的(连 owner 也不行,隐私优先)**。
- **`DELETE /notebooks/{id}/conversations`(批量删,read 守卫)**:仓库层按 `created_by=调用者` 兜(每人只删自己的旧会话)。
- **`POST /answers/{id}/feedback`**:`answers` 表无 `created_by`(仅 conversations 有),故按**父 notebook read 权限**放行(成员可对自己看到的答案反馈)。

## 4. `list_notebooks` 合并 + summary 标记

`list_notebooks` 从「`WHERE created_by=我`」→「**自有 ∪ 我加入的(`notebook_members.user_id=我`)**」。`NotebookSummary` 加:
- `access: "owner" | "reader"`(默认 `"owner"`,自有;加入的为 `"reader"`)。
- `shared_from: str`(reader 时=原 owner 用户名;owner 时空)。

前端据 `access` 显示只读徽章 + 门控写操作。

## 5. 加入 / 退出 / 撤销(复用 Phase 1 分享链接)

- **预览 mode**:`shared_preview` / `notebook_copy_stats` 对大库返回 `mode="readonly"`(Phase 1 的 `too_large` 改名)。小库仍 `copy`。
- **`POST /shared/{token}/join`**(任意登录):校验 `is_shared=1` + 大库(readonly);已是成员/owner 则幂等返回;否则插 `notebook_members(nb, caller, 'reader')`;返回该库 `NotebookSummary`(`access="reader"`)。小库调 join → 400/引导用 copy。
- **`DELETE /notebooks/{id}/membership`**(成员退出自己):删自己的成员行(read 守卫即可,只动自己)。
- **撤销**:`DELETE /notebooks/{id}/share`(owner)→ 除清 `is_shared/share_token` 外,**`DELETE FROM notebook_members WHERE notebook_id=?`**(踢全员)。
- 拷贝端点 `POST /shared/{token}/copy` 仍仅小库(大库 409)。

### 5.5 owner「已分享总览」界面(本期新增)

owner 在一个界面看到**自己共享出去的所有笔记本**及其信息。

- **`GET /notebooks/shared-by-me`**(登录;返回当前用户 owner 且 `is_shared=1` 的库):每项
  ```json
  { "id","name","share_token","mode":"copy"|"readonly","size":{...},
    "members":[{"username","added_at"}] }
  ```
  `members` 仅 `readonly`(大库)有值(已加入的只读成员);`copy`(小库)为空数组(拷贝是独立副本、无回链,**不追踪拷贝次数**)。
- 仓库 `shared_by_me(user_id)`:`SELECT ... FROM notebooks WHERE created_by=? AND is_shared=1`;每个按 size 门算 `mode`(§5 的 copy/readonly),readonly 的经 `list_members(nb)`(`notebook_members JOIN users` 取 `username`+`added_at`)取成员。
- 动作:每项可复制分享链接、**「取消分享」**(复用 `DELETE /notebooks/{id}/share` → 踢全员)。**只读展示成员用户名,不含逐个移除**(§2)。

## 6. 前端(同 PR,遵循 [[frontend-backend-co-design]] + [[ui-polish-bar]])

- **只读徽章**:notebook 列表项 + 头部,`access==="reader"` 显示「只读 · 来自 {shared_from}」。
- **门控写按钮**:reader 库里**隐藏/禁用**所有写入口——分享、分析弹窗里的治理动作(晋升/基准库/边审查)、添加来源、删除 notebook、刷新图谱/建 KG、KG 编辑、重命名标题。保留:浏览来源、问答(自己的对话)、看知识图谱、看 KG。
- **加入**:Phase 1 预览弹窗加 `mode==="readonly"` 分支 → 「加入(只读)」按钮 → `join` → `loadNotebookCollection` + 选中该库。
- **退出**:reader 库头部给「退出共享」入口 → `membership` DELETE → 从列表移除。
- **已分享总览(§5.5)**:一个入口(顶部/用户菜单「已分享」)打开 modal → `GET /notebooks/shared-by-me` → 列出每个已分享库:名称 + 模式徽章(可拷贝/只读共享)+ 可复制链接 + 规模;`readonly` 展示已加入成员数与用户名(只读);每项「取消分享」(调 unshare 后从总览移除)。复用 `utility-modal` + 分享 modal 的链接/复制样式。

## 7. 安全不变量

- 成员**只读**:§3.2 的 write 路由 + §3.3 的 source 写/conversation 他人数据,成员一律拒(owner-only 或 creator-only)。
- **默认最严**:未显式改挂 read 的 `/notebooks/{id}` 路由仍 owner-only。
- owner 隔离不被削弱:owner 仍只能进自有 + 现在也能进「别人分享给他并加入的」;非 owner 非成员一律 404(不泄露存在性)。
- 撤销分享即时:踢全员后,原成员对该库的 read 守卫立刻失败(404)。
- 对话隐私:conversation 读写严格按 `created_by`,跨用户(含 owner↔成员双向)不可见/不可改。
- 成员 ask 用**自己的** per-user 模型配置(ContextVar 已是 caller),产自己的对话/答案。

## 8. 测试(枚举式,安全导向)

- 成员表迁移 + CRUD(join 幂等、leave 只删自己、unshare 踢全员)。
- **守卫矩阵(防漏挂)**:参数化枚举 §3.2 的**每个 write 路由**断言成员得 403/404;**每个 read 路由**断言成员 200。非 owner 非成员对两类都 404。
- 子资源:成员能 `GET /sources/{id}`、不能 `DELETE`;成员能管自己的 conversation、**碰不了别人的(owner 也碰不了成员的)**。
- `list_notebooks`:reader 看到自有 + 加入的,加入项 `access="reader"`+`shared_from`。
- join/leave 端到端:大库 preview mode=`readonly` → join → 出现在列表 → leave → 消失;unshare → 成员库消失。
- 成员 ask 产生 `created_by=成员` 的对话。
- **已分享总览(§5.5)**:`shared_by_me` 只返回当前 owner 的 `is_shared` 库、`mode` 正确;readonly 库带成员用户名+`added_at`、copy 库 `members=[]`;别人看不到我的、我看不到别人的;`unshare` 后该库从总览消失。
- 回归:Phase 1 小库 copy 不受影响;全量绿。

## 9. 非目标

- 成员列表 / 逐个移除 / 按用户名邀请(最小管理,YAGNI)。
- 写协作(成员改库)——只读。
- 角色分级(只有 `reader`)。
- 通知/审计成员进出。

## 附录 A:改动面清单(实现参照)
- 后端:`sqlite_repository.py`(成员表迁移、**`user_can_access_notebook` 保持 owner-only 不动**、新增 `user_can_read_notebook`/`user_can_read_source`/`add_member`/`remove_member`/`kick_all_members`/`list_members`/`shared_by_me`、`list_notebooks` 合并、`conversation_owner` 改按 creator、`shared_preview` mode=readonly、join/leave)、`deps.py`(现 `require_notebook_access` 保留为 `require_notebook_write`;新增 `require_notebook_read`)、`routes.py`(§3.2 逐路由改挂 read + join/leave/membership + `GET /notebooks/shared-by-me`)、`schemas.py`(`NotebookSummary.access`/`shared_from`、`SharedPreview.mode` 含 `readonly`、`SharedByMeItem`)。
- 前端:`notebook-share.ts`(join/leave + 预览 readonly + `sharedByMe()`)、`page.tsx`(只读徽章 + 写按钮门控 + 加入/退出 + **「已分享总览」modal**)。
