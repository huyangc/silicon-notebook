# 问答会话公开分享 · 设计稿

> 立项日期 2026-08-18。前置:深度报告公开分享(v43/v21)已上线并经 P1-T4 ⑦
> 补齐实时复核;群组知识共享 P0/P1/P2(#516/#517/#519)已合入。
>
> 本特性把「一份已完成报告可发布成免登录只读页」这条既有能力,平移到**问答会话**。
> 报告那条链踩过的坑都写在 `CLAUDE.md`「报告公开分享」红线里,本设计逐条继承,
> 不重新论证;**只登记会话比报告多出来的那些东西**。

---

## 一、目标与非目标

**目标**:把一条问答会话(多轮 Q&A)发布成免登录只读页,链接不可猜,可撤销。

**非目标(v1 明确不做,已登记非遗漏)**:

- 匿名读者的任何写入(评论、追问、点赞)
- 链接口令、有效期、访问次数上限
- 公开页的检索/跳转到原文(公开页本就打不开原文,这是报告页既有口径)
- 会话**导出**(md/pdf)——与分享是两件事,不搭车

---

## 二、已定口径(2026-08-18 拍板)

| 问题 | 决定 | 后果 |
| --- | --- | --- |
| 分享后又问了新问题 | **冻结 + 显式更新** | 链接稳定;新轮次不自动出现,需再点一次「更新到最新」。数据模型要**水位**。 |
| 推理轨迹 / 问题理解 | **不外发** | 与报告的「整个 `understanding` 不跨出去」逐字同一口径。投影里没有 `reasoning_trace` / `intent` / `retrieval_scope` / `retrieval_query`。 |
| 答案附图 | **v1 就带图** | 必须新开一条**匿名的、按 token 限定的**图片通道(见 §六)。这是本特性相对报告分享最大的新增暴露面。 |
| 引用了个人记忆(Memory) | **一并公开** | 安全性论证见 §五「为什么这条是自我发布」。分享弹窗仍要**明说**会包含记忆摘录。 |

「冻结」的语义是刻意的:悄悄延长等于替用户把新内容重新发布给所有已拿到链接的人,
那应该是一次有意识的动作。

---

## 三、数据模型

### 3.1 会话与答案的现状

```
conversations(id, notebook_id, title, created_by DEFAULT '', created_at, updated_at)
answers(id, notebook_id, question, payload TEXT /* 整个 AskResponse */, created_at,
        conversation_id /* v? ALTER TABLE 追加,有索引 */)
```

两条**已核实**的事实,直接影响设计:

1. **`answers` 没有 `created_by`**。答案的归属只能经 `conversations.created_by` 推导。
   所以行级判定的锚点是会话行,不是答案行。
2. **`conversations.created_by` 是 `DEFAULT ''`**,历史行可能为空串。空 `created_by`
   的会话**必须拒绝分享**(fail closed):没有创建者就无法做 §五那条实时复核,
   而一条永远复核不了的公开链接是本设计里唯一不可接受的形态。

### 3.2 新增列(SQLite v52 / PostgreSQL v30)

**token 挂在 `conversations` 行上,不另开侧表**——照 `_migration_43` 给报告 token 的
同一条理由逐字复用:它是一个与所属行同生命周期的可空值,会话被删就带走它的公开链接,
不必维护第二条级联。

```sql
ALTER TABLE conversations ADD COLUMN share_token TEXT DEFAULT NULL;
ALTER TABLE conversations ADD COLUMN shared_through_at TEXT DEFAULT NULL;
ALTER TABLE conversations ADD COLUMN shared_through_id TEXT DEFAULT NULL;
CREATE UNIQUE INDEX idx_conversations_share_token
  ON conversations(share_token) WHERE share_token IS NOT NULL;
```

**水位存的是时刻字面值,不是「最后一条答案的 id」**。理由:存 id 的话,那条答案一旦被删,
水位就失去意义(既没法解析成时间,也没法判断谁在它之前);存字面值则谓词恒有效,
与行的存活无关。

⚠ **排序键必须与既有 `get_conversation` 逐字一致,而它不是想当然的那个**。实测
(`sqlite/ask_state_store.py`)是**三键**:

```sql
ORDER BY julianday(created_at) ASC, created_at ASC, rowid ASC
```

`julianday()` 在最前面是因为要按**绝对时刻**比较——历史裸 naive 文本与带 offset 的新行
混在一张表里,纯字符串比较会在 UTC offset 变化处排错(与「Ask 会话即时入历史」红线
里 SQLite 侧那条同源)。所以水位谓词也必须落在 `julianday()` 上:

```sql
SELECT ... FROM answers WHERE conversation_id = ?
  AND julianday(created_at) <= julianday(?)
  ORDER BY julianday(created_at) ASC, created_at ASC, rowid ASC
```

⚠ 三键里第三键是 `rowid`(SQLite)/ PG 侧的对应物,**不是 `id`**。若并列时刻下两者顺序
可能不同,T1 要实测并两侧定死一种——公开页的轮次顺序与作者自己看到的分叉,是这条特性里
最难被测试发现、又最容易被读者当成「内容被改过」的一类 bug。

另:水位的**边界含义是闭区间**(`<=`),因为它取自分享那一刻已存在的最后一条答案。

### 3.3 shadow 正向复制

`conversations` 已在业务表集合内,本次是**加列 + 加一个部分唯一索引**:

- unique surface 计数 +1(当前 108 → 109,**已实证**)
- 停车策略走 **NULL**(可空列),与 `notebooks.share_token`、`reports.share_token`
  两处**已有的同形先例**一致——这是选「列 + 部分唯一索引」而不是新建侧表的第二个理由:
  停车方案是已知good的,不必重新论证。
- FK 闭包不变(没加外键)

---

## 四、API 面

四个新端点。前三个在主 router(带 router 级鉴权),后两个**必须**在
`public_router`。

| 端点 | 守卫 | 说明 |
| --- | --- | --- |
| `POST /notebooks/{nb}/conversations/{cid}/share` | `require_notebook_read` + 行级 `created_by` | 发放 token(幂等,已有则复用)**并把水位推到当前**。「分享」与「更新到最新」是同一个调用,由界面决定按钮文案。 |
| `GET /notebooks/{nb}/conversations/{cid}/share` | 同上 | 回读 token 与水位。**写级守卫**:token 就是凭证,把它交给只读成员等于让他无写权就能发放匿名访问。 |
| `DELETE /notebooks/{nb}/conversations/{cid}/share` | 同上 | 撤销。下一次公开请求与未知 token 同为 404。 |
| `GET /public/conversations/{token}` | 无 | 匿名页数据。 |
| `GET /public/conversations/{token}/assets/{alias}` | 无 | 匿名图片字节,见 §六。 |

**发放口径 = 会话创建者**,镜像报告的 `_own_report_or_404`:一次读同时回答两半
(行本身证明它属于这个 notebook,行上的 `created_by` 回答创建者是谁),
「存在但不是你的」与「不存在」同为 404。

⚠ **这条口径的后果要写进产品文档而不是埋在代码里**:群组只读成员可以把一条建立在
**库主语料**上的会话发布出去。它与报告分享**已经上线的形态完全一致**(P1-T3b 的裁决),
缓解手段也一样——那条链接的寿命恰好等于**创建者自己读权的寿命**(§五)。
是否要给库主一个「禁止本库会话被公开分享」的开关,登记为待议,v1 不做:
报告已经这样运行,单给会话加一道闸只会造成两套口径。

---

## 五、公开投影(白名单)

**是白名单不是脱敏**——这条从报告分享逐字继承。投影里只有:

```
PublicConversation:
  title            会话标题
  created_at       会话创建时刻
  shared_at        水位时刻(公开页要说明"内容截至何时")
  turns: [PublicTurn]

PublicTurn:
  question         提问原文
  asked_at         提问时刻(浏览器本地格式化)
  answer_md        答案正文
  answered_at      答案写入时刻
  evidence_level   有据/概述/推断(它是答案可信度的一部分,不是内部状态)
  references: [PublicReference]   # 与 PublicReportReference 同形
  images: [PublicImage]           # 见 §六
```

**一个 id 都不跨出去**:`source_id` / `element_id` / `object_id` / `notebook_id` /
`conversation_id` / `answer_id` / `asset_id` 全部不在投影里。公开页本就打不开原文,
给 id 只是让人拿去探测已认证接口。

**不在投影里的(对应「轨迹不外发」)**:`reasoning_trace`、`intent`、
`retrieval_scope`、`retrieval_query`、`top_relevance`、`mode`、`llm_mode`、
`retrieval_effort`、`index_required`。

### 为什么「Memory 一并公开」是自我发布而不是越权

Memory 投影**按创建者私有**(`memory_items.created_by`,与 Memory 检索同一条判据,
写在取数那条 SQL 里)。所以一个用户的答案**只可能**引用到他自己的 Memory——共享笔记本里
别人的私有 Memory 根本不进他这次请求的候选池。于是「公开自己的会话」= 公开自己的记忆摘录,
是自我发布。

但**分享弹窗仍必须明说**「公开页会包含引用到的个人记忆摘录」并给出条数:用户按下分享时
想的是「把这段问答发出去」,记忆是他自己写的、却未必逐条记得写了什么。

### 已定:清单卡(`result_sets`)**不进 v1**

集合枚举结果卡(覆盖率徽标 + 按来源分组的条目)v1 不外发。

**但绝不接受静默丢弃**:原答案带清单卡的轮次,公开页必须在该位置留一句可见说明
(「本次回答还包含一份清单,未包含在公开分享中」),而不是让读者以为答案本来就长这样。
这与 §六附图那条口径同源:少给内容可以,让读者不知道少了什么不行。

⚠ 判据要落在**投影侧**而不是渲染侧:投影里带一个 `omitted_result_sets: int`(只有计数,
没有任何内容),渲染按它出提示。把判据放在前端「payload 里有没有这个字段」会让一条
历史 payload 的格式差异静默关掉提示。

---

## 六、匿名图片通道(本特性最大的新增暴露面)

用户选了「v1 就带图」,所以这条要认真做。

### 6.1 别名而不是真 asset_id

投影里给的是**按 token 派生的不透明别名**,不是 `asset_id`:

```
alias = hmac_sha256(key=share_token, msg=asset_id) 的前 N 位十六进制
```

三个好处,每个都对应一条既有红线:

1. **保住「没有 id 跨出边界」**——公开页拿到的东西在已认证接口上什么也打不开。
2. **撤销即全灭**:token 一撤,所有别名当场失去意义,不需要第二条级联。
3. **不可跨链接关联**:同一张图在两条会话里分享出去得到不同别名,拿到两条链接的人
   无法据此断定它们引用了同一份资料。

反查:服务端用 token 解出快照,枚举快照内**被引用到的** asset_id 集合(有界——每答案
附图上限已在 `docs/product-and-api*.md` 登记),逐个算别名比对。集合小,一次图片请求
O(n) 可接受;绝不接受把别名反向映射持久化成一张新表。

### 6.2 服务端边界

- 只服务**快照内被引用到**的资产。不在集合内 → 404(不区分「不存在」与「不在本次分享里」)。
- 与页面端点**同一条实时复核**:创建者当前读权不成立 → 404。
- **`Cache-Control: no-store`**。理由与跨库代理资产逐字相同:撤销之后浏览器缓存会把
  「失效即 404」静默架空。这里比跨库场景更硬——那边最坏是挂载期内的缓存,这边是**匿名**
  缓存。
- 只回 `MINERU_RETURN_IMAGES` 允许的既有 mime 白名单,复用既有资产服务的路径解析,
  不新写一份 path 拼接。

---

## 七、从报告分享继承的五条硬边界

逐条列出,因为它们每一条都是踩出来的,而会话页会**重犯**同样的错:

1. **匿名端点必须挂独立 `public_router`**。主 API router 带 router 级
   `Depends(get_current_user)`(「零逐路由遗漏」),挂上去会 401 拦掉它要服务的访客。
2. **匿名端点不绑请求用户**,所以只能调**不依赖 current-user** 的仓储方法。
   ContextVar 未设时 `current_user` 回退 seeded admin——等于以管理员身份跑。
   `public_conversation_by_token(token)` 因此**只收 token**。
   ⚠ `auth_optional` 的测试夹具**看不见**这条(缺 token 会静默回退 seeded admin),
   所以**接线本身要有守卫**,不能只靠端点测试。
3. **实时复核创建者读权**,两个 id 显式传参,匿名 router 绝不碰 `current_user`。
   不通过与无效 token 同为 404(可区分的响应会把成员身份透露给匿名调用者);
   恢复授权即复活。**页面端点与图片端点都要做**。
4. **隔离的是产品层不是渲染管线**:公开页必须复用 `remarkCitations`(`[k]`/【k】同一
   语法口径链接化,编号取自 key 里的序号而非位置——公开投影会丢掉既无标题又无摘录的
   条目,按位置数会与正文对不上)、`.answer-table-wrap` / `.answer-code`(宽内容在自己的
   内容块内横向滚动),并**自己** `import "katex/dist/katex.min.css"`——它是又一个不经
   `app/page.tsx` 的界面,漏了不会报错,只会让每条公式渲染两遍。
   守卫沿用 `frontend/tests/guards/katex-stylesheet-guard.test.mjs`。
5. **只分享已完成的东西**。报告是 `status == 'done'`;会话的对应物是**水位之前至少有
   一条已写入的答案**,且**生成中的轮次不进快照**(水位按 `answers` 行取,在飞的 ask
   还没有答案行,天然被排除——这一条要有用例钉住,别依赖巧合)。

---

## 八、前端

- **入口**:会话历史每条的行内菜单 +(打开会话时)标题旁的「分享」。复用既有分享弹窗的
  视觉,但**不是**笔记本链接分享那个弹窗——两者的爆炸半径完全不同,合在一起会让用户
  以为撤销其一会撤销另一个。
- **弹窗内容**:链接 + 复制;「内容截至 <水位时刻>,之后新增 N 轮未包含」+「更新到最新」;
  「撤销分享」;以及**两条披露**——包含 M 张附图、包含 K 条个人记忆摘录。
- **忙碌态**:发放/更新/撤销都是点一下就发请求的动作,按仓库既有规矩给按该动作语义写的
  进行态文案,不能笼统「处理中」。
- **公开页** `/public/conversations/[token]`:多轮 Q&A 竖排;顶部说明这是只读快照及其
  截止时刻;引用卡与报告公开页同形(标题/原始文件/位置/摘录,无跳转)。
- **组件守卫**:照 `public-report-page.component.test.tsx` 建
  `public-conversation-page.component.test.tsx`。

---

## 九、任务切分(每任务双评审)

| 任务 | 内容 | 要点 |
| --- | --- | --- |
| **T1** | schema v52/PG30 全链路 | 三列 + 部分唯一索引;shadow manifest / 夹具 / snapshot verifier / merge_dbs 分类;surface 计数实证。**深拷贝无需处理**——已实测 `_COPY_VALIDATED_TABLES` 里根本没有 `conversations`(会话不随副本走),所以不存在「要不要带 token」的问题;但要在迁移注释里写下这条,免得后来者看到 `notebooks.share_token`/`reports.share_token` 的先例就以为这里也要显式清空 |
| **T2** | 后端发放/回读/撤销 + 水位推进 | 行级 `created_by` 门;空 `created_by` fail closed;水位查询与 `get_conversation` 排序对齐(§3.2 那格要实测定死) |
| **T3** | 匿名页端点 + 白名单投影 | §五、§七 五条;`public_conversation_by_token` 只收 token;接线守卫 |
| **T4** | 匿名图片通道 | §六;别名派生与有界反查;`no-store`;撤销即失效用例 |
| **T5** | 前端弹窗 + 公开页 | §八;katex/remarkCitations/横向滚动三条守卫 |
| **T6** | 文档 + 门禁 + PR + codex 闭环 | 四份文档同步;数值上限只登记在 `docs/product-and-api*.md`;G1/G2/G3 |

### T1 的文件清单(从 `fd38d259`(P2-T1)的实际改动面推导,22 个文件)

本次与 P2-T1 的差别:那次是**新建表**,这次是**给既有表加三列 + 一个部分唯一索引**。
所以 shadow `_TABLES` 不新增条目(`conversations` 已在集合内),但**列清单要改**。

1. `backend/app/repositories/sqlite/migrations.py` — `_migration_51` + `SCHEMA_VERSION=51`
   (⚠ **追加新迁移,绝不塞进已封版的旧迁移**——版本闸对已部署库短路,`IF NOT EXISTS`
   救不了没被执行到的语句)
2. `backend/app/repositories/postgres/migrations/0030_conversation_share.sql`
3. `backend/app/repositories/postgres/schema_manifest.py` — 50/28 → 51/29
4. `backend/app/migration/shadow/manifest.py` — `conversations` 的列清单 + 唯一面
5. 两侧 store(发放/回读/撤销/水位),照 `sharing_store.py` 报告 token 的先例
6. `backend/tests/fixtures/repository_v9/{manifest.json,expected_snapshot.json}` — 重生成
7. `backend/tests/test_repository_v9_fixture.py` — `user_version == 51`
8. `backend/tests/test_repository_snapshot_verifier.py` + `scripts/verify_repository_snapshot.py`
   — `(51, 52)` + `_rollback_v52` + 用例
9. `scripts/merge_dbs.py` — 分类(会话是 notebook-scoped;token 与水位随行走)
10. **⚠ 最易漏的一格**:`backend/tests/postgres/` 下**九个**文件里的
    `migrate() == 28` 版本断言(P2-T1 时是 **85 处**)+ packaged 阶段清单补
    `(29, 'conversation_share')`。P1-T1 就是漏了这一格让整条 G3 泳道在分支上全红,
    而它**没有 marker、三门都不跑**,只能靠全仓 grep 找。**动手第一件事就是先 grep 数
    出这轮到底有多少处**,别信这里写的历史数字。

---

## 十、裁决(2026-08-18 拍板,三条全部已定)

- **C-1 清单卡不进 v1**,但必须留可见说明,判据落在投影侧的 `omitted_result_sets` 计数
  (§五末)。
- **C-2 不做「禁止本库会话被公开分享」开关**。理由:报告已经是同一形态,单给会话加一道闸
  会造成两套口径。**链接寿命 = 创建者读权的寿命**——这就是它的缓解手段,也是必须在
  产品文档里写清的那句话。撤销群组授权 / 把人移出组 / 删组,他在那本库里发出去的每条
  会话链接当场全灭;恢复授权即复活。
- **C-3 公开页的轮次顺序必须与作者自己看到的逐字同源**。落地形态**不是**「另写一条排序
  一样的 SQL」,而是**把那段 ORDER BY 提成每个后端各一份的具名常量**,由
  `get_conversation` 与公开快照查询**共同消费**——两处各写一遍,迟早会在某次优化里分叉,
  而分叉的表现是「公开出去的内容顺序和我看到的不一样」,读者会读成内容被改过。
  另加一条测试:同一份数据下,两条路径返回的轮次序列**逐位相等**。

---

## 十一、已登记的后续(不在本特性内)

- 链接口令 / 有效期 / 访问次数
- 会话导出(md/pdf)
- 公开页的引用能否跳到**同样公开**的报告页(两条链接之间的互联)
