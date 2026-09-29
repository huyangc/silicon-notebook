# 检索层权限审计台账（2026-09-29）

范围：七份只读审计（段落通道、知识图谱通道、结构化通道、Memory 与学习类上下文、非问答入口、
全局问答、访问谓词与天花板地基），基于 master @ 9bacb8d6。所有发现均为**代码路径核实**，未做运行
复现；标「疑似」的是未能端到端走通的。行号以 PostgreSQL 侧为准。

架构裁决（用户 2026-09-29）：权限与来源范围的审核只属于检索/读取层；合成与终态核对假定到手的信息都是
用户可访问的。交付规则：特性不许留一半，本台账所有成立的发现在本轮修完。

## 用户裁决

| 编号 | 问题 | 裁决 |
| --- | --- | --- |
| M1 | Memory 抽出的图谱对象在共用库里如何存在 | **结构上隔离，只属于本人**：不进入共享的概念簇、社区摘要、规范关系、提及桥、分析产物、可视化索引与各类按库缓存；只在本人提问（经本人的隐藏来源天花板）和本人浏览时可见。 |
| M2 | 拷贝笔记本时 Memory 怎么处理 | **一律不带**：所有人的 Memory 及其元素、向量、图谱对象、关系、事实都不进副本。 |
| M3 | 把自己的私有库挂到共享库上，其他成员能否检索到 | **不能，只对挂载人生效**：其他成员提问时该库不参与，除非他们自己也有权读它。 |
| M4 | 公开分享的报告/会话里引用了作者自己的 Memory | **分享时明确提示，由作者决定**：创建分享链接时如实告知引用了几条私有 Memory，确认后照常公开。 |

主 agent 直接定的：入口永远冻结默认天花板；挂载库在单库问答里只开放可见来源；硬删除 Memory、成员退出、
账号删除时同步清掉全部派生行；按回答 id 的接口校验会话属主；收窄来源范围时提问人自己的隐藏来源不参与
（现有刻意行为，只改文档措辞）；部署管理员查看用户回答全文属审计权限，维持并在文档写明。

## 根因

| 根因 | 说明 |
| --- | --- |
| R1 无范围即无天花板 | `_validate_source_scope` 对缺省的 `source_scope` 返回 None（`api/ask_routes.py:166`），`AskService.ask` 随即不安装任何范围（`ask_service.py:1066`）；`scoped_allowed_source_ids` 返回 None，`filter_retrieval_items` 全放行。MCP `ask_notebook`（`mcp_tools/memory_context.py:471`）永远如此；HTTP 省略字段、无范围的报告同理。只有插件引擎自己合成了默认范围（`ask_service.py:2130-2211`）。旧的 `exclude` 模式持久化范围同样返回 None（`source_scope.py:878`）。 |
| R2 挂载库在单库运行里没有天花板 | `allows()` 对没有逐库天花板的非当前库返回 True（`source_scope.py:365`），`scoped_allowed_source_ids` 返回 None（`:904-911`）。只有全局运行安装逐库天花板。段落腿自己用了可见来源（`chunk_federation.py:1259`），KG/关系/社区/推导链/漫游腿没有。 |
| R3 Memory 派生物没有属主谓词 | 确认的 Memory 成为 `source_type='memory'` 的来源，带完整 `source_elements`、元素向量与 KG 抽取（`source_ingestion.py:2162-2260`）。只有 `hidden_source_ids`（`source_store.py:203`）按属主过滤，且只在冻结范围存在时才被用到。任何库读者（含只读成员）都能创建带 KG 抽取的 Memory（`memory_service.py:151`）。`api/source_routes.py:880` 的注释「Memory 投影根本不写 source_elements」与事实不符。 |
| R4 共享派生产物吸收了 Memory | 概念簇名称/描述、社区摘要、规范关系计数、最大簇分析、提及种子、`kwtok` 词项缓存、`fed_rxgraph` 图缓存、可视化索引都把 Memory 派生对象当普通成员。 |
| R5 对等模式的范围谓词对非参与库放行 | `covers_notebook` / `allows` / `scoped_allowed_source_ids` / `filter_retrieval_items` 在对等模式下对不在参与集里的库一律通过（`source_scope.py:330,365,911,1035`）；今天唯一的拒绝点是终态核对，而它即将只做完整性核对。 |

## 发现清单

