"""唯一构造点的行为。换缓存组件时只改 make_cache_backend，调用方零改动。"""
from app.core.cache import NoCacheBackend, llm_key, make_cache_backend
from app.core.config import Settings


def test_disabled_returns_noop(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=False, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    assert isinstance(make_cache_backend(s), NoCacheBackend)


def test_enabled_returns_working_backend(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    backend = make_cache_backend(s)
    backend.put("k", "v", tag="m")
    assert backend.get("k") == "v"


def test_relative_path_is_anchored_to_repo_root(tmp_path):
    """相对路径锚定逻辑归工厂所有——调用方不该知道它。"""
    s = Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=".local/test_cache_factory.db")
    backend = make_cache_backend(s)
    backend.put("k", "v")
    assert backend.get("k") == "v"


def test_llm_key_is_stable_and_content_addressed():
    msgs = [{"role": "user", "content": "hello"}]
    assert llm_key("m", msgs, "{}") == llm_key("m", msgs, "{}")
    assert llm_key("m", msgs, "{}") != llm_key("m2", msgs, "{}")


def test_llm_key_matches_legacy_implementation():
    """key 算法不得漂移，否则存量缓存全部失效。"""
    from app.core.llm_cache import cache_key as legacy
    msgs = [{"role": "user", "content": "中文 content"}]
    assert llm_key("model-x", msgs, '{"a":""}') == legacy("model-x", msgs, '{"a":""}')


def test_empty_json_fallback_is_never_cached():
    """llm.py 的 `content != "{}"` 是防退化固化的关键守卫。

    输出预算烧光时 chat_json 会落到 "{}" 回退；缓存它等于把一次偶发退化永久
    固化在这条 prompt 上。本测试锁定该条件的存在。
    变异验证：删掉 llm.py 里的 `and content != "{}"` 后，本测试必须转红。
    """
    import inspect

    from app.core.llm import OpenAICompatibleClient

    src = inspect.getsource(OpenAICompatibleClient.chat_json)
    # 先切到写入块，再断言条件——避免 [\s\S]*? 越过块边界匹配到别处。
    idx = src.find("cache.put(")
    assert idx != -1, "chat_json 里找不到缓存写入点"
    window = src[max(0, idx - 400):idx]
    assert 'content != "{}"' in window, (
        "缓存写入处丢失了空响应守卫：'{}' 回退会被固化"
    )
