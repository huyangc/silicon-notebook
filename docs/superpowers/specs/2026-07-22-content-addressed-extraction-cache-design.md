# 内容寻址抽取缓存设计

日期：2026-07-22

## 问题

大规模使用时，内容完全相同的文件会被反复完整处理一遍。同一篇论文/规范被 N 个用户
上传，或同一批源被 `reparse`/`reextract` 重跑，每一次都重新付出全部 LLM 与 embedding
代价。

## 现状勘察

### 各阶段真实成本（本机事件日志，全部 `status=done` 的 pipeline 事件）

```
stage          n     median        p90        max      总计(s)
extract      307          2     154957     573264      19973.9   ← 占 pipeline 93.4%
embed        318          0       3346    1270023       2377.4   ← 见下方注
parse        319          1        109      48034        203.8   ← 占 pipeline  1.0%
pipeline     314          7     159233     675458      21396.2
```

在 275 个 `parse` 与 `extract` 均成功的配对样本上：`extract` 总耗时是 `parse` 的
**182 倍**，逐源比值中位数 2704x，74% 的源上 extract 更慢。即使取 parse 最慢的样本
（明显走了 MinerU，28.6s / 33.7s），extract 仍是 431s / 414s，仍有 12~15 倍差距。

> **embed 的 2377s 不可与其它阶段直接相加**：它在后台线程中与 extract 并发执行
> （`process_source` 里 `_embed_bg`），并非串行墙钟时间。缓存它省下的主要是
> **embedding API 配额与并发资源**，而非流水线时长。优先级据此排在 extract 之后。

原因是调用次数的量级差异：parse 每源一次，extract 是 **每窗口一次 × N 窗口 ×
gleaning 轮数**。文档越大差距越大。

> 设计过程中最初假设 parse（MinerU）最贵，被上述实测推翻。parse 缓存因此被移出范围。

### 已有资产

- `sources.file_hash` 已存 `sha256(文件字节)`，UI 上传路径已填充
  （`backend/app/services/source_ingestion.py`）。
- 唯一消费者是离线 `batch_ingest` 的 `already_ingested`，且是**同 notebook 内整源跳过**。
  UI 上传路径完全不查该哈希——两条路径行为不一致。
- `sources` 表**没有 `file_hash` 索引**，`source_id_by_hash` 是全表扫。
- `CacheBackend` Protocol（`get`/`put`）与 `NoCacheBackend` 已存在于
  `backend/app/core/llm_cache.py`——可插拔缓存的架构已就位。
- `LLMCache`（SQLite）已存在但**只挂在 `OpenAICompatibleClient` 上**，且**默认关闭**
  （`LLM_CACHE_ENABLED=false`），**无任何淘汰机制**。

### 关键发现：占 93% 的路径已在带缓存钩子的客户端上

> 本节曾错误断言"KG 抽取走 `KGClient`、未接缓存"。追调用链后更正如下。

产品路径的 KG 抽取客户端来自 `ModelProvider.kg_llm_client` →
`_llm_for_role("kg_llm")`，其构造的是 **`OpenAICompatibleClient`**
（`backend/app/services/model_provider.py`），即已挂载 `_get_cache()` 的那个客户端。
`backend/app/services/kg/client.py` 中的 `KGClient` / `make_client()` 在 backend 与
scripts 中**没有任何调用方**（产品代码只 import 了它的纯函数 `safe_json`）——它服务于
外部 gold generator。

`extract_window` / gleaning / refine 均以 `client.chat_json(messages, _KG_SCHEMA_HINT,
**cap_kwargs(...))` 调用，**不传 `bypass_cache`**。因此 KG 抽取天然经过缓存层，
`LLM_CACHE_ENABLED` 一开即生效，**无需任何装饰器**。

`bypass_cache=True` 目前仅用于 `kg/run_control.py` 探活与 `model_status.py` 健康
检查——这是正确的：健康探针必须绕过缓存，否则模型故障时缓存命中会造成假绿。

### 关键发现：抽取 prompt 是内容的纯函数

`_prompt(labeled_text, section_path, doc_type, base_filter)` 全文已核对，**不含**
source_id、文件名、时间戳或任何随机成分：

- `labeled = "\n".join(f"[{i}] {e.text}" ...)` —— 标号是**窗口内局部序号**
- `make_windows` 只依赖文本内容与章节结构
- `base_filter` 只是 bool（base / personal 两态）

因此：**同内容 + 同 doc_type + 同 tier → 逐字节相同的 prompt → 缓存必命中**。
`doc_type` 与 `base_filter` 参与 key 是语义正确的（不同文档类型、不同层级的抽取
重点本就应当不同），代价是相应场景不命中，接受。

## 决策

