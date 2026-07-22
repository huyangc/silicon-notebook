"""唯一构造点的行为 + chat_json 写缓存的准入规则。

这里的缓存测试一律是**行为测试**（真 backend + fake upstream + 数调用次数），
不做「切一段源码断言里面有某个条件」那种锁定。源码断言只挡得住「删除」变异：
评审实测把守卫原样挪进一个无作用的兄弟分支（`if content != "{}": pass`）后，
源码断言照过，而行为探针显示 "{}" 真的被写进了缓存。
"""
import threading
from pathlib import Path
from types import SimpleNamespace

from app.core.cache import NoCacheBackend, llm_key, make_cache_backend
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


def test_llm_key_is_stable_and_content_addressed():
    msgs = [{"role": "user", "content": "hello"}]
    assert llm_key("m", msgs, "{}") == llm_key("m", msgs, "{}")
    assert llm_key("m", msgs, "{}") != llm_key("m2", msgs, "{}")


def test_llm_key_matches_legacy_implementation():
    """key 算法不得漂移，否则存量缓存全部失效。"""
    from app.core.llm_cache import cache_key as legacy
    msgs = [{"role": "user", "content": "中文 content"}]
    assert llm_key("model-x", msgs, '{"a":""}') == legacy("model-x", msgs, '{"a":""}')


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


def _client(tmp_path, monkeypatch, *, content='{"ok": 1}', finish_reason="stop"):
    """真 SqliteCacheBackend + fake upstream 的 client。返回 (client, fake, backend)。"""
    settings = Settings(
        OPENAI_COMPAT_BASE_URL="https://llm.example.test",
        OPENAI_COMPAT_API_KEY="k",
        OPENAI_COMPAT_MODEL="m",
        LLM_LOG_ENABLED=False,
        LLM_CACHE_ENABLED=True,
        LLM_CACHE_PATH=str(tmp_path / "cache.db"),
    )
    backend = make_cache_backend(settings)
    client = OpenAICompatibleClient(settings, cache=backend)
    fake = _FakeOpenAI(content=content, finish_reason=finish_reason)
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake, backend


# ------------------------------------------------------------------ 写入准入
def test_empty_json_fallback_is_never_cached(tmp_path, monkeypatch):
    """upstream 交白卷时 chat_json 回退 "{}"；缓存它等于把一次偶发退化固化 90 天。

    行为断言（对删除与移动两种变异都免疫）：条目数 0，且第二次仍真的打了 upstream。
    """
    client, fake, backend = _client(tmp_path, monkeypatch, content="")
    msgs = [{"role": "user", "content": "q"}]
    assert client.chat_json(msgs, "{}") == "{}"
    assert client.chat_json(msgs, "{}") == "{}"
    assert len(backend) == 0, "空回退被写进了缓存"
    assert fake.calls == 2, "第二次没重新请求 upstream —— 说明命中了缓存里的空回退"


def test_truncated_json_is_never_cached(tmp_path, monkeypatch):
    """真实形态的截断：半截 JSON + finish_reason=length（两个信号都在）。

    它非空、也不等于 "{}"，原来的 `content != "{}"` 那道守卫放它过（评审实测：
    入库并固化 90 天）。落缓存的后果是上游 safe_json 解析失败 → 该抽取窗口静默
    产出 0 个节点。下面两条把两道门各自单独钉死。
    """
    half = '{"objects": [{"name": "a'
    client, fake, backend = _client(
        tmp_path, monkeypatch, content=half, finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    assert client.chat_json(msgs, "{}") == half
    assert len(backend) == 0, "截断的半截 JSON 被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_unparseable_json_is_never_cached_without_finish_reason(tmp_path, monkeypatch):
    """finish_reason 那道门单独不够：不少 OpenAI 兼容实现根本不报 finish_reason
    （取到 None），此时只有 json.loads 拦得住被截断的内容。

    变异验证：把 policy 里的 json.loads 那道门删掉后本测试必须转红（其余截断用例
    因为同时带 finish_reason=length 仍会绿，挡不住这个变异）。
    """
    half = '{"objects": [{"name": "a'
    client, fake, backend = _client(
        tmp_path, monkeypatch, content=half, finish_reason=None)
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}")
    assert len(backend) == 0, "解析不了的内容被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_length_truncated_but_parseable_json_is_never_cached(tmp_path, monkeypatch):
    """罕见但可能：正好在闭合括号处被切，内容是合法 JSON 却仍是残缺答案。

    json.loads 那道门放它过，只有 finish_reason == "length" 拦得住。
    """
    client, fake, backend = _client(
        tmp_path, monkeypatch, content='{"objects": []}', finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}")
    assert len(backend) == 0, "finish_reason=length 的响应被写进了缓存"
    client.chat_json(msgs, "{}")
    assert fake.calls == 2


