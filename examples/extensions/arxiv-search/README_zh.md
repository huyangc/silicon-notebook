# arXiv 文献检索 —— 样板部署插件

[English](./README.md)

这是本仓库第一个**出厂关闭**的样板部署插件：它是「干净 checkout + 只改配置 = 生效，
零补丁」这句话的可运行证明，也是
[`docs/deployment-extensions-sop_zh.md`](../../../docs/deployment-extensions-sop_zh.md)
（部署插件 SOP）的配套范例。任何未改动的 checkout 里它都是关闭的。启用与否完全是
部署方自己的决定：把 `EXTENSIONS_CONFIG` 指向一份点名它的 TOML 文件，再把
`SILICON_NOTEBOOK_UI_PLUGINS` 指向它的前端插件包。

本文档面向**启用它的运维方**，不是面向要在它基础上继续开发的人——那类需求请读源码里的
注释和 SOP 本身。

## 一、三步启用

1. **把 Python 包装进后端解释器的环境。** 可以对 `PYTHON_BIN` 指向的环境执行
   `pip install -e examples/extensions/arxiv-search`，也可以把它的 `src/` 目录直接
   放上 `PYTHONPATH`。包名是 `silicon-notebook-arxiv-search`，可导入的模块名是
   `silicon_notebook_arxiv_search`。
2. **把 [`extensions.example.toml`](./extensions.example.toml) 复制到 checkout 之外再编辑**，然后让
   `EXTENSIONS_CONFIG` 指向你的副本（例如
   `EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml`）。这份文件不会被任何东西
   自动加载——未改动的 checkout 不带任何会启用这个插件的默认值。
3. **让 `SILICON_NOTEBOOK_UI_PLUGINS` 指向 `ui/arxiv-search`**（用绝对路径，例如
   `/path/to/examples/extensions/arxiv-search/ui/arxiv-search`），然后重新构建前端
   （`npm run build`，或者交给 `npm run start` 的 `prebuild` 自动同步）。

两个变量都必须在**进程启动前**设置好——插件拓扑在启动组装阶段就已冻结。没有热加载：
改了 TOML 或 `SILICON_NOTEBOOK_UI_PLUGINS` 之后，必须重启后端、重新构建并重启前端。

## 二、设置表

以下每个键都是可选的；核心会从设置模型自身计算出可接受的键集合，所以部署 TOML 里写
错一个键名会导致启动失败，而不是被静默忽略。

| 键 | 默认值 | 取值范围/形状 |
| --- | --- | --- |
| `base_url` | `https://export.arxiv.org/api/query` | 必须是绝对 `http(s)` URL，不带查询串，不带 fragment；不满足则启动时 fail-fast |
| `max_results` | `10` | 整数，`1`–`20` |
| `timeout_seconds` | `10.0` | `0 < x ≤ 60` |
| `politeness_interval_seconds` | `3.0` | `0 ≤ x ≤ 30` |
| `user_agent` | `silicon-notebook-arxiv-sample/0.1 (+https://arxiv.org/help/api)` | 非空，不含控制字符；不满足则启动时 fail-fast |
| `consult_enabled` | `false` | 布尔值 |
| `consult_max_suggestions` | `3` | 整数，`1`–`5` |

`base_url` 与 `user_agent` 在启动时就会被校验（必须是绝对 `http(s)` 且无查询串/
fragment；必须非空且不含控制字符）——这两个值都会跨过一道信任边界直接交给
`urllib`，所以 TOML 里的一个笔误会在启动时就响亮失败，而不是变成一个悄悄存在的
运行时形状。

`politeness_interval_seconds` 默认 3.0 秒，这个数字来自 arXiv 官方 API 使用条款——
它要求调用方两次请求之间至少间隔三秒。要不要调低是部署方自己与 arXiv 之间的约定，
不归这个样板管；允许填 `0` 只是为了不强迫测试与镜像部署等待。

## 三、插件私有数值上限登记表

以下这些数字**刻意不登记**在 `docs/product-and-api.md`/`_zh.md`（那对文档只登记
核心的数值上限）——它们是这个插件私有的，登记处就是这份 README。