严重度：P0 他人私有数据泄漏；P1 范围外或未勾选来源的文字泄漏；P2 过度过滤/召回损失/元数据披露；P3 卫生。

### A. 入口与天花板（R1）

| 编号 | 级别 | 发现 | 位置 | 修复方向 |
| --- | --- | --- | --- | --- |
| A-1 | P0 | 无范围的 Ask / MCP / 报告读到所有成员的 Memory 元素与 Memory 派生 KG | 见 R1；元素臂 `retrieval_candidates.py:2661-2674` → `source_store.py:1060`；KG 臂 `knowledge_store.py:1742,3106` | 入口处永远合成冻结范围（可见 ∪ 本人隐藏），消灭「无范围」；`docs/product-and-api.md:560` 同步 |
| A-2 | P3 | 旧 `exclude` 模式的持久化报告范围返回 None | `source_scope.py:878` | 物化为显式清单 |
| A-3 | P2 | 收窄时本人隐藏来源不参与与文档措辞不一致 | `ask_routes.py:201-203` | 行为不变，文档改准确 |
| A-4 | P3 | 写入侧没有守卫阻止给 `memory` 来源写 chunk（段落通道的安全依赖这条入库规则） | `chunk_store.py:380`、`source_ingestion.py:2173` | `replace_source_chunks` / `insert_rows` 拒绝 memory 来源 |

### B. 挂载库与图谱通道（R2）

| 编号 | 级别 | 发现 | 位置 | 修复方向 |
| --- | --- | --- | --- | --- |
| B-1 | P0 | 单库运行里挂载的非公共库的 KG / 关系腿没有天花板，库成员的 Memory 派生对象被召回并引用 | `retrieval_candidates.py:2346-2376`；`source_scope.py:365,904` | 冻结时给每个挂载参与库安装逐库天花板（可见来源）；KG/关系腿显式传 `_peer_visible_sources` |
| B-2 | P1 疑似 | 挂载库的 Knowhow 行经 PPR 进入答案（建图读全部 chunk、水合无过滤） | `graph_retrieval.py:472,480,1152` | 建图时对挂载库只取可见来源的 chunk 与成员关系，或在 `ppr_retrieve` 出口按库天花板过滤 |
| B-3 | P0 | 推导链（`follow_chain`）在未收窄的运行里不过来源闸，他人 Memory 的引文成为可引用锚点 | `graph_retrieval.py:1227`；`reasoning_retrieval.py:3472`；`retrieval_service.py:133-179` | 每一跳的证据无条件过 `filter_evidence`，无幸存证据即丢弃该跳 |
| B-4 | P0 | 弱支撑关系提示没有天花板，把他人 Memory 派生对象名写进 reflect 提示词 | `retrieval_service.py:519-525`；`unified_kg_store.py:1105-1135`；`retrieval_candidates.py:873-905` | 端点与样本关系过天花板（同 `community_member_peers` 的支持判定） |
| B-5 | P0（特定配置） | chunk 模式 KG 叠加游走只在对等模式下按天花板剪枝，本地天花板不生效；`kg_block`/`kg_id_map` 不过滤 | `retrieval_candidates.py:4738`；`retrieval_service.py:352-362` | `scope.ceiling_active` 时同样剪枝，按 `scope.allows` 判属主 |
| B-6 | P1 | `relations:` 行与链路标注不按关系证据过天花板，「×N 源」计所有来源；证据被剪空的边仍渲染标签 | `knowledge_store.py:1720-1739` | 按关系证据过滤，只计天花板内来源 |
| B-7 | P0 | 社区/共提及邻居名称对挂载库无天花板 | `services/communities.py:266,292` | 随 B-1 的逐库天花板生效，并补测试 |
| B-8 | P1 | `defines` 回落不看 `review_status='rejected'` 与对象 `status`，被拒关系/弃用对象仍供定义文字 | `knowledge_store.py:1921-1927` | 并入 PR-A store 修复 |
| B-9 | P2 | 收窄或漂移时精确标识符检索整条关闭；探测窗口先取后滤 | `retrieval_candidates.py:4323-4331,180-183`；`search.py:725-750` | 来源谓词下推进 `chunk_exact_candidate_rows`（双后端），去掉关闭闸 |
| B-10 | P3 | ANN 候选矩阵未随候选掩码 | `retrieval_candidates.py:3776-3786` | `_mask_vector_matrix(kept_ids)` |
| B-11 | P1 疑似 | 晋升到公共库的对象按全局 id 回读推广者私有库的元素原文与来源标题（无库与存活谓词） | `knowledge_store.py:1867-1896`；`evidence_context.py:265`；`governance_store.py:1500,1633` | 读取限定在对象自己的库且库存活；晋升对象携带公共库自有的出处（快照引文） |
| B-12 | P2 | 晋升对象在全局问答里被整体剪掉（证据指向私有库来源） | `retrieval_candidates.py:2155-2166` | 随 B-11 的出处改造解决 |

