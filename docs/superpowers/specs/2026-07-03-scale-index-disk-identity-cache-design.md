# 大库检索按磁盘索引身份缓存(脱离摄取 churn)设计

**日期**: 2026-07-03
**分支**: feat/scale-index-disk-identity
**状态**: 已批准方向 + 取舍(用户确认「恒定成本优先,陈旧可接受」)

## 问题

严格推理在大库上、摄取进行时,于「初检索得到 N 候选」之后的 PPR seed pass 冻结 ~30min。

根因(已逐行核实,非 delta 门控问题——PR#178 四处 delta 门控齐全且正确):
`_scale_index(notebook_id, allow_stale=True)`([sqlite_repository.py:7994-8011])的缓存版本键
`cur = _scale_index_version(nb)` 含 `kg_mutation_seq`(每写一个 chunk/object 就 bump)。
只要库里有 delta(manifest 版本停在上次 build,cur 反映当前 DB),`manifest.version == cur`
**恒为 False** → 走 8011 的 `return idx if allow_stale else None`,该 stale 实例**永不写进
进程缓存**。于是:

- 每次严格推理查询 → 新建 stale ScaleIndex → `load_scale_index` 重新反序列化(graph.npz +
  113 万条 node_ids pickle + labels,几百 MB);
- 新实例 `ann_handle=None` → `_open_scale_ann`([8013+])→ `hnswlib.load_index` 把
  **506,771×4096 ≈ 8GB** 的 kg ANN 整个读进内存;
- PPR chunk 种子([_retrieve_chunks:10402])再独立调一次 `_scale_index(allow_stale=True)` →
  重载 **129,459×4096 ≈ 2GB** 的 chunk ANN。

摄取期磁盘 I/O 争用下,每查询 ~10GB 反序列化 = 分钟级冻结。通用问答只碰 2GB chunk ANN
(约 5-10s,用户当「还行」),推理多扛 8GB kg ANN + 组合图重建,是它 5 倍多 → 「卡住」。

这与「陈旧」无关——**只要有 delta 就每查询重载**,即使摄取停止也不自愈(manifest 版本停在
build 时,cur 反映累积 delta,永不相等),唯一复位是重建/fold 索引让 manifest.version 重新
== cur。

## 核心洞见(重构口径)

`_scale_index(allow_stale=True)` 的语义是**「给我磁盘上的已索引部分」**。已索引部分只在
rebuild/fold 时变,**不随一个 chunk 入库而变**。所以它的缓存必须按**磁盘索引身份**键控,
永不掺入 `kg_mutation_seq`。

用户取舍(已确认):大库检索恒定 O(1) 成本优先;delta 默认查不到,靠 auto-fold/重建最终
收进索引后才可见——这正是「只检索已索引部分、不管是否在新增」的应有语义。逃生阀
`SCALE_SEARCH_INCLUDE_DELTA=true`(已存在)在需要强一致时回到含 delta 暴力的慢路径。

**关键正确性洞见**:stale-serve 与 flag 无关地正确。ANN 核按定义就是磁盘已索引部分;「stale」
只意味「不含 delta」——正是我们要的。flag=ON 时,delta 的新鲜度来自 `⊕delta` 暴力块查询
live DB(检索侧已有,门控在 `scale_search_include_delta` 之后),不来自被缓存的核。

## 方案对比

1. 仅在 `_open_scale_ann` 加 mtime 键控 handle 缓存 —— 最窄,但留下每查询 `load_scale_index`
   numpy 重载 + `_retrieve_chunks` 二次加载。半个修复,弃。
2. 独立 `_stale_idx_cache` —— 语义干净,但新建库(无 delta)时 exact 与 stale 各存一份
   GB 级副本。内存翻倍,弃。
3. **【采纳】统一缓存,按 allow_stale 分派查找规则** —— 单缓存、每 nb 一个实例;exact 调用方
   按 `cur`(DB 版本)校验,stale 调用方按磁盘 manifest 版本校验;单飞加载。复用已在
   `_scale_index_version` 证明可用的 per-nb 锁表范式。

## 设计(三部分,均在 backend/app/services/sqlite_repository.py)

### Part 1 — stale 索引按磁盘身份缓存 + 单飞(核心修复)

改 `_scale_index(nb, allow_stale)`:

- **exact 命中不变**:`cached.manifest.version == cur` → 返回(覆盖两类调用方,字节不变)。
- exact 未命中 + `allow_stale=False`(viz/status 等 version-exact 调用方):**行为完全不变**——
  load,manifest==cur 才 cache 并返回,否则 None。
- exact 未命中 + `allow_stale=True`(检索调用方,热路径):
  1. 廉价读磁盘 `manifest.json` 的 version(几 KB,sub-ms);
  2. 若进程缓存里已有该 nb 的实例且其 `manifest.version == 磁盘 version` → **直接返回**
     (ANN handle 仍 memoize 在实例上,零重载);
  3. 否则在 **per-nb 单飞锁**内 double-check 后 `load_scale_index` 一次,写进缓存,返回。
     单飞防止 N 个并发推理查询各自加载 8GB(N× 内存尖峰)。

统一缓存交互:stale 调用方缓存了 stale 实例(version=磁盘版本 ≠ cur)后,exact 调用方
`cached.version == cur` 判否 → 落到自己的 load 分支返回 None(与今日「有 delta 时 exact 返
None」完全一致,不 regression;exact 分支不覆盖缓存,不 thrash)。

rebuild/fold 原子换目录 + 新 manifest 版本后:下一次 stale 调用读到新磁盘版本 → 缓存实例
版本失配 → 重载一次并重缓存;exact 调用此时 magic 版本 == cur → exact 命中。**自愈**。

新增基础设施:一个 `_scale_idx_load_locks`(per-nb 锁表,镜像 `_scale_ver_locks` 的
get-or-create + 全局锁只护锁表结构,绝不在全局锁内 load)。一个廉价 `_read_manifest_version(out_dir)`
helper(os.path.exists + open + json.load 只取 "version" 字段;缺失/损坏 → None)。

### Part 2 — 组合图缓存键在 delta 门控关时丢弃 churn 项

`_scale_combined_graph`([9455])的 version 元组([~9472])当前含
`active_ver = tuple(self._scale_index_version(notebook_id))`(churn)。当
`scale_search_include_delta=False` 时,组合图内容由 participants 的磁盘 manifest 版本
(已在 `base_ver`)完全决定,`_active_kg_delta` 返空、`splice_active` 空操作。

改:`active_ver` 仅在 `scale_search_include_delta=True` 时计入 version 元组;flag 关时用
一个稳定占位(如 `None`)。flag 已在元组里(PR#178),所以键仍能区分两态。效果:摄取期
flag 关的组合图缓存命中,不再每查询重建 113 万项 dict。

`_retrieve_chunks`(10402)的二次 `_scale_index(allow_stale=True)` 在 Part 1 后自动返回同一
缓存实例(chunk ANN handle 首开后 memoize),二次加载问题随之消解——无需单独改,但要测。

### Part 3 — `_active_kg_delta` 门控前的 55 批 COUNT 早退(次要)

`_active_kg_delta`([9283],门控在 9295)当前在 gate 前无条件 `_index_delta`(跑 48,739 源的
分批 COUNT,结果被丢)。把「读 manifest 判 indexed + flag 关」的廉价判断提到完整 COUNT 之前,
indexed 且 flag 关时提前 `return [], [], []`,省掉 55 次 COUNT。语义不变(gate 结果相同)。

## 正确性不变量(测试须锁死)

1. **stale-serve 与 flag 无关地正确**:ANN 核=磁盘已索引部分;flag=ON 时 delta 新鲜度来自
   `⊕delta` 暴力块,不来自缓存核。→ 测:flag=ON 时 delta 内容仍被检回(经暴力块),且核仍是
   缓存实例。
2. version-exact 调用方(`_scale_index()` 无 allow_stale:viz/status)行为字节不变——drift 时仍
   返 None。→ 测:有 delta 时 `_scale_index(nb)`(no allow_stale)返 None,不被本改动影响。
3. 摄取期恒定成本:连续多次 `_scale_index(nb, allow_stale=True)` 在磁盘索引不变时只
   `load_scale_index` 一次,`_open_scale_ann` 只 `hnswlib.load_index` 一次。→ 测:monkeypatch/spy
   计数 load_scale_index 与 handle open,多查询间调用一次。
4. rebuild/fold 后自愈:磁盘 manifest 版本变 → 下次 stale 调用重载一次并重缓存。→ 测:改磁盘
   manifest 版本后,stale 调用返回新实例。
5. 单飞:并发 cold stale 调用只加载一次。→ 测:barrier 并发 N 调用,load_scale_index 计数=1。
6. 小库/无索引(无 manifest)→ None,不变。
7. 组合图:flag 关时 version 键不含 active churn(摄取期 kg_mutation_seq 变而组合图缓存命中);
   flag 开时含 active_ver(delta 变→组合图重建)。→ 测:flag 关连续查 kg_mutation_seq 变化下
   `_scale_combined_graph` 只 `_load` 一次;flag 开时 delta 变触发重建。

## 净效果

摄取进行时,一次严格推理查询:从「每查询重载 ~10GB ANN + 重建 113 万节点组合图」→
「进程缓存命中,首次加载后 O(1),直到你真正重建索引」。delta 侧语义不变(默认不检索,
最终一致);version-exact 路径不变。

## 非目标(YAGNI)

- 不改 `_scale_index_version` / `_probe_scale_version_signal` 的 settings_tail(会让存量
  manifest 全失配变 stale,PR#178 已守此边界)。
- 不动 delta 门控四处(已完成)、rustworkx 回退守卫、关系检索(reasoning 不调用)、
  personalized_ppr(实测 <1s)、element 守卫。
- 不实现「摄取期后台自动重建让 delta 尽快可见」——那是 auto-fold(已存在)的事;本设计
  只保证「已索引部分恒定成本」。