| 常量 | 取值 | 所在模块 | 说明 |
| --- | --- | --- | --- |
| `TITLE_MAX_CHARS` | 200 | `atom.py` | 见下方「刻意对齐」说明 |
| `SUMMARY_MAX_CHARS` | 400 | `atom.py` | 见下方「刻意对齐」说明 |
| `AUTHOR_MAX_CHARS` | 80 | `atom.py` | |
| `PUBLISHED_MAX_CHARS` | 40 | `atom.py` | |
| `MAX_AUTHORS` | 20 | `atom.py` | 一条记录里超出的作者会被丢弃 |
| `ARXIV_ID_MAX_CHARS` | 64 | `atom.py` | |
| `MAX_RESPONSE_BYTES` | 1 MiB（1024×1024） | `client.py` | 约束的是网络成本而非内存，见第五节 |
| `MAX_QUERY_TERMS` | 8 | `client.py` | 见下方「静默丢词」说明 |
| `QUERY_MAX_CHARS` | 200 | `routes.py` | 交互式 `/search` 检索词长度 |
| `MAX_IMPORT_URLS` | 20 | `routes.py` | 单次 `/import` 请求 |
| `MAX_URL_CHARS` | 2048 | `routes.py` | 导入批次里单个 URL 的长度 |
| `START_MAX` | 10,000 | `routes.py` | 翻页上限 |
| `CONSULT_RETURN_MARGIN_SECONDS` | 0.25 | `consult.py` | 见 4.1 节 |

`TITLE_MAX_CHARS`/`SUMMARY_MAX_CHARS` 刻意与核心自己的
`GAP_SUGGESTION_TITLE_MAX_CHARS`/`GAP_SUGGESTION_SUMMARY_MAX_CHARS` 取相同值——这是
**刻意对齐**（省掉了写进缺口建议时再截断一次的麻烦），**不是契约耦合**：核心有自己
独立的一份上限，这个插件把它调高或调低都不会破坏核心那一侧。

**`MAX_QUERY_TERMS` 是用户可见的行为，必须按这个口径理解。** 用户在检索框里输入
超过 8 个词时，第 9 个词起会在请求发往 arXiv 之前就被**静默丢弃**
（`build_query_url` 取的是 `query.split()[:MAX_QUERY_TERMS]`）。界面对此不会有任何
提示。

## 四、行为披露

### 4.1 缺口外扩需要两个设置项同时打开，不是一个

只把 `consult_enabled = true` 打开**并不够**让插件真的去联系 arXiv。核心给整个
`ask.gap_consult` 扩展点设了一个统一的硬 deadline，即
`ASK_GAP_CONSULT_TIMEOUT_SECONDS`（默认 **4.0** 秒，取值范围 `0 < x ≤ 30`）。按这个
插件自己的默认值，它需要在这个 deadline 内完成的最坏情况耗时是：

```
politeness_interval_seconds + timeout_seconds + CONSULT_RETURN_MARGIN_SECONDS
= 3.0 + 10.0 + 0.25 = 13.25 秒
```

这比 4 秒的 deadline 长，所以插件会直接拒绝发起这次请求——无论你把 `consult_enabled`
翻多少次，它都不会真的触发。要真正启用外扩，必须**同时**把
`ASK_GAP_CONSULT_TIMEOUT_SECONDS` 抬到超过 13.25 秒（例如 `15.0`；核心侧的上限是
30），或者把 `timeout_seconds` 压低到最坏情况能塞进部署现有的 deadline 里。这是
刻意的行为：一个在核心 deadline 之后才到达的答案不会被任何人读到，发起一次注定
赶不上的请求纯属浪费。

### 4.2 中文问题下缺口外扩多半不会出建议

抽取查询词这一步会把**问题措辞与全部缺口短语**一起扫描拉丁字母检索词
（`consult.py::_query_terms`）。如果一个词都抽不出来，外扩会直接返回一个稳定
代码（`arxiv_no_latin_terms`），**零网络调用、零占用节流锁**——连请求都不会尝试
发起。理由是 arXiv 是一个拉丁关键词索引，一个纯中文的问题保证零命中；发过去只会
白白消耗一次节流配额和一次往返，去确认一件本地就能确定的事。可见的后果是：
**在一个内容以中文为主的笔记本里，缺口外扩很少甚至不会出现建议。** 这是插件设计
如此，不是需要修的 bug。

