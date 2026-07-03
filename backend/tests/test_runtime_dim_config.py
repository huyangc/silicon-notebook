"""T0:EMBED_RUNTIME_DIM 配置层(运行时向量截断 4096→1024 项目地基)。

pydantic-settings v2 双坑专项:validation_alias 才能读 env;加 alias 后
kwarg 构造必须用 alias 名(Settings(EMBED_RUNTIME_DIM=...))。
计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T0。
"""
import pytest

from app.core.config import Settings


def test_default_off():
    s = Settings()
    assert s.embed_runtime_dim == 0            # 默认关闭,行为零变化


def test_env_alias_reads(monkeypatch):
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "1024")
    monkeypatch.setenv("EMBED_DIM", "4096")
    s = Settings()
    assert s.embed_runtime_dim == 1024
    assert s.embed_dim == 4096


def test_alias_kwarg_construction():
    """alias 后 kwarg 构造必须用 alias 名(pydantic env 别名坑的另一半)。"""
    s = Settings(EMBED_RUNTIME_DIM=512, EMBED_DIM=4096)
    assert s.embed_runtime_dim == 512


def test_runtime_dim_exceeding_storage_dim_hard_errors():
    with pytest.raises(Exception, match="EMBED_RUNTIME_DIM"):
        Settings(EMBED_RUNTIME_DIM=4096, EMBED_DIM=1024)


def test_negative_hard_errors():
    with pytest.raises(Exception, match="EMBED_RUNTIME_DIM"):
        Settings(EMBED_RUNTIME_DIM=-1)


def test_equal_dims_allowed():
    """runtime == storage 合法(等价于截断无操作,但显式配置不算错)。"""
    s = Settings(EMBED_RUNTIME_DIM=1024, EMBED_DIM=1024)
    assert s.embed_runtime_dim == 1024