| 维度 | 决策 |
|---|---|
| 复用边界 | **全局跨用户共享**。内容上不泄密（复用方自己手里本就有这份文件）；唯一副作用是「秒完成」构成存在性侧信道，内网部署可接受。 |
| 复用层次 | 在**外部调用边界**缓存，产物仍各自落库。不共享行 → 删除、重解析、权限、归属完全不受影响。 |
| 范围 | KG 抽取缓存（93%）+ embedding 缓存（11%）。**parse 缓存不做**（1%）。 |
| 缓存组件 | **自研 `SqliteCacheBackend`**（~160 行），经已有 `CacheBackend` Protocol 接入。 |
| 部署形态 | 单机多用户、单后端进程 → SQLite 后端足够。 |
| 默认状态 | **默认开**（优化的目的），保留 env 逃生口。 |

### 为什么自研而非用 diskcache

初始判断是引入 `diskcache==5.6.3`（"别造轮子"），原型对比实测后**结论反转**。

**决定性证据：`cull_limit` 陷阱。** diskcache 每次触发裁剪时剔除固定
`cull_limit` 条，而非"剔除到刚好达标"。条目越大越致命——20KB 条目 × 默认
`cull_limit=10` = 200KB，一次就能清空整个缓存。同一场景（size_limit=200KB，
20KB 条目，交错访问保持 h0-h2 为热）：

```
cull_limit=10 (默认)   热条目存活 0/3   终态 2条/74KB   ← 浪费 63% 容量
cull_limit=1           热条目存活 3/3   终态 8条/197KB  ← 正确
cull_limit=100         热条目存活 0/3   终态 2条/74KB
```

**KG 抽取响应正是几十 KB 的大条目**，会真实命中此坑。且它不报错——缓存"正常
工作"，只是留存率与命中率莫名偏低，极难诊断。

**性能对比**（Python 3.13.11，8 线程并发，150×20KB）：

```
自研 SqliteCache   写 4734/s   读 11514/s   留存 48/150   960KB/1000KB
diskcache          写 2263/s   读  5179/s   留存 40/150   852KB/1000KB
```

综合：我们只需要 get / put / TTL / 容量上限 / 按 tag 清空这 5 个能力，是 diskcache
功能的很小子集；自研约 160 行，性能约 2 倍，大条目下淘汰语义正确，且消除了
"依赖 12 个月无发布"的长期风险。

**风险与缓解**：自研原型第一版确实带一个真缺陷——`_evict_if_needed` 里
`ORDER BY used_at ASC LIMIT 64` 无条件删除整批，当候选批大于剩余条目数时一次清空
缓存、连带淘汰热条目。它**被测试当场捕获**，修复约 12 行（改为按 size 累加、删到
刚好达标）。这印证该类代码可测、风险可控，但也说明**下列测试是交付的必要条件而非
可选项**。

**diskcache 保留作为开发期的差分测试 oracle，但仅限 KV 语义**（put/get/覆盖/落空），
**不可用于校验淘汰行为**——两者的容量判据量纲不同，详见下文"测试"一节。经
`pytest.importorskip` 引入，不进 `requirements.txt`。

## 架构

```
OpenAICompatibleClient.chat_json   # 已有缓存钩子 → KG 抽取/ask 天然覆盖，零改动
make_embedder() → CachedEmbedder(inner, backend)   # 新增，覆盖全部 6 个 embed_texts 调用点
                          ↓
                  CacheBackend (Protocol，仅 get/put)
                          ↓
                  SqliteCacheBackend (~160 行)
```

LLM 侧无需任何装饰器——`extract_graph` / `extract_window` / refine / gleaning 均已
走在带缓存的客户端上。embed 侧用装饰器，6 个 `embed_texts` 调用点**零改动**。

`OpenAICompatibleClient._get_cache()` 内联的开关判断与路径解析一并**删除**，改为向
`make_cache_backend(settings)` 索取（ask 路径同样受益，全系统只有一套缓存机制、
一个构造点）。旧的 `llm_cache.db` 表结构不兼容（缺 tag/size/used_at 列），不迁移、
直接弃用——缓存冷启动重建即可。

### SqliteCacheBackend 要点

- 表 `cache(key PK, value, tag, size, created_at, used_at)` + `idx(used_at)` +
  `idx(tag)`；`meta.total_bytes` 增量维护，避免每次裁剪 `SUM()` 全表扫，另备
  `recount()` 修正进程崩溃导致的漂移。
