# Spec:历史对话「按最后活动时间」批量清理

- 日期:2026-06-24
- 状态:设计已批(用户「同意」),待实现计划(writing-plans)
- 分支:`claude/cool-carson-d7cebc`(off origin/master `9a4c2ed`)
- 关联记忆:`ask-mode-ux-state`、`chunk-native-retrieval-state`

## 背景 / 动机

历史会话面板(`page.tsx` 的 `.chat-session-popover`,[page.tsx:2436](frontend/app/page.tsx:2436))目前只能**逐条删除**:每条卡片一个删除按钮 → 确认 → `DELETE /conversations/{id}`([page.tsx:2482](frontend/app/page.tsx:2482) → `requestDeleteSession` [:1696](frontend/app/page.tsx:1696) → `deleteSession` [:1683](frontend/app/page.tsx:1683))。会话一多,清理老对话要点很多次,缺一个**批量**入口。

用户诉求:按时间「分类」批量删,以 **3 天为基础**(例:删 3 天前的当前 notebook 全部对话)。

### 关键澄清(review 结论)

用户最初表述为「创建时间」,review 时修正为应按**最后一次对话的时间**——一个 5 天前创建、但今天又追问过的会话,不该被「删 3 天前」清掉。

代码核实:

- 追加一轮对话时,`conversations.updated_at` **会被刷新**到最新 —— `_ensure_conversation` 对已存在会话执行 `UPDATE conversations SET updated_at = now`([sqlite_repository.py:6421](backend/app/services/sqlite_repository.py:6421));所有 ask 路径都经此唯一入口。
- 列表/卡片**本就显示 `updated_at`**([list_conversations:6511](backend/app/services/sqlite_repository.py:6511) 排序+返回、[page.tsx:2476](frontend/app/page.tsx:2476) 展示),且每轮问答结束后前端无条件 `loadSessions`([page.tsx:1638](frontend/app/page.tsx:1638))即时重载。

故卡片时间已经是「最后活动时间」,语义正确、无需额外修。**批量清理以 `updated_at` 为基准**(非 `created_at`):二者仅在「老会话被重新追问」时分叉,按 `updated_at` 才不会误删近期还在用的会话。

## 目标

- 历史面板新增**一键批量清理**入口,按「最后活动早于 N 天」批量删当前 notebook 的会话(级联删对应历史问答)。
- 预设 **3 / 7 / 30 天**三档,每档旁显示「将删条数」,0 条禁用;删前确认。
- 删除范围 = 用户当前在面板里看到的同一集合(当前 notebook + `user-local`),不跨 notebook、不误删他人。

## 非目标

- 不做时间分组列表 UI、不做多选勾选、不做自定义天数输入(已在 brainstorming 中排除,选定「预设阈值按钮」)。
- 不动单条删除现有行为([page.tsx:1683](frontend/app/page.tsx:1683)、[routes.py:548](backend/app/api/routes.py:548))。
- 不给 `ConversationSummary` 增 `created_at`(基准改为 `updated_at` 后已无必要)。
- 不改卡片时间展示逻辑(现有 `updated_at` 即正确)。

## 取数策略(核心决策)

**前端用列表里已有的 `updated_at` 本地算「将删条数」,后端按 `older_than_days` 服务端算截断时间执行删除。**

- 本应用为**单用户本地**(`current_user()` 恒返回 `user-local`,[sqlite_repository.py:829](backend/app/services/sqlite_repository.py:829);sqlite + 127.0.0.1),前后端同一台机器、同一时钟 → 前端预览条数 = 后端实删条数,**无需额外计数端点**。
- `_now()` 返回**本地时间、无时区** isoformat(如 `2026-06-24T16:21:00`,[sqlite_repository.py:7156](backend/app/services/sqlite_repository.py:7156))。截断时间**必须后端**用同样的 `datetime.now() - timedelta(days=N)` 生成后按字符串比较;**禁止**前端传 UTC 的 `toISOString()`(带 `Z`/时区会错位误删)。
- 备选(不采用):新增 dry-run 计数端点 —— 对本地应用是过度设计。

## 设计

### A. 后端

#### A1. 新增仓储方法 `bulk_delete_conversations`

`SqliteRepository`(`backend/app/services/sqlite_repository.py`,紧邻 `delete_conversation` [:6535](backend/app/services/sqlite_repository.py:6535)):

```python
def bulk_delete_conversations(self, notebook_id: str, older_than_days: int) -> int:
    """删除当前用户在该 notebook 下、最后活动(updated_at)早于 now-N天 的会话,
    级联删其 answers,返回删除会话数。notebook 不存在抛 KeyError。"""
    self.get_notebook(notebook_id)  # 不存在 -> KeyError -> 404
    cutoff = (datetime.now() - timedelta(days=older_than_days)).replace(microsecond=0).isoformat()
    with self._write() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM conversations "
            "WHERE notebook_id=? AND created_by=? AND updated_at < ?",
            (notebook_id, self.current_user().id, cutoff),
        ).fetchall()]
        for cid in ids:
            db.execute("DELETE FROM answers WHERE conversation_id=?", (cid,))
        db.executemany("DELETE FROM conversations WHERE id=?", [(c,) for c in ids])
    return len(ids)
```

- 级联删法复刻单条 `delete_conversation`([:6535](backend/app/services/sqlite_repository.py:6535));范围谓词复刻 `list_conversations` 的 `notebook_id=? AND created_by=?`([:6510](backend/app/services/sqlite_repository.py:6510)),仅多一个 `updated_at < cutoff`。
- `datetime` / `timedelta`:`datetime` 模块已导入(`_now` 在用);`timedelta` 按需补导入。

