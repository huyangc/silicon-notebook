# 设计：大 notebook 的 viz-only 索引(点亮休眠的 KG 视图快路径)

- 日期：2026-07-01
- 范围：让普通(非 base 层)大 notebook 的 KG 视图不再走全量派生的慢回退路径。为其单独持久化一份「只含 viz」的轻量索引,懒构建(同步)+ 重合并时刷新,并在「图谱处理」面板暴露索引状态。
- 不在本 spec：检索侧(scale 索引 / PPR)不动;base 层 notebook 的完整 scale 索引流程不动;后台异步构建 + 进度轮询(本期用同步懒构建)。

## 背景与根因

现网某 notebook `nb-4ad4ba7939`(concept 4.5 万 / claim 19 万 / formula 6.4 万 / procedure 9k,约 30.8 万对象)在 KG 视图慢:`GET /unified-kg?level=object&limit=320` = 28.5s,`GET /objects/{id}/neighbors` = 37s。

根因(已定位):KG 视图有一条 SP1 快路径(`_unified_graph_bounded` / `viz_neighbors`),从持久化的**折叠 viz 数组**直接切度数 top-N,≈0 秒。但它要求 `self._scale_index(nb)` 返回一个已构建的索引,而这个索引**只在离线为 base 层 notebook 构建**。普通 UI notebook 从不构建它,于是每次请求都落到慢回退:

- `unified_graph` → `_unified_graph_full`([sqlite_repository.py:4449](../../../backend/app/services/sqlite_repository.py)):`SELECT ... FROM knowledge_objects`(全部 30.8 万行)+ 对每一行 `json.loads(payload)`(其中 25 万+ claim/formula 在 object→concept 折叠后大量丢弃)+ 全量 relations + 全量 `cluster_map` + O(V+E) 折叠。纯 Python 解析 30 万个 payload 是主要墙。
- `kg_neighbors` → `_kg_neighbors_db`([sqlite_repository.py:4548](../../../backend/app/services/sqlite_repository.py)):每次调用重建整张 `cluster_map` + 反向索引 + 对折叠成数百成员的 canonical 做 `source IN (...) OR target IN (...)` 宽扫描,且无缓存。

**关键洞察**:viz 快路径需要的只是那 6 个折叠数组(canonical id / 邻接 / 度数 / 类型 / 名字 / 边),**根本不需要 4GB 的 ANN 和 CSR 转移阵**。所以给普通大 notebook 单独持久化一份「只含 viz」的索引即可点亮快路径,而不必构建完整 scale 索引。

## 架构

一个独立的、与检索 scale 索引隔离的 viz-only 索引产物 + 一个统一访问器 `_viz_index`,`unified_graph` / `kg_neighbors` 两处改接到它;懒构建(同步、自愈)+ `rebuild_unified_kg` 结尾主动刷新;版本失效复用 `_scale_index_version`;状态经 `/unified-kg/status` 暴露到前端「图谱处理」面板。

## 组件

### 1. 独立 viz 索引产物 `kg/viz_index.py`(新建)
- `save_viz_index(out_dir, *, viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload, manifest)` 与 `load_viz_index(out_dir)`。只写/读 6 个 viz 数组 + `manifest.json`(含 `version`、`n_viz_nodes`、`n_viz_edges`)。
- 落盘目录 `{storage_dir}/kg_viz/{notebook_id}/`,**与检索索引的 `kg_index/` 严格分开**。理由:若把只含 viz、transition 为空的产物写进 `kg_index/`,`_scale_index` 会把它当成可用检索索引加载,`scale_ppr` 用空 transition → 直接搞坏该库的检索。隔离目录彻底避免污染。
- `load_viz_index` 返回一个轻量对象(或复用 `ScaleIndex` dataclass 的子集),暴露属性名与现有 `idx` 一致:`viz_ids / viz_adj / viz_deg / viz_types / viz_names / viz_edges / manifest`,使 `_unified_graph_bounded` / `kg_neighbors` 无需区分来源。