- **粗粒度 LRU**：命中时仅当距上次刷新超过 `refresh_window`（默认 1h）才写
  `used_at`。cache hit 是热路径，逐次 `UPDATE` 会把"读"变成"写"，在高命中率场景
  （reparse 重跑）下写放大可观。牺牲少量淘汰精度换掉绝大部分写。

  > **⚠ 已知权衡：生产配置下没有热条目保护。** `refresh_window` 默认 3600s，而
  > `make_cache_backend()` 刻意不暴露它——**生产上它恒为 3600**。一小时窗口内的命中
  > 不刷新 `used_at`，于是 `used_at ≈ 写入时间`，淘汰**退化为近似 FIFO**：反复被
  > 访问的条目并不会因此延寿。同场景只改这一个参数的实测：
  >
  > ```
  > refresh_window=0        热条目存活 3/3
  > refresh_window=3600.0   热条目存活 0/3     ← 生产默认值
  > ```
  >
  > `0/3` 正是上文用来否决 diskcache 的那个数字。**刻意不改默认值**：调小
  > `refresh_window` 能换回热条目保护，代价是把 cache hit 这条最热的路径重新变成
  > 写——正是本节要消除的写放大。缓存条目丢了只是多打一次后端（可恢复），写放大
  > 却是持续成本。诚实记录优于加复杂度。
  >
  > 测试如实锁定这个行为：`test_hot_entries_are_not_protected_at_the_production_refresh_window`
  > 跑生产默认值并断言近似 FIFO；`test_hot_entries_survive_eviction_with_fine_grained_lru`
  > 用 `refresh_window=0`（生产到不了的配置）单独锁 LRU 机器本身的正确性。两者
  > 分工是"生产行为"与"机器正确性"，缺一会把"刻意的权衡"与"淘汰逻辑写错了"混为一谈。
- **裁剪必须删到刚好达标**（按 size 累加选取受害者），不可无条件删除整批——这正是
  diskcache 的 `cull_limit` 陷阱，也是自研原型第一版的缺陷所在。
- 裁剪先清过期条目（免费空间），不足再按 `used_at` 升序淘汰；裁到
  `size_limit × 0.9` 留 headroom，避免每次 put 都触发。
- `PRAGMA synchronous = NORMAL`——缓存丢失可重建，不值得为它付 fsync。

### 配置

沿用现有 `LLM_CACHE_ENABLED` 作为**总开关**（避免引入第二个开关概念），默认值由
`false` 改为 **`true`**；`LLM_CACHE_PATH` 继续指向 SQLite 文件（换新文件名）。新增
`LLM_CACHE_SIZE_LIMIT`（字节）与 `LLM_CACHE_TTL_DAYS`（默认 90）。按项目既有约定，
所有新配置项的环境变量映射必须用 `validation_alias`——pydantic-settings v2 下
`Field(env=)` 静默失效。

### 缓存键

| | key | 依据 |
|---|---|---|
| KG | `sha256(base_url + model + messages全文 + schema_hint + 有效 temperature/top_p/max_tokens)` | prompt 已核对为内容的纯函数；base_url 见下"服务身份"；生成参数见下"生成参数" |
| embed | `sha256(base_url + model + 单条 text[:embed_truncate_chars])`，**per-text 而非 per-batch** | 见下三条（embedding 无采样/token 参数，不受生成参数影响） |

**服务身份（base_url）必须进 key（已解决）**：本仓库有 per-user 模型配置，不同
用户/角色可配不同 `base_url` 指向不同 endpoint，而缓存是**跨用户全局共享**的。若
key 只认 model 名字符串，两个用户配了同一个 model 名却不同 `base_url` 时会共用同
一条缓存——第二个用户拿到第一个 endpoint 的响应，他自己选的模型服务根本没被调用。
缓存默认开启，这是活跃的正确性问题，因此 `llm_key`/`embed_key` 各把 `base_url`
纳入 sha256 材料。

⚠ **只取 `base_url`，绝不放 `api_key`**：key 会经 `stats()`（现回显 tag/计数）、
日志、可能的调试路径间接暴露，任何秘密都不能进 key 材料。`base_url` 足以区分
endpoint（正是上面的场景），且它本就不是秘密（出现在日志、错误信息里）；换
`api_key` 通常伴随换 endpoint，即便同 endpoint 只轮换 key，也有 `evict_tag` 逃生口
兜底。所以刻意**不**复用含 api_key 的 `model_config_fingerprint()`。⚠ `base_url`
只进 **key**、不进 **tag**：tag 保持为 model 名，服务于 `evict_tag(model)` 按模型
清空的语义，混入 base_url 会破坏它。

（此改动让此前**全部**缓存条目 key 变化 = 一次性全冷。缓存尚未上线，冷启动重建
即可，无迁移成本。）