### 4.3 节流锁没有单次调用的总耗时上限

`timeout_seconds` 约束的是**一次** socket 操作
（`urllib.request.urlopen(..., timeout=timeout_seconds)`），不是整次调用——这个计时
在每次 connect 和每次局部 `read()` 时都会重置。一个持续小流量滴流、但从不在
`timeout_seconds` 内彻底沉默的上游，可以把进程级的节流锁（生产环境固定
`--workers 1`，所以这把锁确实是进程级的）占用得远超 `timeout_seconds` 表面暗示的
时长，而共享 FastAPI 线程池的每一个并发 `/search` 请求都要跟着一起等。
`ask.gap_consult` 那条路由由它的**调用方**兜底——核心的 `GapConsultHost` 会用自己
的墙钟 deadline 给整次「探活 + 咨询」调用兜住上限，不管下面这层传输实际怎么表现。
交互式 `/search` 路由**没有等价的外层 deadline**：它的预算
（`timeout_seconds + politeness_interval_seconds`）只是交给节流锁的一个请求值，
不是这个模块自己对调用施加的上限。这个样板不打算加一个。多 worker 或多副本的
部署需要额外的外部协调（例如基于 Redis）；这个样板刻意不提供。

### 4.4 「本次已导入过」是面板的会话记忆，不是后端去重

核心的 URL 导入路径**不按内容去重**——每一个通过 PDF 探测的 URL 都会无条件建出
一条新来源行，并完整解析一遍。（内容哈希去重在这个产品里确实存在，但只存在于
浏览器**上传**路径上，按 `(notebook_id, file_hash)` 键控；URL 导入器完全走不到
那条路。）所以重复导入同一个 PDF 链接，会真的建出第二个来源并再解析一遍。面板上
「本次已导入过，可能已产生重复来源」这句提示，读的是它自己在本次会话里记下的
「已经发送过这个 URL」的记忆——它是一句**警告**，不是「服务端复用了同一个来源」
的安抚。

### 4.5 样板的测试刻意偏离 SOP §5.3