### C. Memory 隔离（R3、R4；裁决 M1）

| 编号 | 级别 | 发现 | 位置 | 修复方向 |
| --- | --- | --- | --- | --- |
| C-1 | P0 | 知识库列表、图谱、可视化、KG 搜索、节点上下文、概念详情返回他人 Memory 派生对象及引文 | `knowledge_store.py:2055,2104,1109,1898`；`knowledge_query.py:186,444`；`kg_routes.py` | 统一的 Memory 属主谓词下推到所有对象/关系读取；Memory 派生对象只对属主可见 |
| C-2 | P0 | 笔记本搜索的 KG 腿无隐藏来源过滤（HTTP 与 MCP `search_notebook_context`） | `search.py:977-979`；`query_store.py:1975` | 同一谓词 |
| C-3 | P0 | 来源与元素接口只校验库读权限，可读出他人 Memory 全文；MCP `get_cited_element` 同；元素 id 可预测 | `source_routes.py:491,791,801,885,916-942`；`notebook_sharing.py:1147`；`mcp_tools/citations.py:44-72` | `user_can_read_source` 与 `source_readable_in_participant_scope` 对 memory 来源要求 `memory_items.created_by = 调用者` |
| C-4 | P1 | 共享派生产物带 Memory 内容：簇名称/描述、社区摘要、最大簇分析、规范关系、提及种子 | `knowledge_lifecycle.py:5316-5380,7012,7049-7070`；`unified_kg_store.py:333,1710` | 按 M1：Memory 派生对象/关系不进入共享代际的任何输入；上线后对含 Memory 的库触发一次重建 |
| C-5 | P2 疑似 | 按库缓存吸收 Memory：`kwtok` 词项、`fed_rxgraph` 整图、关系向量矩阵 Top-K | `retrieval_candidates.py:1333,1709`；`graph_retrieval.py:413` | 缓存只含共享内容；本人 Memory 对象按用户另行并入或读取时叠加 |
| C-6 | P2 | 冲突候选的 `rationale` / `resolved_payload`、合并候选、查重列表未见 Memory 过滤（未核实） | `governance_store.py:686,930`；`kg_routes.py:426,565`；`knowledge_routes.py:220` | 随 M1 在候选生成处排除 Memory 对象，读取处加属主谓词 |
| C-7 | P3 | 计数侧信道：看板计数、`/knowledge-types`、列表总数、分享预览大小含他人 Memory | `knowledge_counts_cache`；`collection_catalog.py:838` | 计数只含共享内容加本人 Memory |
| C-8 | P0 | 深拷贝/分享链接拷贝带走所有成员的 Memory 投影 | `sharing_store.py:128-191`；`notebook_sharing.py:376-389` | 按 M2：快照查询排除 memory 来源及全部派生行；翻转 `test_notebook_share_copy.py:487` |
| C-9 | P2 | 硬删除 / 批量删除 Memory 只删主表行，派生来源、元素、向量、KG 残留；账号删除同 | `memory_service.py:801-807` | 删除时调用 `remove_memory_source`；孤儿清扫；成员退出与账号删除同处理 |
| C-10 | P2 疑似 | 弃用 Memory 后，簇名称、社区摘要、分析产物、冲突理由滞留到下次重建 | `delete_source` 只置 dirty | 随 M1 结构隔离后不再有共享派生物，残余只剩本人可见部分 |
| C-11 | P1 | MCP `memory:read` 可被绕过：过滤只丢 `memory_id` 非空的引用，Memory 投影的元素/KG/链命中不带 `memory_id`；`ask_notebook` 的锚点缺少引用那道过滤 | `mcp_tools/memory_context.py:923-963` | 无 `memory:read` 的 token 的天花板不含本人隐藏 Memory 来源；锚点与引用同过滤 |

