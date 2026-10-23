# 经挂载读的合并知识对象：不再带出隐藏成员的名字（计划，2026-10-23）

来源：`fangan_todo.md` 第二节「知识图谱」的「隐藏成员名字经 canonical id 暴露，需全产品统一处理」。
用户裁决（2026-10-23）：**只修挂载读的读侧，内部 id 不动**（不改 canonical id 派生、不引入对外不透明 id）。

## 1. 摸排结论（裁决依据）

canonical id = `K-`（claim / formula / procedure 为 `KL-` / `KF-` / `KP-`）+ 合并组全体成员的最小种子；
`canonical_name` = 成员最多的种子第一次见到的名字。两者都可能来自对某个读者隐藏的成员。读者分两类
（`KgViewerScopeReader.for_notebook`）：

- **直接读者**（过 `require_notebook_read`：owner ∪ 成员 ∪ 有效授权边；与 for_notebook 的 member 判定同一谓词
  `user_can_read_notebook`）。对他们没有越界：Knowhow 投影整库可读（裁决 Q4；Knowhow 表读接口只挂
  `require_notebook_read`）；Memory 从 0067 / v87 起不进共享簇（合并对象流带 `_not_memory`，迁移删掉了含 Memory
  成员的簇，隔离重建启动后自动完成）。所以 pending-merges、统一图谱、KG 搜索、概念详情、图谱分析
  （largest_clusters、板块成员名）、问题清单 F4 的 canonical id / 名字对他们的读者都不越界。
- **经挂载读的非成员**：笔记本 A 挂了公共库 L，A 的成员不能直接读 L。`source_notebook_id=L` 的三个详情读按
  「L 的可见来源」判定；问答对每个被挂载库（以及全局问答的每个库）冻结的来源天花板也只含可见来源。
  **真实泄漏只在这里**：
  1. 邻居视图 `nodes[].id`、边端点、`focus_id` 原样是 `K-<最小种子>`（标签已按第一个可见成员改写，id 没换；
     临时探针实测 `K-quietzebraxylophone`，Knowhow 成员 "Quiet Zebra Xylophone (QZX)" 经缩写别名与可见的 "QZX" 合并）。
  2. 概念详情 `canonical_id` 同上；传可见成员 id 也会被 `_resolve_concept_member` 转成簇 id 回显。
  3. 焦点本身被隐藏时，邻居返回 `{**result, nodes: [], edges: []}`，`focus_id` 仍是簇 id（与未知 id 的形状不同）。
  4. 簇 id 作为输入（概念详情路径、邻居焦点）对非成员是存在性预言机：猜中 `K-<隐藏写法>` 得 200。
  5. 问答对比兄弟（共提 / 社区成员）取存储的 `canonical_name`，闸只要求「簇有天花板内来源支撑」，名字本身可能是
     天花板外成员的写法；它进 reasoning 轨迹 `expand_community.detail.peers`（HTTP / SSE / 会话详情 / MCP ask）、
     chunk 模式子查询与提示。
  6. 陈旧名字：Knowhow 重投影 / 删表只删被丢弃对象的簇成员行，留下的成员行仍带旧的 canonical id / name，
     直到下一次重新合并；此时簇里已经没有「活的隐藏成员」，按现有判据不会被改写。

现实触发面窄：Knowhow 投影对象的类型 = 列名，只有列名恰为 concept / claim / formula / procedure 才进合并簇。

## 2. 三条规则（实现目标）

**R1 直接读者：去掉 id 替换的隔离门。** 现在「可能含该读者不可读成员的簇按第一个可见成员的对象 id 作答」只在
`memory_isolation_pending` 时生效（KG 搜索折叠命中、统一图谱、邻居含 `focus_id`、概念详情）。改为：凡
`cluster_needs_check` 为真就按第一个可见成员的对象 id 作答，与隔离标记无关。隔离标记仍决定是否读持久化产物
（`artifacts`），那部分不动。隔离后直接读者没有这种簇，响应逐字不变。

**R2 经挂载读的非成员：响应里不出现任何合并知识对象的 id 或存储名。** 适用于 `for_notebook(L, active=A)` 判为非成员
的读取（概念详情、邻居；对象上下文本来就只回原始对象）：

- 这类读取**总是**建 scope（即使 L 没有任何隐藏来源，也是为了覆盖规则 1.6 的陈旧名字），并标记「按成员作答」；
- 响应里的**每一个**簇（不只是含活的隐藏成员的簇）都按第一个可见成员作答：id = 该成员的对象 id，名字 = 该成员的
  名字；没有可见活成员的簇照旧不出现；
