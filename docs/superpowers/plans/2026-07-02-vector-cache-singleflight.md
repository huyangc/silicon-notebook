# VectorCache single-flight + 容量上限 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景(承 PR#152 真机事故的遗留放大器)**:`backend/app/services/vector_cache.py` 的 `VectorCache` 是进程级大件缓存(2GB 向量矩阵 `{nb}:matrix:*`、全库分词 `{nb}:kwtok`、rustworkx 图 `{nb}:rxgraph`/`{active}:fed_rxgraph`、PPR/组合图等)。两个内存放大器:
1. **无 single-flight**:K 个并发请求同 key 首查 → `loader()` 被并行跑 K 次(每次可能分钟级+GB 级),瞬时峰值 ×K,Python RSS 钉在峰值——真机 64GB 打满的直接推手之一(550s×3 并发 `/neighbors` 实录)。
2. **无容量上限**:条目只增不减(版本变才原位替换);`fed_rxgraph` 按发起提问的 active notebook 复制,多用户线性增长。

**Goal**:同 key 并发 miss 只构建一次;LRU 上限防无界增长。行为对调用方透明(get/invalidate 签名不变、`_store` 属性保留 dict 语义——`_invalidate_unified_cache` 直接迭代 `_store` 找 `:fed_rxgraph` 后缀)。

**Tech Stack**:纯 Python threading,pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,测试在 worktree `backend/` 跑。

## Global Constraints
- **签名/语义兼容**:`get(key, version, loader)`、`invalidate(key)` 不变;`_store` 仍是 key→(version, value) 的 dict-like(用 `OrderedDict`,是 dict 子类,既有 `for k in self._vector_cache._store` 迭代照常工作)。
- **single-flight 语义**:per-key 锁 + double-check——拿到 key 锁后重查 store(版本命中直接返回);loader 异常必须释放锁并**传播**(不缓存失败结果),等待方各自重试自己的 loader(简单正确优先)。全局锁只保护锁表/store 的结构操作,**绝不在全局锁内跑 loader**。
- **LRU**:`max_entries` 构造参数(默认 32);`get` 命中 `move_to_end`;插入后超限 `popitem(last=False)` 淘汰最旧。淘汰只影响性能不影响正确性(下次重载)。
- **配置**:config.py 加 `vector_cache_max_entries: int = Field(32, validation_alias="VECTOR_CACHE_MAX_ENTRIES")`(pydantic-settings v2 必须 validation_alias);`sqlite_repository.py:301` 改 `VectorCache(max_entries=self.settings.vector_cache_max_entries)`(类默认值保留,便于裸用)。
- **锁表不泄漏**:per-key 锁条目在无人等待时可回收(引用计数或加载完成后 pop——实现取简单正确者,报告里说明选择)。
- 不动 viz/scale 索引缓存(`_scale_idx_cache`/`_viz_idx_cache`,另一套机制)。

---

## Task 1: single-flight + LRU(vector_cache.py)+ 配置接线

**Files:** `backend/app/services/vector_cache.py`、`backend/app/core/config.py`、`backend/app/services/sqlite_repository.py`(仅 L301 一行);Test 新建 `backend/tests/test_vector_cache.py`。

- [ ] Step 1 写测试(先红):
  - `test_single_flight_concurrent_miss`:慢 loader(threading.Event 门控)+ N=8 线程并发 `get` 同 key 同 version → loader 调用计数 == 1,8 个返回是同一对象。
  - `test_loader_exception_propagates_and_not_cached`:loader 抛异常 → get 抛;随后成功的 get 正常缓存;并发等待方不会拿到异常结果卡死(各自重试)。
  - `test_lru_eviction`:max_entries=3,插入 4 个 key → 最旧被淘汰(其 loader 再次被调);get 命中刷新新鲜度(命中后它不是下一个被淘汰的)。
  - `test_version_replace_in_place`:同 key 版本变 → 替换不新增条目(len(_store) 不变)。
  - `test_invalidate_under_concurrency`:invalidate 后 get 重载,无死锁。
  - `test_store_iteration_compat`:`_store` 可按 dict 迭代且 key 顺序含义不影响 endswith 过滤(兼容 `_invalidate_unified_cache` 用法)。
- [ ] Step 2 实现(OrderedDict + 全局 `threading.Lock` + per-key `threading.Lock` 表,double-checked;LRU 逻辑如上)。
- [ ] Step 3 config + repository L301 接线。
- [ ] Step 4 回归:`pytest tests/test_vector_cache.py -q` 新测试绿;全量 `pytest tests/ -q`(基线 **1433 passed, 1 skipped**)。
- [ ] Step 5 提交 `perf(cache): VectorCache single-flight(并发首查只构建一次)+ LRU 上限(VECTOR_CACHE_MAX_ENTRIES)`。

## Self-Review 要点
- 全局锁内绝无 loader 调用(否则所有缓存互相串行=更糟)。
- 等待方拿锁后 double-check 命中就返回,不重复 load。
- 淘汰的是 (version, value) 元组整体;невalidate/淘汰与 in-flight load 交错不死锁(锁次序:先全局后 per-key,或拿 per-key 时不持全局)。
- 32 条上限对典型规模(几个库×~6 类 key)足够;fed_rxgraph 多用户场景被硬性封顶。

## 收尾
- rebase origin/master → push → `gh pr create --base master`。