### D. 回答级与分享（读取层）

| 编号 | 级别 | 发现 | 位置 | 修复方向 |
| --- | --- | --- | --- | --- |
| D-1 | P1 | `memory-preview`、`from-answer`、`feedback` 只校验库读权限，不校验会话属主（需先知道回答 id） | `memory_routes.py:396,479,522`；`memory_service.py:648-705`；`ask_routes.py:1338`；`notebook_sharing.py:1151` | 要求 `conversations.created_by = 调用者` |
| D-2 | P2 | 全局会话分享的复核集合偏窄（只看被引库，答案可能转述了未被引的库） | `global_ask.py:479-502` | 每轮复核 已检索 ∪ 已引用 的库 |
| D-3 | P3 | 笔记本会话与报告的公开页不复核挂载库是否仍有效（全局分支会） | `ask_routes.py:1236`；`report_public_view.py` | 每次打开复核挂载库 |
| D-4 | — | 公开分享含作者自己的 Memory 引用 | `report_engine.py:2427,3594`；`conversation_public_view.py` | 按 M4：创建分享时提示条数并确认 |
| D-5 | P3 | `catalog_routes._owned_source` 接受 memory 来源（知道 id 即可见标题）；插件元素命中来源不在 `source_origin` 时回落到当前库 | `catalog_routes.py:138`；`plugin_ask_engine.py:641` | 拒绝；丢弃该命中 |

### E. 全局问答（R5）

| 编号 | 级别 | 发现 | 位置 | 修复方向 |
| --- | --- | --- | --- | --- |
| E-1 | P1 潜在 | 对等模式下范围谓词对非参与库放行；一旦终态核对不再承担权限角色，全靠每个生产者自觉 | `source_scope.py:330,365,911,1035` | `subjectless` 时 `covers_notebook(nb)` = 该库有天花板；端到端测试：锚点上挂了未选中的公共库，各通道都不出其内容 |
| E-2 | P2 | Knowhow 完整枚举在全局运行里只在锚点库上跑，且越过该库天花板 | `ask_service.py:4869-4873` | 闸加 `subjectless_run_active()` |
| E-3 | P2 | 枚举/目录/文档概览读的是活的来源清单而非冻结天花板，冻结后上传的来源会被列出和引用 | `collection_enumeration.py:1147-1150` | 与逐库天花板取交（并入 PR-B） |
| E-4 | P1 | 枚举动作接受模型传入的任意 `source_id`，只校验库成员身份不校验天花板 | `collection_enumeration.py:1589-1605` | `_explicit_source_plan` 校验 `scope.allows`（并入 PR-B） |
| E-5 | P3 | `local_only` 在对等模式下只列锚点库；集合地图 `knowhow tables` 计数不看天花板 | `collection_enumeration.py:1149`；`collection_catalog.py:881` | 对等模式隐藏该选项；计数按可达范围（并入 PR-B） |
| E-6 | P3 | 锚点库的 Agent 画像在对等模式下仍注入 | `reasoning_retrieval.py:4740-4744` | 对等模式不注入 |
| E-7 | P3 | 运行中途撤权只在段落回执与引擎返回后被发现 | `global_ask.py:1188,1799,1836` | 登记为已知时间窗；整份作业失败的现有处理不变 |

### F. 已确认正确的部分

段落主通道（向量、词法、双语关键词、联邦合并、生成问题索引）都把天花板下推到 LIMIT 之下；工作线程的
上下文传播无缺口；访问谓词本身（读/写/管理、存活、挂载有效性、借入不转借）成立；Memory 直接检索、
Agent 画像、检索经验库、观察记录、待办铃铛按用户隔离正确；全局问答的范围解析、冻结、授权复核、
属主读取与幂等重放正确；公开页只输出白名单字段。

## 钉住错误行为、修复时必须改写的现有测试

- `test_notebook_share_copy.py:487-520`（Memory 随拷贝迁移）
- `test_memory_mcp.py:3321-3324`（同库无属主 Memory 来源可读）
- `test_memory_api.py:158`（读者从不属于自己的回答保存出处）
- `test_knowledge_context_source_ceiling.py:221`、`test_peer_ceiling_subgraph.py:301`（无天花板即不动）
- `test_global_ask_engine_parity.py` 中两条以 `out_of_ceiling` / `unattributed` 作废的用例