### 2. `build_viz_index(notebook_id)`(sqlite_repository 新方法)
- 产出格式复用现有 `_build_viz_graph_arrays` 的 6 数组契约(保证与 `_unified_graph_bounded` 等价)。
- **构建时避免整 payload `json.loads`**:折叠 viz 图只需每个节点的 `id / object_type / name`。名字改用 SQL `json_extract(payload,'$.name')` 直接取(SQLite JSON1),不再对 30.8 万行做 Python `json.loads`。这是把首次同步构建从 ~28s 压到几秒级的关键。
  - 具体:新增内部取数 `_viz_nodes_lite(notebook_id)`,`SELECT id, object_type, json_extract(payload,'$.name') AS name FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'`;relations 用现有 `relations_for_notebook`;折叠用现有 `cluster_map` + `derive_unified_graph` 的等价折叠(在 lite 节点上)。
- 构建完成 `save_viz_index` 落 `kg_viz/{nb}/`,manifest.version = 当前 `_scale_index_version(notebook_id)`。
- 空库(无 object)→ 不落盘,返回空标记(让访问器回退)。

### 3. 统一访问器 `_viz_index(notebook_id)`(进程内缓存 + 版本校验)
优先级:
1. 有效的完整 `_scale_index(nb)`(base 库,version 匹配)→ 直接返回它(本就带 viz 数组)。
2. 否则 `load_viz_index(kg_viz/{nb})`;若存在且 `manifest.version == _scale_index_version(nb)` → 进程缓存(`_viz_idx_cache`)并返回。
3. 否则**同步** `build_viz_index` → 持久化 → 缓存 → 返回。
4. 构建产出为空(空库)→ 返回 None。
- 进程缓存 `self._viz_idx_cache: Dict[str, Any]`,命中时按 version O(1) 比对(与 `_scale_index` 同款)。

### 4. 调用点改接
- `unified_graph`([sqlite_repository.py:4421](../../../backend/app/services/sqlite_repository.py)):当前 `if limit is not None and level != "concept": idx = self._scale_index(...)` → 改为 `idx = self._viz_index(...)`;`idx` 非空则 `_unified_graph_bounded(idx, limit)`。`idx is None` 走原全量派生(小库)。
- `kg_neighbors`([sqlite_repository.py:4519](../../../backend/app/services/sqlite_repository.py)):`idx = self._scale_index(...)` → `self._viz_index(...)`;非空走 `viz_neighbors` 快路径,否则 `_kg_neighbors_db`。

### 5. 失效 & 重合并刷新
- 访问时按 `_scale_index_version` O(1) 比对;不匹配即在 `_viz_index` 内重建(与 scale 索引一致)。KG 变更路径(上传/删除/合并裁决/`update_knowledge`)已改动 objects/relations/clusters 计数或时间戳 → version 变 → 下次访问自动重建。
- `rebuild_unified_kg`([sqlite_repository.py](../../../backend/app/services/sqlite_repository.py))结尾**主动调用 `build_viz_index`**:它刚算完聚类、数据在手,顺手落一份新鲜索引,避免下次打开图谱触发懒构建卡一下,并覆盖「同秒原地编辑致 version 元组不变」这类边角(与 scale 索引同款已知局限,靠重合并兜底)。

### 6. 可观测:`/unified-kg/status` 扩展 + 前端徽章
- `unified_kg_status(notebook_id)` 增加三字段(**只读探针,绝不触发构建**):
  - `viz_indexed: bool` —— `_scale_index` 有效,或 `kg_viz/{nb}` 存在且 version 匹配当前 `_scale_index_version`。
  - `viz_nodes: int` / `viz_edges: int` —— 来自 manifest(O(1) 读)。
  - `viz_stale: bool` —— `kg_viz/{nb}` 存在但 version 不匹配(下次访问会重建)。
  - 实现:新增 `_viz_index_probe(notebook_id)`,只 `load_viz_index` 读 manifest 比对 version,不构建、不缓存重建。
- `UnifiedKgStatus`(pydantic)+ 前端 `type UnifiedKgStatus`([page.tsx:305](../../../frontend/app/page.tsx))加同名字段。
- 前端在「当前视图」小节现有状态标签行([page.tsx:3640](../../../frontend/app/page.tsx))并排加一枚徽章,三态:
  - 🟢 `图谱索引：已就绪 · N 节点`(`viz_indexed`)
  - ⚪ `图谱索引：未构建`(非 indexed 且非 stale)
  - 🟡 `图谱索引：待刷新`(`viz_stale`)
  - 徽章须对齐现有 `tag`/`tag-row` 样式,符合 UI 对齐标准。