**生成参数（temperature / top_p / max_tokens）必须进 key（已并入）**：`chat_json` 的
这三个参数是 per-call 的（KG 抽取用高 token 预算、答案合成用另一档）。相同 messages
+ schema 但不同采样设置、或不同 token 预算是**语义不同的 upstream 请求**，绝不能共用
一条缓存。`max_tokens` 尤其关键——本仓库文档化的截断补救手段就是调大
`KG_EXTRACT_MAX_TOKENS` 重跑，若它不在 key 里，调大后仍会命中被截断的旧响应。这与
「截断响应不入缓存」（`is_cacheable_llm_response`）互为**两半**：那条防坏响应被写入，
这条防参数变了还命中旧响应。

⚠ **key 用解析后的有效值，而非原始入参**：`max_tokens=None` 在 `chat_json` 里回落到
`settings.openai_compat_max_tokens`，因此 `llm_key` 收到的必须是**解析后**的有效预算
（`chat_json` 在缓存查询前先算出 `effective_max_tokens` 再传入）。否则 `max_tokens=None`
与显式传入等价默认值会算出两条不同 key = 虚假 miss，白白多打一次模型。`temperature`
/ `top_p` 无 None 回落，直接用入参即可。（此前设计与 `llm_key` 文档串曾把 temperature
记作「固定常量，刻意排除；若将来加了 per-call temperature 必须并入 key」——现在正是
那个"将来"，已并入，并一并补上 top_p 与 max_tokens。）

embed key 的另两个必须点：

- **per-text 而非 per-batch**：否则批次边界一变（`embed_batch_size` 调整、或上游
  chunk 数量变化）就全部 miss。
- **必须对截断后的文本取哈希**：`DashscopeEmbedder.embed_texts` 内部先执行
  `t[:embed_truncate_chars]`（默认 2000）才发给 API。若用原文取哈希，两个前 2000
  字符相同的长文本会各占一条缓存却拿到完全相同的向量——白白损失命中率。对截断后
  文本取哈希同时自动捕获了 `embed_truncate_chars` 配置变更的影响。

embed 缓存**存储 API 原始维度的输出**。4096→1024 的运行时维度截断发生在消费侧
（原向量作为真相源保留不改写），因此缓存层与维度决策相互独立，不受其影响。

副产品：**prompt 一改，缓存自动全冷**。不需要维护版本号，正确性天然成立——这是
调用级缓存优于「抽取产物缓存」的关键之处。

### 注入点

- **LLM 侧：无需注入。** `OpenAICompatibleClient._get_cache()` 已是挂载点，改造它
  的构造来源即可（见上文"唯一构造点"）。
- **embed 侧：`make_embedder()` 工厂**（`backend/app/services/embedding.py`）。包装
  须置于 `bind_model_status_identity` 之内层，不得破坏 model_status 身份绑定与
  model_error 上报通道。

### 健康探针必须绕过缓存

`model_status.py` 用 `make_embedder(...)` 构造探针做健康检查。若工厂无条件包缓存，
**模型服务故障时探针会命中缓存而显示假绿**。必须对称于 LLM 侧既有的
`bypass_cache=True` 机制：`make_embedder` 增加 `cache: bool = True` 形参，
`model_status.py` 与 `kg/run_control.py` 同类探活路径显式传 `cache=False`。
此项须有专门测试。

## 缓存模块的内聚与可替换性

这是一条**硬性要求**：缓存必须高内聚，将来出现合适组件时能低成本替换。

### 现状问题

缓存代码目前散在 5 个文件（`core/llm_cache.py`、`core/llm.py`、`core/config.py`、
`services/model_provider.py`、`services/sqlite_repository.py`）。最严重的是
`OpenAICompatibleClient._get_cache()` 内联了**开关名、配置项名、相对路径的仓库根锚定
规则、具体实现类名**——这些都是缓存的内部事务，却由客户端持有。本设计新增两个消费者
（`CachedEmbedder` 与运维查询接口），若照此模式就会出现多份重复构造逻辑，换组件需
逐处修改。

### 模块结构

```
app/core/cache/
  __init__.py        # 唯一公开面: make_cache_backend() / CacheBackend / CacheAdmin
  backend.py         # Protocol 定义 + NoCacheBackend
  sqlite_backend.py  # 自研实现（全模块唯一写 SQL 处）
  policy.py          # key 计算 + 可缓存性判定
```

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。

### 三条规则

**1. 唯一构造点。** `make_cache_backend(settings)` 是缓存的唯一诞生处，负责读开关、
解析路径、选实现。消费者不自行 new、不解析路径、不读配置。换组件 = 改工厂一行。

**2. Protocol 分两层。** 这是可替换性的关键：

| 接口 | 方法 | 要求 |
|---|---|---|
| `CacheBackend` | `get(key) -> str \| None`、`put(key, value, tag="")` | **必需，仅 2 个** |
| `CacheAdmin` | `evict_tag(tag)`、`stats()`、`clear()` | 可选，`isinstance` 探测 |