#### A2. 新增路由

`backend/app/api/routes.py`,紧邻单条删除([:548](backend/app/api/routes.py:548)):

```
DELETE /notebooks/{notebook_id}/conversations?older_than_days=N
```

```python
@router.delete("/notebooks/{notebook_id}/conversations")
def bulk_delete_conversations(notebook_id: str, older_than_days: int = Query(..., ge=1)):
    try:
        deleted = repository().bulk_delete_conversations(notebook_id, older_than_days)
        return {"deleted": deleted}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- 与单条删除 `DELETE /conversations/{id}` 路径不冲突。
- `older_than_days` 校验 `>= 1`(`Query(..., ge=1)`)。

#### A3. 不改动

`ConversationSummary` schema([schemas.py:230](backend/app/models/schemas.py:230))、`list_conversations`([:6498](backend/app/services/sqlite_repository.py:6498)):`updated_at` 已具备。

### B. 前端(`frontend/app/page.tsx`)

#### B1. 类型不改

`ConversationSummary`([:143](frontend/app/page.tsx:143))已含 `updated_at: string`。

#### B2. 「清理」入口 + 预设

历史面板头部(`.chat-session-popover` 内,列表 [:2447](frontend/app/page.tsx:2447) 之上)新增「**清理**」按钮,点击展开 3 个预设项 **3 / 7 / 30 天**:

```ts
const CLEANUP_PRESETS = [3, 7, 30];
const cutoffMs = (days: number) => Date.now() - days * 86400000;
const cleanupCount = (days: number) =>
  sessions.filter((s) => new Date(s.updated_at).getTime() < cutoffMs(days)).length;
```

- `new Date(s.updated_at)`:`updated_at` 为无时区本地 ISO → JS 按本地解析,与 `Date.now()`(本地纪元)同基准,无偏差。
- 每项展示「N 天前(条数)」;条数为 0 时禁用该项。

#### B3. 确认 + 执行 + 收尾

点击某预设 `days` → 复用 `setInfoModal`(同 `requestDeleteSession` [:1696](frontend/app/page.tsx:1696)):

> 标题「批量清理会话」,正文「将删除 N 条最近 X 天内无活动的会话,对应历史问答一并移除。」操作 取消 / 删除(danger)。

确认后:

```ts
const { deleted } = await api(`/notebooks/${currentNotebookId}/conversations?older_than_days=${days}`,
  { method: "DELETE" });
// 当前打开会话若在被删集合内(其 updated_at < cutoff),重置视图(复刻 deleteSession 的 id===conversationId 分支 [:1685])
await loadSessions(currentNotebookId);
setToast(`已删除 ${deleted} 条会话`);
```

- 「当前会话是否被删」在发请求**前**用 `updated_at < cutoffMs(days)` 判定并暂存,成功后据此重置(`setTurns([])` / `setConversationId(null)` 等,复刻 [:1685-1690](frontend/app/page.tsx:1685))。
- `api()` helper 见 [:470](frontend/app/page.tsx:470)。

### C. 语义与边界

- **「X 天前」= `updated_at` 早于 now − X×24h**,即「最近 X 天内无任何新对话」的会话。文案统一用「最近 X 天内无活动」避免歧义。
- 边界:恰好 X 天的保留(严格 `<`);X 天 + 1 秒删除。
- 范围隔离:仅当前 `notebook_id` + `created_by=user-local`,不波及其它 notebook。
- 级联:删会话同时删其 `answers`(历史问答),与单条删除一致。
- 空集:某档条数为 0 → 该预设项禁用,不会发请求。

## 数据流

```
用户点「清理 → 3 天前(N)」
  └─ 前端: cleanupCount(3) 已展示 N;判定当前会话是否在删集
  └─ setInfoModal 确认
       └─ DELETE /notebooks/{id}/conversations?older_than_days=3
            └─ repo.bulk_delete_conversations: cutoff=now-3d
                 ├─ SELECT 命中会话 ids (updated_at<cutoff, 本用户, 本 notebook)
                 ├─ DELETE answers WHERE conversation_id IN ids
                 └─ DELETE conversations WHERE id IN ids  → return len(ids)
       └─ {deleted}
  └─ (按需)重置当前会话视图 → loadSessions 重载 → toast「已删除 N 条会话」
```

## 测试

### 后端单测(pytest,紧邻既有 conversations 测试)

- 边界日:`updated_at` 恰好 = cutoff 的保留、早于 cutoff 的删除。
- **以 `updated_at` 而非 `created_at` 判定**:构造「`created_at` 老、`updated_at` 新」的会话,断言 `older_than_days` 大于其活动间隔时**不被删**(防回归到按创建时间)。
- 跨 notebook 隔离:他 notebook 同龄会话不受影响。
- 级联:被删会话的 `answers` 同删;未命中会话的 `answers` 保留。
- 返回值 = 实删会话数。
- 非法 `older_than_days`(0 / 负)→ 422(`Query(ge=1)`)。
- notebook 不存在 → 404。

### 前端验证(preview 手动 + 截图)

- 各预设条数计算正确、0 条禁用。
- 确认 → 删除 → 列表刷新 → toast。
- 删到当前打开会话时视图正确重置。

## 兼容性 / 回滚

- 纯新增(一个仓储方法 + 一个路由 + 面板内一个入口),不改既有删除/列表/展示路径。
- 回滚 = 撤销新增代码即可,无数据迁移、无 schema 变更。