## 数据流 / 边界

- **首次打开(无索引)**:`/unified-kg?level=object&limit=80` → `_viz_index` 未命中 → 同步 `build_viz_index`(几秒)→ 持久化 + 缓存 → `_unified_graph_bounded` 返回。之后 O(1) 命中 ≈0。
- **点节点**:`/neighbors` → `kg_neighbors` → `_viz_index` 命中 → `viz_neighbors`。
- **base 库**:已有完整 scale 索引 → `_viz_index` 复用它,不重复建、不占额外磁盘,状态显示「已就绪」。
- **小库 / 空库**:`build_viz_index` 产出为空 → `_viz_index` 返回 None → 现有全量派生 / DB 回退,行为不变。
- **并发首访**:两请求同时懒构建 → 各算一遍、后写覆盖(结果等价、幂等);不加锁(与现有 `_scale_index` 惰性加载一致,YAGNI)。
- **status 面板**:只读探针,打开面板不会触发构建 → 不卡。

## 测试

- **等价**(核心不变量):中等 fixture 上,`_viz_index` 驱动的 `unified_graph(level=object, limit=N)` 与 `kg_neighbors(nb, oid, cap)` 的 nodes/edges/totals,与全量派生(`_unified_graph_full` + `limit_graph_by_degree` / `_kg_neighbors_db`)**逐字段相等**。复用 SP1 已有等价测试骨架。
- **构建正确 + 提速**:`build_viz_index` 取到的 name 与 `json.loads(payload)['name']` 一致(含缺 name、payload 为空的边界);断言取数走 `json_extract` 而非整 payload 解析。
- **持久化往返**:`save_viz_index`→`load_viz_index` 数组/manifest 完整;version 不匹配 → `_viz_index` 判失效并重建。
- **检索隔离**:建了 `kg_viz/{nb}` 后,`_scale_index(nb)`(检索)对该库仍返回 None(检索路径不受污染)。
- **重合并刷新**:`rebuild_unified_kg` 后 `kg_viz/{nb}` 的 version == 新 `_scale_index_version`;`viz_indexed==True`。
- **status 探针**:未建 → `viz_indexed=False`;建后 → `True` + `viz_nodes>0`;KG 变更后 → `viz_stale=True`;探针不触发构建(用调用计数/打桩验证)。
- **前端**:tsc + 现有测试;徽章三态渲染正确、对齐。
- **全量回归**绿(后端 pytest / 前端 tsc)。

## 风险

- **`json_extract` 兼容性**:依赖 SQLite JSON1(现代 SQLite 内置)。若 payload 非合法 JSON,`json_extract` 返回 NULL → name 取空串(与现状对缺 name 的行为一致)。测试覆盖。
- **version 元组同秒局限**:`_scale_index_version` 用 count+MAX(timestamp),同秒原地编辑(改名/翻转裁决且计数不变)可能漏判失效 → 靠「重新合并」主动重建兜底(与 scale 索引同款,已知可接受)。
- **磁盘占用**:`kg_viz/{nb}` 只存折叠图(canonical 级,远小于原始对象数),量级可控;随 notebook 数增长而增长,可后续加清理(本期不做)。
- **懒构建首访延迟**:即便提速后仍有几秒同步构建。用户已确认接受「同步懒构建」;`rebuild` 主动刷新进一步降低触发概率。

## 实施分期(可并行)

- **P1**:`kg/viz_index.py`(save/load + 轻量对象)+ 单测。独立文件,**可与 P2 并行**。
- **P2**:`build_viz_index` + `_viz_nodes_lite`(json_extract 取数)+ 构建正确/提速测试。依赖 P1 的 save 接口签名(可先约定签名并行)。
- **P3**:`_viz_index` 访问器 + `_viz_idx_cache` + `unified_graph`/`kg_neighbors` 改接 + 等价测试。依赖 P1、P2。
- **P4**:`rebuild_unified_kg` 结尾主动刷新 + `unified_kg_status` 扩展(`_viz_index_probe`)+ `UnifiedKgStatus` 字段 + 探针测试。依赖 P2、P3。
- **P5**:前端 `UnifiedKgStatus` 类型 + 「图谱处理/当前视图」状态徽章三态 + tsc/视觉验证。依赖 P4。