`tag` 是可选参数，不支持的实现忽略即可——降级为"无法按 model 清空"，**不影响
正确性**。若把 `evict_tag`/`stats` 并入必需接口，将来换任何简单 KV 组件都得先补齐
管理方法，可替换性即告失效。管理端点在后端不支持 `CacheAdmin` 时如实降级提示。

**3. 策略与存储分离。** key 计算（`llm_key` / `embed_key`）与"什么不该缓存"
（空响应保护）属策略，留在 `policy.py`；backend 只负责存取字节。换存储不碰策略。

### 把要求变成可执行约束

- **导入守卫**：除 `app/core/cache/` 内部外，任何文件不得 import 具体实现类，只能
  import Protocol 与工厂。须做变异验证——在别处插入
  `from app.core.cache.sqlite_backend import SqliteCacheBackend`，守卫必须转红。
- **Protocol 契约测试套件**：参数化 fixture 覆盖全部 backend，测的是接口行为而非
  实现细节。这是可替换性的另一半——接口能插上不等于行为正确。新组件接入时跑通这套
  即可切换。

### 验收标准

替换为任意组件 = 新增 1 个文件实现 2 个方法 + 改工厂 1 行 + 契约测试通过，
**零调用方改动**。

### 已验证：Redis 替换路径

原型实测（同一套契约测试跑三个后端）：

```
noop     PASS
sqlite   PASS
redis    PASS          ← 只实现 CacheBackend 的两个方法

redis 实现 CacheAdmin: False   ← 刻意不实现，不影响使用
redis 仍满足 CacheBackend: True
```

**Redis 后端反而比 SQLite 后端简单**（约 30 行 vs 160 行），因为两件最麻烦的事都能
交给服务端：TTL 用 `SET ... EX` 由 Redis 自动过期；容量与 LRU 用 `maxmemory` +
`maxmemory-policy allkeys-lru` 配置，**零代码**。

替换成本：新增 `redis_backend.py` + 工厂加一个分支与 `LLM_CACHE_BACKEND` 配置项 +
`redis` 进 requirements + 契约测试登记一行。调用方零改动。

两个实现注意点：

- **redis-py 默认返回 `bytes`**，而 Protocol 要求 `Optional[str]`——后端内部必须
  decode（或以 `decode_responses=True` 建连接）。
- **值的编码边界**：`CachedEmbedder` 将向量 JSON 编码为字符串（1024 维约 20KB 文本）。
  Redis 存字符串可行但内存效率不高。若将来要改为二进制编码，Protocol 的 `str` 需变为
  `bytes`，那是**破坏性变更**，所有后端都要跟着改。现在不预防（YAGNI），但边界记在此。

`runtime_checkable` Protocol 的 `isinstance` **只检查方法名存在与否，不检查签名**——
这是 Python 的已知限制。因此契约测试套件不是可选项，它才是行为正确性的真正保障。
契约套件中固定包含一个只实现两个必需方法的 `MinimalBackend` 参数项，作为可替换性的
活体标尺：一旦有人把 `stats`/`evict_tag` 变成事实上的必需方法，它会立刻转红。

## 三个安全阀

### 1. 缓存 opt-in + 只缓存真正可用的响应（已实现，重构中必须保持）

**缓存默认不缓存——调用方传 `response_validator` 才缓存（opt-in）。** 这是 Codex 第 6 轮
后用户拍板的方向：`response_validator` 从「第四道门」升级为**缓存开关**。理由是逐个调用方
补 validator 一直在漏（第 4/5/6 轮同一投毒主题逐步放大）：`chat_json` 的绝大多数调用方
（Ask、paper_meta、summary、schema 归纳、query rewrite…）**不传** validator，它们偶发拿到
`[]`/error 形状的响应也会被缓存 90 天，Ask 重试复用坏值、reparse 一直中毒。翻成 opt-in 一次
关掉整个类：**不传 validator 就既不写也不读缓存**（对调用方透明，正确性保留，只失去性能）。
占 93% 成本的 KG 抽取三处（`extract_window`/gleaning/refine，见 `services/kg/extract.py`）
本就传 validator（`_kg_fragment_cacheable`/`_refine_response_cacheable`），保持缓存；其余
不传的调用方失去缓存是**预期的、安全优先的取舍**（它们要么低频、要么响应因子每次都变、
要么 best-effort 优雅降级，缓存收益有限）。

`response_validator` 是**两道门共用**的开关（`backend/app/core/llm.py`）：

- **命中门**：`if response_validator is not None:` 才 `cache.get` 并 serve；且命中值必须仍过
  该 validator（validator 不进 key，同一 key 上别的调用方写的值会在这里被重判，拒绝＝当
  miss、落到真实调用，其新响应由写入门再判）。
