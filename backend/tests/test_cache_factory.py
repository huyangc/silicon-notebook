"""唯一构造点的行为 + chat_json 写缓存的准入规则。

这里的缓存测试一律是**行为测试**（真 backend + fake upstream + 数调用次数），
不做「切一段源码断言里面有某个条件」那种锁定。源码断言只挡得住「删除」变异：
评审实测把守卫原样挪进一个无作用的兄弟分支（`if content != "{}": pass`）后，
源码断言照过，而行为探针显示 "{}" 真的被写进了缓存。
"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.cache import CacheAdmin, NoCacheBackend, llm_key, make_cache_backend
from app.core.config import Settings, _ROOT_DIR
from app.core.llm import OpenAICompatibleClient


def test_disabled_returns_noop(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=False, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    assert isinstance(make_cache_backend(s), NoCacheBackend)


def test_enabled_returns_working_backend(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    backend = make_cache_backend(s)
    backend.put("k", "v", tag="m")
    assert backend.get("k") == "v"


def test_cache_is_enabled_by_default():
    """默认开是本轮的头号交付物，但测试进程里 conftest 强制 LLM_CACHE_ENABLED=false，
    `Settings()` 永远读到 env —— 于是把 Field(True) 改回 Field(False) 全量 4623 项
    也不会红。断言 Field 默认值本身：不依赖 env，也不受开发机真 .env 干扰。
    变异验证：config.py 里改成 Field(False, ...) 后本测试必须转红。
    """
    assert Settings.model_fields["llm_cache_enabled"].default is True


def test_test_process_is_isolated_from_the_shared_cache():
    """测试进程必须**强制**拿到 NoCacheBackend——安全阀 #2，这条守卫锁的就是它。

    缓存默认开、跨用户全局共享、落在仓库根 `.local/llm_cache_v2.db`。带真 .env 跑
    全量时若隔离失效，断言会读到上一次运行的响应，制造大规模假失败/假成功——本
    仓库台账里记过的真实事故。

    ⚠ **必须走 env 构造 `Settings()`，不能写 `Settings(LLM_CACHE_ENABLED=...)`**：
    显式传参绕过了 conftest 那行 `os.environ[...] = "false"`，测的就成了 pydantic
    的赋值语义而非隔离本身，鉴别力归零。这里断言的是「本进程的**环境**已被置成
    关闭」这一事实。

    变异验证：注释掉 conftest.py 顶部的 os.environ["LLM_CACHE_ENABLED"] = "false"
    后本测试必须转红（实测：删掉那行跑 6 个缓存测试文件 61 passed，无一转红，
    同时 .local/llm_cache_v2.db 被真实创建 28 KB）。
    """
    backend = make_cache_backend(Settings())
    assert isinstance(backend, NoCacheBackend), (
        "测试进程没有被隔离在共享缓存之外：make_cache_backend(Settings()) 返回了 "
        f"{type(backend).__name__}。跑全量时断言会读到上次运行留下的响应。"
    )


def test_relative_path_is_anchored_to_repo_root(tmp_path, monkeypatch):
    """相对路径必须锚定到**仓库根**，与进程 CWD 无关。

    只断言 put/get 往返是测不出锚定的：把工厂里的锚定整段换成 `Path.cwd() / raw`
    照样 6 passed，文件却落到了 backend/.local（本仓库栽过三次的「双 .local」）。
    所以这里 chdir 到别处，再断言文件的**绝对落点**。
    """
    rel = ".local/test_cache_factory_anchor.db"
    expected = _ROOT_DIR / ".local" / "test_cache_factory_anchor.db"
    stray = tmp_path / rel
    monkeypatch.chdir(tmp_path)
    _unlink_db(expected)
    try:
        backend = make_cache_backend(
            Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=rel))
        backend.put("k", "v")
        assert backend.get("k") == "v"
        assert expected.is_file(), f"缓存文件没落在仓库根：期望 {expected}"
        assert not stray.exists(), f"缓存文件跟着 CWD 跑了：{stray}"
    finally:
        _unlink_db(expected)


def _unlink_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


# --------------------------------------------------- 每个缓存文件只有一个实例
def test_factory_reuses_one_backend_per_path(tmp_path):
    """同一个缓存文件在本进程内只能有一个 backend 实例。

    生产形态是「每个 OpenAICompatibleClient（system llm / kg_llm / rewrite /
    reasoning + per-user）各一个 + embedder 一个」都来这里取。每次 new 一个的话，
    它们共享同一个文件而锁是实例私有的——put() 里的读改写在实例之间没有互斥。
    """
    path = str(tmp_path / "shared.db")
    a = make_cache_backend(Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=path))
    b = make_cache_backend(Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=path))
    assert a is b, "同一路径拿到了两个实例：锁不共享，计量会漂移、fd 会翻倍"


def test_factory_keeps_distinct_paths_isolated(tmp_path):
    """记忆化的键是路径，不是全局单例——不同文件必须各自独立。

    没有这条，把记忆化写成"整个进程一个实例"也能让上面那条绿，而测试隔离
    （每个 tmp_path 一个库）会当场塌掉。
    """
    a = make_cache_backend(
        Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "a.db")))
    b = make_cache_backend(
        Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "b.db")))
    assert a is not b, "不同缓存文件被记忆化成了同一个实例"
    a.put("k", "va")
    assert b.get("k") is None, "两个库串了"


def test_concurrent_consumers_never_drift_volume_accounting(tmp_path):
    """行为断言：多个消费者各自 make_cache_backend + 并发 put 同一个 key，
    增量计量不得与真实内容漂移。

    这是记忆化真正要防的事，identity 断言只是它的机制。评审实测未记忆化时
    4 实例 × 4 线程 × 400 次同 key put → meta=54000 real=50000（8% 虚高），
    且漂移只累积不自愈；虚高越过 size_limit 后，下一次 put 会为了满足幻影字节
    把健康条目**全部**淘汰（实测 entries 8 → 0）并永久停在 0 条。

    "相同内容 → 相同 key → 并发重复 put" 对内容寻址缓存是最典型的场景，不是边角。
    修复后单锁串行化，本测试确定性绿；变异（去掉记忆化）后转红。
    """
    path = str(tmp_path / "shared.db")
    consumers = [
        make_cache_backend(Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=path))
        for _ in range(4)                    # 生产形态：4 个 LLM client + embedder
    ]
    key = "shared-key"
    errors = []

    def worker(consumer, t):
        try:
            for i in range(300):
                consumer.put(key, f"v{t}-{i}-" + "z" * (t * 11 + i), tag="m")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(consumers[c], c * 4 + t))
        for c in range(4) for t in range(4)   # 4 消费者 × 4 线程
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)

    assert not any(th.is_alive() for th in threads), "并发 worker 超时未结束"
    assert not errors, f"并发操作抛异常：{errors[:3]}"
    volume, real = consumers[0].volume(), consumers[0].recount()
    assert volume == real, (
        f"多消费者并发 put 导致计量漂移：volume()={volume} != recount()={real}。"
        "漂移只累积不自愈，虚高越过 size_limit 会把整个缓存清空并永久停在 0 条。"
    )


def test_llm_key_is_stable_and_content_addressed():
    msgs = [{"role": "user", "content": "hello"}]
    u = "https://llm.example.test"
    assert llm_key("m", msgs, "{}", u) == llm_key("m", msgs, "{}", u)
    assert llm_key("m", msgs, "{}", u) != llm_key("m2", msgs, "{}", u)


def test_llm_key_includes_service_identity_and_never_api_key():
    """base_url（服务身份）是 key 的一部分：per-user 模型配置下同 model 名可指向不同
    endpoint，而缓存跨用户全局共享——只认 model 名会让第二个用户拿到第一个 endpoint
    的响应。api_key 绝不进 key：它会经 stats()/日志/调试路径间接暴露。

    变异验证：把 policy.llm_key 里的 base_url 从 key 材料里去掉后，第一条断言转红。
    """
    import inspect

    from app.core.cache import llm_key as lk

    msgs = [{"role": "user", "content": "hello"}]
    assert llm_key("m", msgs, "{}", "https://a.test") != llm_key("m", msgs, "{}", "https://b.test"), (
        "同 model 名、不同 base_url 必须是不同的 key"
    )
    # api_key 不是也不该是 key 的入参——从签名层就杜绝它进入 sha256 材料。
    assert "api_key" not in inspect.signature(lk).parameters, "api_key 绝不能成为 key 材料"


def test_llm_key_deliberately_diverges_from_legacy_after_adding_base_url():
    """历史实现（llm_cache.cache_key）不含 base_url。本特性刻意让 key 纳入服务身份，
    因此与历史值不再逐字节相同——这是一次性全冷（缓存尚未上线，冷启动重建即可），
    不是回退。留此测试锁住「确实因为 base_url 而分叉」，避免有人把它改回去。"""
    from app.core.llm_cache import cache_key as legacy
    msgs = [{"role": "user", "content": "中文 content"}]
    assert llm_key("model-x", msgs, '{"a":""}', "https://svc.test") != legacy(
        "model-x", msgs, '{"a":""}'
    )


def test_llm_key_includes_generation_parameters():
    """采样/预算参数（temperature / top_p / max_tokens）是 key 的一部分：相同 messages
    + schema 但不同生成参数是**不同的 upstream 请求**，绝不能共用一条缓存。max_tokens
    尤其关键——本仓库文档化的截断补救就是调大 KG_EXTRACT_MAX_TOKENS 重跑，若它不在
    key 里，调大后仍会命中被截断的旧响应。

    变异验证：把 policy.llm_key 材料里的任一参数（temperature / top_p / max_tokens）
    从 payload 里删掉，对应那条断言即转红。
    """
    msgs = [{"role": "user", "content": "hello"}]
    u = "https://llm.example.test"
    base = llm_key("m", msgs, "{}", u, temperature=1.0, top_p=1.0, max_tokens=8192)
    assert base != llm_key("m", msgs, "{}", u, temperature=0.2, top_p=1.0, max_tokens=8192), (
        "不同 temperature 必须是不同的 key"
    )
    assert base != llm_key("m", msgs, "{}", u, temperature=1.0, top_p=0.5, max_tokens=8192), (
        "不同 top_p 必须是不同的 key"
    )
    assert base != llm_key("m", msgs, "{}", u, temperature=1.0, top_p=1.0, max_tokens=51200), (
        "不同 max_tokens 必须是不同的 key（调大预算重试不能命中被截断的旧响应）"
    )
    # 全同 → 稳定同 key（内容寻址不因入参顺序/重复调用而漂移）。
    assert base == llm_key("m", msgs, "{}", u, temperature=1.0, top_p=1.0, max_tokens=8192)


def test_llm_key_isolates_explicit_thinking_mode_without_colding_defaults():
    """Provider-default and explicit non-thinking requests are distinct, while
    omitting the new parameter preserves the historical key material."""
    msgs = [{"role": "user", "content": "hello"}]
    u = "https://llm.example.test"
    default = llm_key("m", msgs, "{}", u)
    assert default == llm_key("m", msgs, "{}", u, thinking_mode=None)
    assert default != llm_key(
        "m", msgs, "{}", u, thinking_mode="disabled"
    )


# --------------------------------------------------------------- fake upstream
class _FakeStream:
    """流式替身：内容分两块发，finish_reason 只挂在最后一块（真实 SSE 的形状）。"""

    def __init__(self, content: str, finish_reason):
        mid = len(content) // 2
        self._chunks = [
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content[:mid]), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content[mid:]), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None), finish_reason=finish_reason)]),
        ]
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        self.outer.seen.append(kwargs)
        content, finish_reason = self.outer.reply
        if kwargs.get("stream"):
            return _FakeStream(content, finish_reason)
        choice = SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason)
        return SimpleNamespace(choices=[choice], usage=None)


class _FakeOpenAI:
    """可配返回内容/finish_reason 并数真实调用次数的 upstream 替身。"""

    def __init__(self, content='{"ok": 1}', finish_reason="stop"):
        self.calls = 0
        self.seen = []
        self.reply = (content, finish_reason)
        self.chat = SimpleNamespace(completions=_FakeCompletions(self))


class _KeySpy:
    """透传后端 + 记录生产代码**实际用过**的 key。

    存在的理由：下面那组"绝不入缓存"的断言要落在具体某一条 key 上，而 key 是
    `llm_key(model, full_messages, schema_hint)`——full_messages 由 chat_json 自己
    拼上 system 提示词。在测试里重算一遍等于复制生产逻辑：system 提示词一改，测试
    就会去查一条根本没人写过的 key，永远断言成功而彻底失去鉴别力。

    chat_json 在打 upstream 之前必然先 `cache.get(ckey)`，所以记录 get 的入参就等于
    白拿到生产用的那把 key，零复制。

    本类只实现 CacheBackend 的 get/put——**不**实现 __len__：整组测试因此只依赖
    Protocol 面，换任何只有 get/put 的后端（Redis 等）都照样跑。
    """

    def __init__(self, inner):
        self.inner = inner
        self.keys = []

    def get(self, key):
        self.keys.append(key)
        return self.inner.get(key)

    def put(self, key, value, tag=""):
        self.inner.put(key, value, tag=tag)


def _cached_value(spy):
    """生产代码本次查的那条 key 在缓存里的值；None = 它没被写进去。

    比 `len(backend) == 0`（"缓存整体是空的"）更强：锁的是**这一条** key。
    """
    assert spy.keys, "本次调用根本没查过缓存——测的不是写入准入规则"
    return spy.inner.get(spy.keys[-1])


def _opt_in(client):
    """缓存是 opt-in——调用方传 response_validator 才参与。本文件测的是
    is_cacheable / 缓存键那几道门，与 opt-in 开关正交，所以给每个调用默认挂一个
    放行 validator 把开关打开，命中/写入逻辑才会被真正走到。显式传 validator 的
    调用（如没有）会被尊重；bypass_cache 路径照旧整体跳过。"""
    real = client.chat_json

    def wrapped(*args, **kwargs):
        kwargs.setdefault("response_validator", lambda _c: True)
        return real(*args, **kwargs)

    client.chat_json = wrapped
    return client


def _client(tmp_path, monkeypatch, *, content='{"ok": 1}', finish_reason="stop"):
    """真 SqliteCacheBackend + fake upstream 的 client。返回 (client, fake, spy)。

    第三个返回值是 _KeySpy（只有 get/put），不是具体后端：整组断言因此走 Protocol
    面。评审实测过——换上只实现 get/put 的 Redis 后端后，原来靠 `len(backend)` 的
    8 条安全阀测试全挂在 `TypeError: object of type 'RedisCacheBackend' has no
    len()`，而 __len__ 既不在 CacheBackend 也不在 CacheAdmin 里。
    """
    settings = Settings(
        LLM_LOG_ENABLED=False,
        LLM_CACHE_ENABLED=True,
        LLM_CACHE_PATH=str(tmp_path / "cache.db"),
    )
    spy = _KeySpy(make_cache_backend(settings))
    client = _opt_in(OpenAICompatibleClient(
        settings, base_url="https://llm.example.test", api_key="k", model="m",
        cache=spy,
    ))
    fake = _FakeOpenAI(content=content, finish_reason=finish_reason)
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake, spy


# ------------------------------------------------------------------ 写入准入
def test_empty_json_fallback_is_never_cached(tmp_path, monkeypatch):
    """upstream 交白卷时 chat_json 如实返回空串 ""（master #328 起不再兜底成 "{}"，
    见 test_llm_client.test_raw_client_preserves_empty_success）；缓存空响应等于把一次
    偶发退化固化 90 天。

    行为断言（对删除与移动两种变异都免疫）：条目数 0，且第二次仍真的打了 upstream。
    """
    client, fake, spy = _client(tmp_path, monkeypatch, content="")
    msgs = [{"role": "user", "content": "q"}]
    assert client.chat_json(msgs, "{}") == ""
    assert client.chat_json(msgs, "{}") == ""
    assert _cached_value(spy) is None, "空响应被写进了缓存"
    assert fake.calls == 2, "第二次没重新请求 upstream —— 说明命中了缓存里的空响应"


def test_truncated_json_is_never_cached(tmp_path, monkeypatch):
    """真实形态的截断：半截 JSON + finish_reason=length（两个信号都在）。

    它非空、也不等于 "{}"，原来的 `content != "{}"` 那道守卫放它过（评审实测：
    入库并固化 90 天）。落缓存的后果是上游 safe_json 解析失败 → 该抽取窗口静默
    产出 0 个节点。下面两条把两道门各自单独钉死。
    """
    half = '{"objects": [{"name": "a'
    client, fake, spy = _client(
        tmp_path, monkeypatch, content=half, finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    assert client.chat_json(msgs, "{}") == half
    assert _cached_value(spy) is None, "截断的半截 JSON 被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_unparseable_json_is_never_cached_without_finish_reason(tmp_path, monkeypatch):
    """finish_reason 那道门单独不够：不少 OpenAI 兼容实现根本不报 finish_reason
    （取到 None），此时只有 json.loads 拦得住被截断的内容。

    变异验证：把 policy 里的 json.loads 那道门删掉后本测试必须转红（其余截断用例
    因为同时带 finish_reason=length 仍会绿，挡不住这个变异）。
    """
    half = '{"objects": [{"name": "a'
    client, fake, spy = _client(
        tmp_path, monkeypatch, content=half, finish_reason=None)
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}")
    assert _cached_value(spy) is None, "解析不了的内容被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_length_truncated_but_parseable_json_is_never_cached(tmp_path, monkeypatch):
    """罕见但可能：正好在闭合括号处被切，内容是合法 JSON 却仍是残缺答案。

    json.loads 那道门放它过，只有 finish_reason == "length" 拦得住。
    """
    client, fake, spy = _client(
        tmp_path, monkeypatch, content='{"objects": []}', finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}")
    assert _cached_value(spy) is None, "finish_reason=length 的响应被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_streamed_length_truncated_response_is_never_cached(tmp_path, monkeypatch):
    """流式路径（带 cancel_event）的 finish_reason 挂在**最后一块 chunk** 上，取法
    与非流式完全不同，必须单独覆盖 —— 否则准入规则只在一半路径上生效。

    刻意用「合法 JSON + finish_reason=length」：内容能过 json.loads 那道门，本测试
    因此只依赖流式路径真的把 finish_reason 带了回来。
    """
    client, fake, spy = _client(
        tmp_path, monkeypatch, content='{"objects": []}', finish_reason="length")
    msgs = [{"role": "user", "content": "stream me"}]
    assert client.chat_json(
        msgs, "{}", cancel_event=threading.Event()) == '{"objects": []}'
    assert _cached_value(spy) is None, "流式截断响应被写进了缓存"


def test_streamed_unparseable_response_is_never_cached(tmp_path, monkeypatch):
    """流式的半截 JSON（服务端没报 finish_reason）同样不入缓存。"""
    half = '{"objects": [{"name": "a'
    client, fake, spy = _client(
        tmp_path, monkeypatch, content=half, finish_reason=None)
    assert client.chat_json(
        [{"role": "user", "content": "stream me"}], "{}",
        cancel_event=threading.Event()) == half
    assert _cached_value(spy) is None, "流式的解析不了的内容被写进了缓存"


def test_streamed_complete_response_is_cached(tmp_path, monkeypatch):
    """流式的正常响应仍要缓存 —— 上面几条不能靠「流式一律不写」蒙混过关。"""
    client, fake, spy = _client(tmp_path, monkeypatch, content='{"ok": 1}')
    msgs = [{"role": "user", "content": "stream me"}]
    client.chat_json(msgs, "{}", cancel_event=threading.Event())
    assert _cached_value(spy) == '{"ok": 1}', "正常的流式响应没能写进缓存"
    assert client.chat_json(msgs, "{}", cancel_event=threading.Event()) == '{"ok": 1}'
    assert fake.calls == 1


def test_raising_max_tokens_refetches_after_truncation(tmp_path, monkeypatch):
    """本项目文档化的截断补救手段 = 调大 KG_EXTRACT_MAX_TOKENS 重跑。

    max_tokens 不在缓存键里，所以只要截断响应进了缓存，这条补救手段就彻底失效
    （实测：调大后 upstream 调用数仍是 1，拿回同一段垃圾）。
    """
    client, fake, spy = _client(
        tmp_path, monkeypatch,
        content='{"objects": [{"name": "a', finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}", max_tokens=64)

    fake.reply = ('{"objects": [{"name": "abc"}]}', "stop")
    out = client.chat_json(msgs, "{}", max_tokens=51200)
    assert fake.calls == 2, "调大 max_tokens 后没重新请求 upstream"
    assert out == '{"objects": [{"name": "abc"}]}'
    assert fake.seen[-1]["max_tokens"] == 51200


def test_larger_max_tokens_does_not_reuse_a_smaller_budget_entry(tmp_path, monkeypatch):
    """评审场景，与「截断响应不入缓存」互补的另一半。

    上一条（test_raising_max_tokens_refetches_after_truncation）靠的是**截断响应根本
    没进缓存**（finish_reason=length 被写入准入门拦掉），所以调大预算必然重打——它
    测的是准入门，而非 key 里有没有 max_tokens：把 max_tokens 从 key 删掉它照样绿。

    这一条把 max_tokens **真的进了 key** 单独钉死：让第一次小预算响应是一个恰好合法、
    且 upstream 没报 finish_reason 的结果（`{"objects": []}` + finish_reason=None）——
    它过了全部三道准入门，**确实被写进了缓存**。随后按文档补救手段调大预算重试，绝不能
    命中这条小预算的旧条目。小预算 vs 大预算是两次语义不同的 upstream 请求，只有
    max_tokens 进 key 才能让第二次真正重新请求。

    变异验证：把 max_tokens 从 policy.llm_key 材料里删掉（或 llm.py 不把有效值传进
    去）后，第二次命中小预算旧缓存，fake.calls 停在 1，本测试转红。
    """
    client, fake, spy = _client(
        tmp_path, monkeypatch, content='{"objects": []}', finish_reason=None)
    msgs = [{"role": "user", "content": "extract"}]
    first = client.chat_json(msgs, "{}", max_tokens=64)
    assert first == '{"objects": []}'
    assert _cached_value(spy) == '{"objects": []}', (
        "前提失效：小预算的合法响应本应进缓存，否则这条测的就成了准入门而非 key"
    )

    fake.reply = ('{"objects": [{"name": "abc"}]}', "stop")
    out = client.chat_json(msgs, "{}", max_tokens=51200)
    assert fake.calls == 2, "调大 max_tokens 后命中了小预算的旧响应，没重新请求 upstream"
    assert out == '{"objects": [{"name": "abc"}]}'


def test_max_tokens_none_resolves_to_the_same_key_as_the_explicit_default(tmp_path, monkeypatch):
    """防虚假 miss：chat_json 里 max_tokens=None 回落到 settings.openai_compat_max_tokens。
    显式传入那个**同值**必须命中同一条缓存——key 用的是解析后的**有效值**，不是原始
    入参。否则 None 与其等价默认值分叉成两条 key，白白多打一次模型。

    变异验证：把 llm.py 喂给 llm_key 的 effective_max_tokens 换回原始 max_tokens（None
    vs 8192）后，两次 key 分叉，第二次不再命中，fake.calls 变成 2，本测试转红。
    """
    client, fake, spy = _client(tmp_path, monkeypatch)
    default_budget = client.settings.openai_compat_max_tokens
    assert isinstance(default_budget, int) and default_budget > 0, "前提：全局预算是正整数"
    msgs = [{"role": "user", "content": "same question"}]
    client.chat_json(msgs, "{}", max_tokens=None)
    key_none = spy.keys[-1]
    client.chat_json(msgs, "{}", max_tokens=default_budget)
    key_explicit = spy.keys[-1]
    assert key_none == key_explicit, (
        "max_tokens=None 与显式传入等价默认值产生了不同 key —— key 没用解析后的有效值"
    )
    assert fake.calls == 1, "等价的 max_tokens 没命中同一条缓存（第二次又打了 upstream）"


def test_bypass_cache_never_touches_cache_regardless_of_generation_params(tmp_path, monkeypatch):
    """健康探针走 bypass_cache=True：无论生成参数如何，都不查也不写缓存——否则模型
    故障时缓存命中会显示假绿（model_status / kg run_control 探活正依赖这条）。

    把生成参数纳入 key 的改动不得改变这条路径：effective_max_tokens 的求值挪到了
    bypass 判断之前，但 llm_key/cache.get 仍必须留在 `if not bypass_cache` 之内。
    """
    client, fake, spy = _client(tmp_path, monkeypatch)
    msgs = [{"role": "user", "content": "probe"}]
    client.chat_json(msgs, "{}", bypass_cache=True, max_tokens=64)
    client.chat_json(msgs, "{}", bypass_cache=True, max_tokens=64)
    assert spy.keys == [], "bypass_cache 不应触碰缓存（连 get 都不该调）"
    assert fake.calls == 2, "bypass_cache 仍命中了缓存（第二次没打 upstream）"


class _FalsyWhenEmptyBackend:
    """空时 `bool()` 为 False 的最小后端——SqliteCacheBackend 正是这个形态
    （定义了 __len__，0 条目时 falsy）。

    前提由本测试**自己造**，不再依赖生产后端碰巧实现了 __len__：换成 Redis 这类
    没有 __len__ 的后端时，`not backend` 恒为 False，原来那条前提断言不但会挂，
    语义还整个反了（"空 backend 应当是 falsy" 直接不成立）。而这条测试要锁的东西
    与后端无关——它锁的是 llm.py 的写法。
    """

    def __init__(self):
        self.stored = {}

    def get(self, key):
        return self.stored.get(key)

    def put(self, key, value, tag=""):
        self.stored[key] = value

    def __len__(self):
        return len(self.stored)


def test_cold_cache_accepts_its_first_write(tmp_path, monkeypatch):
    """冷启动回归：写入处若用真值判断 `if cache and ...`，一个 0 条目时 falsy 的
    缓存永远写不进第一条（len 恒 0 → 永远为假），默认开启的缓存实际上从不生效。

    锁的是 llm.py 用 `cache is not None` 而非真值判断这一写法。
    变异验证：llm.py 的 `cache is not None` 改回 `cache` 后本测试必须转红。
    """
    settings = Settings(LLM_LOG_ENABLED=False)
    backend = _FalsyWhenEmptyBackend()
    assert not backend, "前提失效：本测试用的后端在空时必须是 falsy"
    client = OpenAICompatibleClient(
        settings, base_url="https://llm.example.test", api_key="k", model="m",
        cache=backend,
    )
    monkeypatch.setattr(client, "client", lambda: _FakeOpenAI())
    # opt-in：写入需调用方传 validator（本测试锁的是 `cache is not None` 写法）。
    client.chat_json(
        [{"role": "user", "content": "q"}], "{}", response_validator=lambda _c: True
    )
    assert backend.stored, "冷缓存没能完成它的第一次写入"


def test_llm_writes_are_tagged_with_the_model(tmp_path, monkeypatch):
    """evict_tag 的用途正是「换模型服务后清掉该模型的缓存」，不带 tag 就永远清不掉。

    条目事后无法补 tag，所以这条必须在写入侧锁住。本测试**刻意**触碰 CacheAdmin
    （stats/evict_tag）——它测的就是那组可选能力，所以按 isinstance 探测后跳过：
    只实现 get/put 的后端（Redis 等）没有 tag 概念，"清不掉"是它如实的降级而不是
    缺陷。其余安全阀测试一律走 Protocol 面，不需要这个 gate。
    """
    client, fake, spy = _client(tmp_path, monkeypatch)
    if not isinstance(spy.inner, CacheAdmin):
        pytest.skip("当前后端未实现 CacheAdmin：按 tag 清空是可选能力")
    client.chat_json([{"role": "user", "content": "q"}], "{}")
    assert spy.inner.stats()["by_tag"] == {"m": 1}
    assert spy.inner.evict_tag("m") == 1
    assert _cached_value(spy) is None, "evict_tag 之后这条 key 仍在缓存里"


# ---------------------------------- LLM 缓存键必须含服务身份（base_url），排除 api_key

def _client_on(tmp_path, monkeypatch, *, base_url, api_key, backend):
    """一个指向 base_url/api_key、model 名固定为 'm' 的 client，共用传入的 backend。"""
    settings = Settings(LLM_LOG_ENABLED=False)
    # 同名模型 'm'、不同 base_url/api_key：评审场景的前提（服务身份进 key）。
    client = _opt_in(OpenAICompatibleClient(
        settings, base_url=base_url, api_key=api_key, model="m", cache=backend,
    ))
    fake = _FakeOpenAI()
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake


def test_llm_cache_is_scoped_to_the_service_endpoint(tmp_path, monkeypatch):
    """同 model 名、不同 base_url 的两个 client 绝不能互相命中缓存——否则第二个用户
    拿到第一个 endpoint 的响应，自己选的模型服务根本没被调用。

    变异验证：把 llm.py 的 llm_key(... , self.base_url) 里的 base_url 去掉（或把
    policy.llm_key 的 base_url 从材料里删掉）后，本测试转红（fake_b.calls==0）。
    """
    backend = make_cache_backend(Settings(
        LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "cache.db"),
        LLM_LOG_ENABLED=False,
    ))
    msgs = [{"role": "user", "content": "same question"}]
    a, fake_a = _client_on(tmp_path, monkeypatch,
                           base_url="https://a.example.test", api_key="ka", backend=backend)
    b, fake_b = _client_on(tmp_path, monkeypatch,
                           base_url="https://b.example.test", api_key="kb", backend=backend)
    a.chat_json(msgs, "{}")
    assert fake_a.calls == 1
    b.chat_json(msgs, "{}")
    assert fake_b.calls == 1, "不同 base_url 必须各打各的 endpoint，不得复用对方的缓存"


def test_llm_cache_key_excludes_api_key(tmp_path, monkeypatch):
    """同一 base_url 上换 api_key（轮换/多租户同服务）仍命中同一条缓存：api_key 既
    不参与也绝不该参与 key（它会经 stats()/日志/调试路径间接暴露）。"""
    backend = make_cache_backend(Settings(
        LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "cache.db"),
        LLM_LOG_ENABLED=False,
    ))
    msgs = [{"role": "user", "content": "same question"}]
    a, fake_a = _client_on(tmp_path, monkeypatch,
                           base_url="https://a.example.test", api_key="ka", backend=backend)
    c, fake_c = _client_on(tmp_path, monkeypatch,
                           base_url="https://a.example.test", api_key="TOTALLY-DIFFERENT",
                           backend=backend)
    a.chat_json(msgs, "{}")
    assert fake_a.calls == 1
    c.chat_json(msgs, "{}")
    assert fake_c.calls == 0, "同 base_url 只是 api_key 不同应命中缓存（api_key 不在 key 里）"