- 簇 id 作为**输入**（概念详情路径里的 `{canonical_id}`、邻居的焦点）按未知 id 作答（概念详情 404；邻居与未知 id
  逐字相同的空邻域）——非成员只拿得到成员对象 id，按成员 id 打开照旧可用（`_resolve_concept_member`）；
- 焦点被隐藏时，邻居的响应与未知 id 逐字相同（`focus_id` / `focus_object_id` 不带簇 id）；
- 成本：建 scope 的两次有界读（`memory_source_ids` / `hidden_source_ids`）非成员分支今天已经在做；新增的只有
  「本页每个簇的第一个可见成员」那一次批量读（没有隐藏来源时窗口为 1），不得为此把隐藏对象折叠到簇。

**R3 问答对比兄弟名：名字必须来自天花板内的成员。** 在有来源天花板的读（`_ceiling_kwargs` 非空）里，对每个经
来源闸留下的兄弟簇：在成员 id 顺序里找「天花板内成员」；若这批读到的天花板内成员里有人的
名字与存储的 `canonical_name` 逐字相同，名字用存储名（零行为变化的常见情形），否则用第一个名字非空的天花板内成员的名字；
一个天花板内成员都没有的簇丢弃。无天花板时 SQL 与结果逐字不变（既有用例
`test_absent_ceiling_is_byte_identical` 继续成立）。共提与社区两路同一处出口、同一规则。成员读取一次批量
（每簇首窗口），窗口里没有天花板内成员的簇才单独往后翻页（页大小有上限）。

「天花板内成员」与对象规则对齐（`KgViewerScope.row_hidden`：对象只由自己的来源决定是否隐藏）：成员自己的
`source_id` 为空（晋升进公共库的对象一律写成 `source_id=''`，证据改写到库内可见的 `promotion` 来源）或在该库天花板
内即算；只有 `source_id` 非空且不在天花板内的成员不能命名。（修正，2026-10-23，T2 质量评审 P1：初版写成「自己的
`source_id` 在天花板内」，会把晋升对象构成的兄弟簇整簇丢掉。）共提路在天花板下按 `top_k` 的两倍从 SQL 取对端，
R3 之后再截到 `top_k`，被丢弃的簇不白占名额。

**R4 问答对比的焦点解析：只认天花板内成员的写法。**（用户裁决 2026-10-23，T2 实现中发现。）`_resolve_focal`
按 `lower(canonical_name) == 焦点归一键` 解析焦点簇；存储名可能是天花板外成员的写法，提问者猜中隐藏写法就能解析出
焦点并拿到兄弟（存在性预言机）。在有天花板的读里，解析出的焦点簇还必须有一个天花板内成员（同 R3 定义）的名字
`lower(name)` 等于该键，否则按未解析处理（与今天解析不到时同一条路径、同一个事件）。成员检查只取名字等于该键的
成员行（SQL 里按 `lower(payload 名)` 过滤，返回其 `source_id`），天花板判定在 Python 里做（冻结天花板是集合）；
不得把 ~4.9 万个来源 id 作为数组参数逐行比较。无天花板时 SQL 与结果逐字不变、零额外语句。可见写法（如 `QZX`）
因存储名是另一种写法而解析不到焦点，是既有的召回局限，不在本次。

## 3. 任务

### T1（后端 KG 详情读，R1 + R2）

文件：`backend/app/services/kg_viewer_scope.py`、`backend/app/services/knowledge_query.py`、
`backend/app/services/knowledge_lifecycle.py`（只改邻居 / 图谱视图里与 id 替换、焦点隐藏相关的几处）、
以及相应用例。两个后端共用服务层；PG 孪生用例必须补。

验收：
- 新增 SQLite 路由级用例（真路由、真鉴权，参照 `tests/test_kg_viewer_scope_routes.py::_mounted_library`）：
  公共库 L 含可见概念 `QZX` 与 Knowhow 来源概念 `Quiet Zebra Xylophone (QZX)`（簇 id 由后者铸出，用例先断言
  这一前提），挂进共享笔记本；非成员成员读邻居（以可见成员、以邻接对象为焦点）、概念详情（以可见成员 id 打开）：
  对**整个响应文本**做不分大小写的子串扫描，`quiet zebra xylophone`、种子 `quietzebraxylophone`、`zebra` 一个都
  不出现；返回的节点 id / `focus_id` / `canonical_id` 是成员对象 id，按它再打开概念详情得 200。