样板自己的测试放在 `backend/tests/test_arxiv_sample_plugin.py`（插件自己的判断，
对着手搭的接缝跑）与 `backend/tests/test_arxiv_sample_plugin_e2e.py`（真发现、真
应用、真挂载的那条线）——都在本仓库自己的测试树里——而不是像
[SOP §5.3](../../../docs/deployment-extensions-sop_zh.md#53-在插件仓库里跑的检查)
教一个真正的仓库外插件那样，放进插件包自己的 `tests/` 目录。原因：本仓库的后端
验证泳道只收集 `backend/tests`。如果样板按 SOP §5.3 描述的方式把测试留在自己的
包树里，这些测试在本仓库自己的 CI 里就永远不会被跑到——它们会被交付出去却从未
运行过。**一个真正的仓库外插件应当照 SOP §5.3 的字面意思去做**，把测试放进自己
仓库的 `tests/` 目录；不要照抄这个样板的安排。这条偏离的代价是：「整包复制出去、
测试就能独立跑起来」这半句承诺在这个样板身上不成立，虽然它对一个真正的仓库外插件
是成立的。

零补丁验收管的是**运行时**，那一半是覆盖到的：
`test_arxiv_sample_plugin_e2e.py::test_the_package_runs_from_outside_the_repository`
把整个包复制到 checkout 之外的临时目录，只把副本放上 `sys.path`，用一份点名它的
TOML 起一个真应用，经挂载好的路由跑通一次检索，并断言每个被 import 的模块的
`__file__` 都在副本目录之下。另一半——一个干净的第二份 checkout、三个环境变量、
一次绿色的 `npm run build`、以及一个真能用的侧栏面板——需要第二份 checkout，
转录在引入这个样板的那个 PR 正文里。

### 4.6 一条对镜像部署有实际影响的静默过滤

缺口建议里的 URL 会经过 `settings.py::egress_allowed` 校验，它只放行
`arxiv.org`、`export.arxiv.org`，以及**这次部署自己配置的 `base_url` 所在的主机**。
所以如果 `base_url` 指向一个内部镜像，而这个镜像的 Atom 订阅返回的 PDF 链接落在
**第三个**、不同的主机上，这些建议会被**静默丢弃**（fail-safe 方向）。注意导入
路由自己的白名单更宽（放行任意 `*.arxiv.org` 子域）——这两处刻意不同：用户自己
点出来的结果走更宽松的检查；没人要求过的缺口外扩建议走更窄的检查。

## 五、其它已登记的局限

- **XML 实体扩展攻击。** `xml.etree` 走的是 libexpat 解析器，自 libexpat 2.4 起
  （CPython 3.9.6+/3.8.11+/3.10.0b4+ 及更新版本已内建）就自带一道针对实体扩展（「千笑攻击」）
  载荷的放大系数护栏。真正在这些运行时上起作用的防御是那道护栏，不是这个插件里
  的任何东西。第三节里的 `MAX_RESPONSE_BYTES` **不是**针对这类攻击的缓解措施——
  几百字节的载荷照样能声明百万级的放大系数——它只约束网络成本。这个样板真正拥有
  的那一半缓解是：`base_url` 是部署方配置的值，不是用户输入，没有人能不先编辑一份
  TOML 文件就把这个模块指向一个不可信的上游 URL。一个真正要面对不可信上游的插件
  应当依赖 `defusedxml`；这个样板刻意保持零第三方依赖——`pyproject.toml` 只声明了
  `pydantic`，扩展 SDK 与 FastAPI 都来自插件被装进去的那个后端环境，不做 vendoring。
- **节流锁是进程内的**（见 4.3 节）。
- **没有 `api_key_env`。** arXiv 的 API 不需要凭证，所以这个插件没有为一个用不上
  的东西造一个设置键。一个真正需要凭证的插件该怎么引用密钥，见
  [SOP §3.2](../../../docs/deployment-extensions-sop_zh.md#32-settings可选) 的凭证惯例。

## 六、两个入口分别演示了什么

1. **人工检索导入。** 侧栏入口 → 检索弹窗 → 勾选结果 → 走插件**自己的**
   `/import` 路由 → 核心的 URL 导入端口（这个端口自己会对当前请求用户在目标笔记本
   上的 `sources:write` 权限做判定——插件路由自己不做这道判定）。
2. **Agent 触发的缺口外扩。** 一次逐步推理问答收尾时，核心的 `ask.gap_consult`
   扩展点会向已安装的插件询问笔记本之外的线索。回答卡上那个导入建议的按钮是
   **核心自己的界面，调用核心自己的端点**——完全不经过这个插件。

**两道能力门是分开的两件事，不是一件。** `manifest.provides` 里的那个能力只门控
侧栏入口本身（「这个插件配好了吗」）。缺口外扩单独由 `ArxivGapConsultContributor`
自己的可用性探针门控（「这次部署是否同意让它自己去联系 arXiv」）。关掉外扩不影响
检索面板与导入路由，它们照常可用。

## 七、G2 泳道与恢复命令

本节只讲**前端 UI 包**这一半——后端那一半的测试见 4.5 节，随 **G1** 每次 PR 就跑，
与仓库里其它测试同待遇。这个样板的前端插件包**不在**默认的 `npm run test` 覆盖树里——
`extension-ui-host.component.test.tsx` 钉住的是「合并后的注册表等于内建目录，
长度为 1，零插件配置」，这条不能为了迁就这个样板而放宽。它自己的验证泳道是
[`bash scripts/check_sample_plugin.sh`](../../../scripts/check_sample_plugin.sh)
（G2），挂在 `bash scripts/check_extended.sh` 里跑。

如果这条脚本被中途中断（或者同步结果因其它原因留在了树上），之后发现
`frontend/features/ext-arxiv-search/` 仍在磁盘上，用下面的命令恢复：

```bash
cd frontend && SILICON_NOTEBOOK_UI_PLUGINS="<你原来的值，或留空>" \
  node scripts/sync-ui-plugins.mjs
```

脚本自己带的 `trap` 会在退出时**恢复**调用者原本的 `SILICON_NOTEBOOK_UI_PLUGINS`
取值，而不是把它清空——一台开发者本机或部署机器如果本来就配置了自己的私有插件，
不该因为这个样板的检查被中断而丢掉它们。
