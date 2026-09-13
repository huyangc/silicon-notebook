# 电路图识别 —— 部署插件样板

[English](./README.md)

`source.element_enricher` 扩展点的样板部署插件：来源解析时把其中每张图片交给视觉
模型判定，被判为电路原理图的，把 SPICE 风格网表和一句功能说明写进核心已有的元素里。

任何默认 checkout 里它都是关闭的，开启完全是部署方的决定。它与
[`docs/deployment-extensions-sop_zh.md`](../../../docs/deployment-extensions-sop_zh.md)
（部署插件 SOP）配套，也与隔壁的 arXiv 样板互补——那个演示的是另外两个点（HTTP 路由与
gap 咨询）。

本文写给开启它的运维，不写给扩展这份样板代码的人；后者请读源码 docstring 与 SOP。

## 1. 开启的两步

1. **把 Python 包装进后端解释器。** 或者 `pip install -e examples/extensions/circuit-diagram`
   装进 `PYTHON_BIN` 指向的环境，或者把它的 `src/` 放进 `PYTHONPATH`。包名是
   `silicon-notebook-circuit-diagram`，可导入模块是 `silicon_notebook_circuit_diagram`。
   然后把 [`extensions.example.toml`](./extensions.example.toml) 复制到 checkout 之外、
   改好，再让 `EXTENSIONS_CONFIG` 指向你的副本（例如
   `EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml`）。没有任何东西会自动加载
   那个文件——未改动的 checkout 不带任何让插件生效的默认值。
2. **导出 API key**，变量名由 `api_key_env` 指定（默认 `DEEPSEEK_API_KEY`）。在该变量
   为空之前，插件处于「已加载但不可用」：它出现在 `/admin/extensions` 里，核心不会向它
   提问，每次解析的行为与没装它逐字相同。事件日志记 `api_key_missing`，不是失败。

两者都必须在进程启动**之前**就位——插件拓扑在启动组合期冻结，没有热更。改了 TOML 或
环境变量之后重启后端。