- **写入门**：`if cache is not None and ckey and response_validator is not None and
  is_cacheable_llm_response(content, finish_reason) and
  _response_validator_allows(response_validator, content)`。

写入门除了 opt-in 开关，仍保留三道**可用性**门（对 validator-bearing 调用方生效）：

1. **非空回退 `"{}"`**、**`json.loads` 解析不了**、**`finish_reason == "length"`**——
   由 `is_cacheable_llm_response`（`core/cache/policy.py`）承担，schema-agnostic 的通用
   可用性判断。输出预算烧光时 `chat_json` 落到 `"{}"` 回退（即
   `reasoning-empty-content-degeneration` 记录的退化），缓存一次偶发退化等于永久固化整个 TTL。
2. **调用方 schema 形状（validator 本身）**——前三道拦不住「语法合法但违反调用方 schema」
   的响应：KG 抽取拿到 `{"nodes":"invalid"}`（`nodes` 该是 list 却是 string）能过 `json.loads`，
   下游 `safe_json` + 抽取静默产出 **0 对象**，缓存 90 天后每次重解析都命中这个 0。validator
   **复用抽取自己的 `safe_json` + 形状判断**（`nodes`/`edges`/`items` 存在时必须是 list，缺省
   算合法空窗——别写太严把好响应也挡了）。validator 抛异常＝保守地不缓存
   （`_response_validator_allows` try/except 兜底），**缓存故障永不炸主流程**。

`bypass_cache=True`（健康探针）路径不变：整段读/写都跳过。

两道 opt-in 门 + 三道可用性门必须**逐门**变异验证（逐一削弱后对应守卫测试必须转红）：
cache/ckey 对每个非 bypass 调用都算出来，所以命中门与写入门的 `response_validator is not None`
可**各自单独**被变异抓红（守卫在 `tests/test_cache_response_validator.py`；is_cacheable 与
缓存键各门在 `tests/test_cache_factory.py`、`tests/test_llm_cache.py`）。

embed 侧对称要求：空向量列表、长度与输入不符的响应，一律不写入缓存。（embed 缓存不在本次
opt-in 范围内——它另有强不变式护栏，且 embedding 无「违反调用方 schema」这一类退化。）

### 2. 测试隔离

已有实测教训：带真 `.env` 跑后端全量会被 `llm_cache.db` 污染出大规模假失败。
conftest 必须强制 `NoCacheBackend`（或临时目录），且该隔离本身要有测试。

### 3. 装饰器属性透传

`CachedEmbedder` 须以 `__getattr__` 兜底透传被包装对象的全部属性（`dim`、
`embed_query`、model_status 身份等）。LLM 侧的 `LimitedJsonChatClient` 已是此形态
（`__getattr__` + `chat_json(*args, **kwargs)`），可作参照。

## 生命周期

代码侧的所有变更**都不需要 TTL**，key 自带版本语义：

| 变化 | key 捕获 | 结果 |
|---|---|---|
| 改 prompt | 是（prompt 全文在 key 里） | 自动全冷 |
| 改窗口参数 `n`/`m` | 是（labeled_text 变） | 自动失效 |
| 改 `should_extract_window` | 是（窗口集合变） | 自动失效 |
| 换模型（改 model 名） | 是 | 自动失效 |
| 换 endpoint（同 model 名、不同 base_url） | 是（base_url 在 key 里） | 自动失效 |
| **同一 endpoint 上换掉同名模型的权重** | **否** | ← TTL 存在的理由之一 |

TTL 只为两件事存在：

1. **同一 endpoint 上同名模型权重被替换** —— key 里的 `model` 只是字符串，
   `base_url` 也没变（换权重不换地址）。跨 endpoint 的同名模型已由 base_url 区分。
2. **删除后残留** —— 缓存 value 是 KG 抽取结果，而 prompt 明确要求
   「Preserve entity/concept names EXACTLY as they appear」，**原文片段留在缓存里**。
   用户删掉 notebook 后，其内容的衍生片段仍在全局缓存中。决策：**TTL 封顶即可**，
   不做定向清理——缓存 key 是 sha256 无法反查，且定向清理会连带删掉他人仍在使用的
   共享条目（那人的文件还在，逻辑上也说不通）。

三层机制：

- **容量驱动（主力）**：`size_limit` + LRU。99% 的回收由它完成。⚠ 注意"热条目自然
  留存"只在 `refresh_window` 足够小时成立，而生产默认 3600s 下它**不成立**（退化为
  近似 FIFO），见上文粗粒度 LRU 一节的实测与取舍说明。