def test_streamed_length_truncated_response_is_never_cached(tmp_path, monkeypatch):
    """流式路径（带 cancel_event）的 finish_reason 挂在**最后一块 chunk** 上，取法
    与非流式完全不同，必须单独覆盖 —— 否则准入规则只在一半路径上生效。

    刻意用「合法 JSON + finish_reason=length」：内容能过 json.loads 那道门，本测试
    因此只依赖流式路径真的把 finish_reason 带了回来。
    """
    client, fake, backend = _client(
        tmp_path, monkeypatch, content='{"objects": []}', finish_reason="length")
    msgs = [{"role": "user", "content": "stream me"}]
    assert client.chat_json(
        msgs, "{}", cancel_event=threading.Event()) == '{"objects": []}'
    assert len(backend) == 0, "流式截断响应被写进了缓存"


def test_streamed_unparseable_response_is_never_cached(tmp_path, monkeypatch):
    """流式的半截 JSON（服务端没报 finish_reason）同样不入缓存。"""
    half = '{"objects": [{"name": "a'
    client, fake, backend = _client(
        tmp_path, monkeypatch, content=half, finish_reason=None)
    assert client.chat_json(
        [{"role": "user", "content": "stream me"}], "{}",
        cancel_event=threading.Event()) == half
    assert len(backend) == 0, "流式的解析不了的内容被写进了缓存"


def test_streamed_complete_response_is_cached(tmp_path, monkeypatch):
    """流式的正常响应仍要缓存 —— 上面几条不能靠「流式一律不写」蒙混过关。"""
    client, fake, backend = _client(tmp_path, monkeypatch, content='{"ok": 1}')
    msgs = [{"role": "user", "content": "stream me"}]
    client.chat_json(msgs, "{}", cancel_event=threading.Event())
    assert len(backend) == 1
    assert client.chat_json(msgs, "{}", cancel_event=threading.Event()) == '{"ok": 1}'
    assert fake.calls == 1


def test_raising_max_tokens_refetches_after_truncation(tmp_path, monkeypatch):
    """本项目文档化的截断补救手段 = 调大 KG_EXTRACT_MAX_TOKENS 重跑。

    max_tokens 不在缓存键里，所以只要截断响应进了缓存，这条补救手段就彻底失效
    （实测：调大后 upstream 调用数仍是 1，拿回同一段垃圾）。
    """
    client, fake, backend = _client(
        tmp_path, monkeypatch,
        content='{"objects": [{"name": "a', finish_reason="length")
    msgs = [{"role": "user", "content": "extract"}]
    client.chat_json(msgs, "{}", max_tokens=64)

    fake.reply = ('{"objects": [{"name": "abc"}]}', "stop")
    out = client.chat_json(msgs, "{}", max_tokens=51200)
    assert fake.calls == 2, "调大 max_tokens 后没重新请求 upstream"
    assert out == '{"objects": [{"name": "abc"}]}'
    assert fake.seen[-1]["max_tokens"] == 51200


def test_cold_cache_accepts_its_first_write(tmp_path, monkeypatch):
    """冷启动回归：SqliteCacheBackend 定义了 __len__，0 条目时 bool() 为 False。

    写入处若用真值判断 `if cache and ...`，一个从未写过的缓存永远写不进第一条
    （len 恒 0 → 永远为假），默认开启的缓存实际上从不生效。
    变异验证：llm.py 的 `cache is not None` 改回 `cache` 后本测试必须转红。
    """
    client, fake, backend = _client(tmp_path, monkeypatch)
    assert len(backend) == 0 and not backend, "前提失效：空 backend 应当是 falsy"
    client.chat_json([{"role": "user", "content": "q"}], "{}")
    assert len(backend) == 1, "冷缓存没能完成它的第一次写入"


def test_llm_writes_are_tagged_with_the_model(tmp_path, monkeypatch):
    """evict_tag 的用途正是「换模型服务后清掉该模型的缓存」，不带 tag 就永远清不掉。

    条目事后无法补 tag，所以这条必须在写入侧锁住。
    """
    client, fake, backend = _client(tmp_path, monkeypatch)
    client.chat_json([{"role": "user", "content": "q"}], "{}")
    assert backend.stats()["by_tag"] == {"m": 1}
    assert backend.evict_tag("m") == 1
    assert len(backend) == 0