这和运行时启停是两层：插件加载之后，管理员可以在 `/admin/extensions` 不重启地把它关掉。
见 [部署插件 SOP §8](../../../docs/deployment-extensions-sop_zh.md#8-升级回滚与停用)。

没有 UI 包，也没有前端步骤：这个插件不加面板、不加路由、不加插槽。它产出的一切由核心
既有的来源详情视图渲染。

## 2. 设置表

所有键都可选；核心从设置模型本身算出可接受的键集，所以部署 TOML 里拼错的键是启动失败，
不是被静默忽略的一行。

| 键 | 默认 | 取值范围 / 形状 | 作用 |
| --- | --- | --- | --- |
| `base_url` | `https://api.deepseek.com` | 绝对 `http(s)` URL，无 query、无 fragment | 端点根。插件自己接 `/chat/completions`；末尾斜杠会被去掉。 |
| `model` | `deepseek-flash` | 非空、无控制字符 | 随请求体发出，并写进每条补全的元素 metadata。 |
| `api_key_env` | `DEEPSEEK_API_KEY` | 环境变量名 | 持有 key 的变量**名**，不是 key 本身。 |
| `timeout_seconds` | `30.0` | `0 < x ≤ 120` | 单请求 socket 超时，也是预算检查预留的最坏情况。 |
| `max_images_per_source` | `8` | `1..64` | 一条来源最多送几张图，按解析顺序。 |
| `max_image_bytes` | `4194304` | `1024..33554432` | 超过这个字节数的图直接跳过，不发。 |
| `prompt_language` | `zh` | `zh` 或 `en` | 要求模型作答的语言，也是描述抬头的语言。 |

## 3. 它写什么、写在哪

模型判为电路原理图的每张图，插件出一条候选，核心把它落进那个元素：

* `metadata.extensions["examples.circuit_diagram.enricher"].metadata` ——
  `{"is_circuit": true, "netlist": "...", "function": "...", "model": "..."}`，
  挂在该 contribution 自己的名下，旁边是插件 id 与版本。
* `metadata.description` —— 给人看的文本，追加在解析器已有描述之后：

  ~~~
  电路功能：一个由 R1/R2 构成的分压网络……

  ```spice
  R1 in out 10k
  R2 out 0 10k
  ```
  ~~~

  前端按围栏切分，把网表渲染成代码块，而不是把它重排成一段话。
* `text` —— 同一段描述压平空白后追加到元素自己的 text。这一步才是让元素进入检索语料的
  原因：既无图注也无描述的图片元素不进分块，所以补全之前一张裸电路图是搜不到的。

模型说**不是**电路图的，什么都不写——不留「已检查、非电路图」的标记。那种标记的全部内容
只是这个插件自己的判断，不值得落进笔记本。

## 4. 不承诺准确性

这份样板的存在是为了证明「解析出的图片 → 可检索的持久化元数据」这条链路通。它不声称
`deepseek-flash` 能读对任何一张具体的原理图，它产出的网表应当当作给人看的起点，而不是
能直接仿真的电路描述。准确性够不够是部署方自己的问题；`model` 字段随每条补全一起落库，
就是为了以后回头核对时不用猜是哪个模型写的。

## 5. 已登记的限制

* **`timeout_seconds` 约束的是单次 socket 操作，不是整次调用。**
  `urllib.request.urlopen(..., timeout=...)` 在连接和每次部分读取时都会重置这个计时，
  所以一个一直挤牙膏的上游可以拖得比它久。真正约束这个插件这一轮的，是核心自己的
  `SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS` 硬截止，由宿主在一条它可以放弃的线程上执行。
* **一条来源可能只被处理一部分。** 除非核心截止时间还剩 `timeout_seconds + 0.25s`，
  否则插件不开始下一张图，剩下的作为部分结果上报。两个设置怎么互相配平，见
  `extensions.example.toml` 末尾那段说明。
* **请求是串行的，一次一张。** 不批处理、不并发：这个点跑在宿主自有的单条工作线程上，
  插件自己并发发请求等于制造部署方没有预算过的延迟。
* **没有出网地址策略。** `base_url` 是部署配置而不是用户输入，所以插件不像核心的 URL
  摄取那样再查一次主机是否解析到公网地址。如果做成让**用户**选端点的变体，必须补这道检查。
* **响应体最多读 256 KiB**，超过这个上限的响应变成一张被跳过的图，而不是无界读取。
* **单图失败只记数，不逐图上报。** 请求被拒、答案畸形、资产读不出，各自跳过该图并把
  结果变成 `partial`；核心的事件带的是计数，不是哪一张。
* **不跟随重定向。** `base_url` 返回 3xx 一律当作请求失败，不当作跳转。凭据放在
  `Authorization` 头里，而 `urllib` 默认的处理器会把这个头原样重放到响应指定的任何主机上；
  端点搬家的部署方应当改 `base_url`。
* **同字节图片只判一次，但不做预筛。** 一条来源之内按图片字节的 SHA-256 去重，所以重复
  四十页的页眉 logo 只花一次请求。但它仍然占掉 `max_images_per_source` 的四十个名额
  ——这个上限在读任何字节之前就作用在元素列表上。也刻意**没有**最小尺寸过滤：「太小了不可能
  是电路图」是这份样板不做的判断，所以满页图标可以把预算吃光。文档是这种形状的部署方
  应当调低 `max_images_per_source` 或调高核心的截止时间。
* **模型的答案在落库前会被重塑。** 换行归一；核心会拒收的字符（U+3000、NBSP、零宽连接符、
  BOM）替换成普通空格；模型自带的围栏行被剥掉，免得和本插件写的围栏嵌套；两个字段都截到
  下面的上限。声称是电路图却既没网表也没功能说明的答案直接丢弃。

### 插件私有数值上限

这些不是部署可配置项，写死在插件源码里。列出来是因为它们解释了运维会看到的现象。

| 上限 | 值 | 位置 | 约束什么 |
| --- | --- | --- | --- |
| `NETLIST_MAX_CHARS` | 4000 | `enricher.py` | 归一后落库的网表最长长度。 |
| `FUNCTION_MAX_CHARS` | 1000 | `enricher.py` | 落库的功能说明最长长度。 |
| `RETURN_MARGIN_SECONDS` | 0.25 | `enricher.py` | 开始下一张图之前，在 `timeout_seconds` 之外额外预留的余量，覆盖解析与核心 50ms 的 join 分片。 |
| `MAX_RESPONSE_BYTES` | 256 KiB | `client.py` | 单次响应体最多读多少。 |
| 去重 | 单次 `enrich` 内按图片字节 SHA-256 | `enricher.py` | 只记住成功的分类；失败的请求会在下一张同字节图上重试。 |

当核心的 `SOURCE_ELEMENT_ENRICHER_MAX_DESCRIPTION_CHARS` 更小时，
`NETLIST_MAX_CHARS` 与 `FUNCTION_MAX_CHARS` 会被进一步压缩——抬头和围栏先从那个上限里扣。

## 6. 用真实模型手工验证

自动化测试全程零网络（见 §7），所以它们唯一验不了的就是你的端点、key 和模型到底答不答。
这一步是手工的，一分钟：

1. 导出 key，把 `EXTENSIONS_CONFIG` 指向你的 TOML，启动后端。
2. 打开 `/admin/extensions`，确认插件在列且启用。如果在列却什么都不产出，就是 key 没给
   ——事件日志里是 `api_key_missing`。
3. 往一个临时笔记本上传一个带内嵌电路图的小 Markdown 文件（一个
   `![](data:image/png;base64,...)` data URI 就够，`.zip` 包或 PDF 也行）。
4. 等来源进入 `extracted` 再打开它。那张图下面应当出现 `电路功能：…` 一行，后面跟着
   围栏起来的网表代码块。
5. `GET /api/sources/{source_id}/elements`，确认图片元素上有
   `metadata.extensions["examples.circuit_diagram.enricher"]`。
6. 在那个笔记本里问一个答案落在功能说明里的问题。这张图片元素现在应当能被检索到
   ——这正是把描述追加进 `text` 的目的。

第 4 步什么都没有时，先看事件日志：这个点每次尝试都发 `source_element_enricher_attempt`
并带一个稳定的 `reason_code`，而插件从不把 URL、key 或任何设置值放进去。

## 7. 测试

`backend/tests/test_circuit_diagram_sample_plugin.py` 覆盖插件自己**决定**的部分：设置
校验、可用性探针三态、模型答案的三种形状、传输层构造的请求、预算不足时的提前停止，以及
把非图片挡在外面的过滤。`…_e2e.py` 覆盖真 TOML 点名之后核心拿它做什么：真
`create_app()`、真上传、真解析，再把落库的元素从 API 读回来断言。

两者都零网络：唯一被替换的接缝是 `client._post`——本包为此专门提供的可注入传输；e2e 文件
另外让 `socket.getaddrinfo` 抛异常，这样以后谁把真实拨号改回来都会响亮地红。

这些测试放在后端测试根目录而不是本包里，是因为后端验证泳道只收 `backend/tests`——样板
把测试放自己树里就等于发了一堆没人跑的测试。**不要把这个安排抄进真正的仓库外插件**：
SOP 要求那样的插件把测试留在自己的仓库、在自己的 CI 里跑。