- **TTL（兜底）**：默认 **90 天**，env 可配。必须**长**——短 TTL 会毁掉最大的收益
  场景：`reparse`/`reextract` 那种几万源重跑可能几个月后才发生，届时命中率接近 100%。
- **显式失效（逃生口）**：`evict_tag(model)` 按 model 名清空——换模型服务时这才是
  正确操作，而非等 TTL 到期。写入时以 model 名作 tag。另加全清。

## 可观测

无埋点则无法证明缓存在工作。

- 命中 / 未命中计数进事件日志；现有 `stage` 事件（parse/embed/extract）附
  `cache_hits` / `cache_misses`。
- 一个「缓存现状」查询：总大小、命中率、按 model 分布——否则无从判断何时该清。

## 顺带修复

- `sources` 加 `file_hash` 索引（现为全表扫）。
- UI 上传做**同 notebook 内**去重，对齐 `batch_ingest` 的既有行为，消除两条路径的
  不一致。跨 notebook 不做整源去重——用户通常确实想在自己库里拥有这份文件，且跨用户
  共享 source 行会引爆权限、删除级联与归属问题。

## 已知边界

### 搁浅在 `parsed` 的源，换后缀重传收不到重解析（Codex 第 6 轮 P2-1，未修，待定夺）

换后缀重传的重解析守卫是 `parse_status in ('failed','extracted')`（settled 集）。源
可以停在 **`parsed`** 且**没有任何流水线在跑**，此时换后缀重传走在飞分支（只改名、不
调度），而收口它的 `_reconcile_pending_suffix` 要靠一条正在跑的 `process_source` 完成来
触发——根本没在跑，于是旧解析器的 elements 永远留着，后缀纠正悬空到下一次手动重解析
或 notebook KG build（build 从 stale elements 抽取，不重解析）。

制造「搁浅 `parsed`」的三个源头（均由设计有意为之，`parsed`＝解析已成、抽取待跑，是
这些源**正确**的休止态）：

1. 启动崩溃恢复把 `extracting` 回退成 `parsed`（`migrations.py::_recover_interrupted_jobs`）。
2. notebook KG build 单源**失败**回 `parsed`（`knowledge_lifecycle.py::_extract_one` 的
   通用 except）。
3. notebook KG build **取消**回 `parsed`（同上，`KgBuildAborted` 分支）。

**为什么不无脑把 `parsed` 加进 settled 集**：`parsed` 同时是**流水线中途的瞬态**——
`process_source` 在 `set 'parsed'` 与随后**无条件** `set 'extracting'` 之间会短暂停在
`parsed`。此时被并发 reparse 认领（`parsed→queued`），紧接着那条流水线的
`set 'extracting'` 会把认领覆盖回去 → 同一源两条流水线互清 elements/KG。这正是本特性
前几轮辛苦消除的 mid-pipeline 竞态，绝不能重新引入。

**评估过的三条修法**：

- **(a) 让三个源头落到明确 settled 的态（如 `failed`）**：语义上错。`parsed` 的源解析
  **成功**（有 elements），只是抽取被打断；标 `failed` 是谎报解析失败，且会让它掉出
  KG-build 的 `parse_status IN ('parsed','extracting','extracted')` 目标集（「继续分析
  未完成内容」的恢复语义断掉），换后缀重传还会按失败源走**整条重解析**而非只重抽——
  破坏面大，否决。
- **(b) 把「流水线是否在跑」和 parse_status 解耦**：最干净是加一个 in-flight 标记（需
  schema 迁移，用户此轮倾向不加）；轻量变体是把 `process_source` 的 `parsed→extracting`
  也改成 WHERE 守卫的原子认领——输了（被 reparse 抢走）就中止本条流水线、让给
  reparser。这**能**消除双流水线竞态（与现有四处 rowcount 认领同构、不需新列），但它
  改的是**热路径**，且中止的那条流水线已经起了后台 embed 线程（在旧 elements 上跑），
  与 reparser 的 clear_source_extraction_state 存在 embed-vs-clear 的收尾竞态（该暴露面
  在现有 `failed` 重试路径上其实已存在、被容忍，但 `parsed` 更早、更易触发）。改热路径
  + 收尾竞态判断，风险不小。
- **(c) 如实标注为已知边界，交由用户定夺**（本轮采纳）。

**当前影响面（窄）**：需要（崩溃重启 **或** KG build 失败/取消把源留在 `parsed`）**且**
在任何 KG build 重新处理它之前，用户用**不同解析器后缀**重传同一内容。后果是旧解析器的
elements 保留到一次手动重解析为止（`parse_source` 公有入口会按新 file_name 重解析）。
正确性优先于覆盖：本轮**不引入新竞态**，把它留作已知边界。若要修，推荐 (b) 的守卫认领
变体，但因触及热路径 + embed 收尾语义，需用户显式点头后单独一轮做。