- 簇 id 作输入：非成员以 `K-quietzebraxylophone` 打开概念详情得 404；以它为焦点读邻居与以一个从未存在的 id 读邻居
  逐字相同（id 字符串替换后比较）。
- 焦点被隐藏：非成员以 Knowhow 对象 id 读邻居，与未知 id 逐字相同。
- 陈旧名字：在 L 上把 Knowhow 成员的簇成员行删掉（或删掉该 Knowhow 来源与对象，模拟删表），存储的 canonical id /
  name 仍是旧写法时，非成员的响应扫描同样干净（含 L 已没有任何隐藏来源的情形）。
- 库成员（L 的 owner）经同一挂载读到原样的簇 id 与名字（不受 R2 影响）。
- R1：`test_kg_service_readers.py::test_a_pending_notebook_never_answers_a_search_hit_by_a_seeded_cluster_id`
  与 PG 孪生的 `pending=False` 分支改为同样按成员 id 作答（文档化行为变化），其余既有用例不改语义。
- PG 孪生：在 `tests/postgres/` 里补等价的服务级用例（R2 的扫描、簇 id 输入、焦点隐藏、陈旧名字），
  `postgres_integration` 标记，本机按 `-n 1` 一次性库跑通。
- 变异：分别撤回 (a) R2 的「总是建 scope / 每个簇都按成员作答」、(b) 簇 id 输入按未知 id、(c) 焦点隐藏形状、
  (d) R1 去门，各自让对应用例变红；变异前先 commit，结束后 `git checkout` 还原并确认工作树干净。

### T2（问答对比兄弟名，R3）

文件：`backend/app/services/communities.py`、两个后端的 `unified_kg_store.py`（若需要让兄弟读返回 canonical id
或新增「首窗口成员 + 自己的 source_id」批量读）、仓库契约夹具（若新增 store 方法，按 `docs/development.md` 用
`scripts/generate_repository_contract_fixtures.py --rebaseline-surface` 更新）、相应用例
（`tests/test_comparison_peer_ceiling.py` 一带，PG 孪生）。

验收：
- 天花板内成员 `QZX`、天花板外成员 `Quiet Zebra Xylophone (QZX)` 且存储名是后者：共提、社区两路返回的名字、
  chunk 子查询、reasoning `expand_community` 轨迹（整段轨迹文本不分大小写扫描）都不出现隐藏写法与其种子，
  出现 `QZX`。
- 存储名由天花板内成员携带时名字不变；无天花板逐字不变；没有天花板内成员的簇丢弃；陈旧存储名（携带它的成员行
  已删）不出现。
- 成员读取是一次批量读（用例数语句），不按兄弟逐个读。
- PG 孪生同上；变异：撤回改名逻辑让用例变红。
- 晋升形状：成员 `source_id=''`、证据引天花板内来源时，簇照常保留并可由它命名（两个后端；撤回修正让用例变红）。
- R4：存储名是隐藏写法时，以隐藏写法为焦点在天花板下解析不到（不返回兄弟、走未解析事件），以存储名为焦点且有
  天花板内成员携带该写法时照常解析；无天花板逐字不变；两个后端；变异撤回让用例变红。

### T3（文档与台账）

- `docs/product-and-api.md` / `_zh.md`：「KG detail reads」一段写入 R1 / R2（去掉「仅隔离重建前」的限定，写明
  直接读者为何不受影响）；pending-merges 与图谱分析、问题清单处补一句为何它们的 canonical id / 名字对其读者
  不越界；对比兄弟名写入 R3。
- `docs/development.md`（及中文配对，如有）可见性边界一节补一条：canonical id / 存储名的读者分类与 R2 / R3。
- `backend/app/services/kg_findings_view.py` 里 `INTERNAL_EVIDENCE_KEYS` 的注释改成准确的理由（种子是内部匹配键，
  不对外；Knowhow 对问题清单读者本就可读）。
- `fangan_todo.md`：删去该条；`fangan_done.md` 补一节（问题、裁决、R1–R3、PR 号）。

## 4. 不做

- 不改 canonical id / canonical_name 的派生与存储，不改跨库桥接、决定键、F4 匹配、同步 / 拷贝 / 导出。
- 直接读者面的 canonical id / 名字不做额外改写（按 Q4 / M1 不越界）。