## 测试

- **key 稳定性**：同内容、不同 source_id / 文件名 → 同 key；不同 doc_type / tier → 不同 key。
- **空响应不入缓存**：LLM 侧锁定 `llm.py` 既有的 `content != "{}"` 条件（变异验证：
  删掉该条件后测试必须转红）；embed 侧覆盖空向量列表与长度不符两种情形。
- **健康探针不吃缓存**：`make_embedder(..., cache=False)` 返回的对象不得命中缓存；
  变异验证——把 `cache=False` 改回 `True` 后测试必须转红。
- **embed 批量部分命中**：N 条命中 K 条时，只对 miss 的 N−K 条调用后端，且返回
  **顺序与长度严格对齐**。错配是静默灾难（向量张冠李戴，检索层看不出来），必须
  用「命中项与未命中项交错」的用例覆盖。重组时 miss 子集仍须遵守 dashscope 的
  `_BATCH = 10` 硬上限（重新分批，而非把原批次原样透传）。
- **重复文本**：同一批内出现两条相同文本时，只应产生一次后端调用，且两个位置都拿到
  向量——这是 per-text key 的直接推论，容易在重组逻辑里写错。
- **淘汰**（自研缓存的核心风险面，逐项必测）：
  - 超过 `size_limit` 时裁剪生效，且**裁剪后容量利用率仍应接近上限**——"缓存被清得
    只剩零星几条"是 `cull_limit` 类缺陷的特征，只断言"没超限"抓不到它。
  - **热条目**：注意测试本身要写对——LRU 看的是最后访问时间而非访问次数。有效用例
    须在灌入冷数据的**过程中交错访问**热条目，否则热条目的 `used_at` 仍旧早于所有
    冷条目，被淘汰是正确行为（此坑在原型阶段实际绊倒过一次）。
    ⚠ 更要紧的是：这条用例**必须跑生产默认的 `refresh_window=3600`** 并断言真实
    行为（近似 FIFO，热条目 0/3）。早期版本用 `refresh_window=0` 断言"存活 3/3"，
    而那个配置生产上根本到不了——测试因此在描述一个不存在的系统。细粒度 LRU 的
    正确性另用一条明确标注"生产到不了"的测试单独覆盖。
  - **候选批大于剩余条目数**时不得清空缓存（原型第一版的真实缺陷）。
  - **差分测试（限 KV 语义，不含淘汰）**：与 `diskcache` 逐步比对 put/get/覆盖/落空
    判定，分歧数必须为 0，但**必须把 `size_limit` 设到不触发淘汰**。
    实测教训：把淘汰纳入对比不成立——diskcache 的 `volume()` 计的是 SQLite 文件物理
    页数（含 schema/WAL/freelist），本实现计的是逻辑内容字节，量纲不同；且 diskcache
    默认 `disk_min_file_size=32KB`，小于该值的条目走内联存储、`size` 记账为 0，其
    LRU-by-size 判据根本不被触发。带淘汰对比会得到 23~37 处分歧且随 seed 漂移
    （42→37、7→29、99→23），仅比 KV 语义则 4 个 seed 全部归零。
    淘汰行为由自研实现自己的测试独立覆盖——我们本就不认同 diskcache 的裁剪语义，
    它不该充当淘汰的标准答案。
  - 覆盖写时 `total_bytes` 按差值更新；`recount()` 与增量计量一致。
- **测试隔离**：确认测试环境拿到的是 `NoCacheBackend`。
- **属性透传**：`control` 经装饰器后仍可达，取消语义不变。
- **Protocol 契约套件**：参数化 fixture 覆盖全部 backend（`sqlite` / `noop` / 未来
  新增者），只测接口行为，不碰实现细节——put 后 get 得回原值、缺失 key 返回 None、
  覆盖写生效、`tag` 参数被接受（即便被忽略）。新组件接入时跑通即可切换。
- **内聚导入守卫**：除 `app/core/cache/` 内部外，无文件 import 具体实现类。须变异
  验证：在别处插入 `from app.core.cache.sqlite_backend import SqliteCacheBackend`，
  守卫必须转红。

## 不做

- **parse 缓存**——仅占 1%，实测推翻了最初假设。
- **ask 路径专门优化**——其 prompt 含检索上下文与对话历史，命中率极低，且会导致
  「同一问题永远同一答案」的语义问题。ask 走通用 `LLMCache` 切换即可，不特殊处理。
- **跨 notebook 整源去重**——见上文。
- **多机共享缓存**——部署形态确定为单机多用户单进程。`CacheBackend` Protocol 已为
  将来换 Redis 留好位置。
