"""Z1: 启动路径的连接池预算告警——只告警,不拒启。

`_pool_budget_warning` 是纯函数(只读几个 settings 字段),这里直接单测它,
不经由完整的 `run_startup`/真实数据库构造。`run_startup` 侧的接线只是一次
`logger.warning(...)`调用,见 `startup_warmup.run_startup` 里紧跟
`validate_process_local_scheduler_deployment()` 之后的那几行。
"""
from types import SimpleNamespace

from app.services.startup_warmup import _pool_budget_warning


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        database_url="postgresql://u:p@localhost:5432/db",
        background_maintenance_concurrency=4,
        background_light_job_concurrency=4,
        kg_job_concurrency=8,
        search_concurrency_limit=4,
        scale_build_concurrency=2,
        notebook_delete_concurrency=1,
        postgres_pool_max_size=10,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_production_defaults_trigger_the_warning():
    """生产默认 4+4+8+4+2+1=23 > postgres_pool_max_size 默认 10——必须触发。
    搜索闸与 scale 构建并发计入(codex #627 R4 P2),批 3·W1 的删除池并发同理
    计入(codex #659 R4):它们各占连接且与维护池独立。"""
    warning = _pool_budget_warning(_settings())
    assert warning is not None
    assert "POSTGRES_POOL_MAX_SIZE=10" in warning
    assert "23" in warning
    assert "24" in warning  # 建议值 = budget + 1


def test_pool_max_strictly_above_budget_is_silent():
    warning = _pool_budget_warning(_settings(postgres_pool_max_size=24))
    assert warning is None


def test_pool_max_exactly_equal_to_budget_still_warns():
    """边界:`<=` 触发,不是 `<`——池子被打满时前台请求已经排不上号了。"""
    warning = _pool_budget_warning(_settings(postgres_pool_max_size=22))
    assert warning is not None


def test_sqlite_backend_is_exempt():
    """postgres_pool_max_size 对 SQLite 后端没有意义,不应误报。"""
    warning = _pool_budget_warning(
        _settings(database_url="sqlite:///./data/app.db")
    )
    assert warning is None


def test_malformed_database_url_does_not_raise():
    """URL 校验是别处的职责;本函数只在能确认是 postgres 时才判定,绝不额外报错。"""
    warning = _pool_budget_warning(_settings(database_url="not a url"))
    assert warning is None


def test_postgres_url_with_missing_pool_fields_does_not_raise():
    """回归(评审变异实证:现有 5 例全部够不到整体 try/except 这道防线——它们要么走
    sqlite 短路提前 return,要么 settings 上四个池字段一应俱全)。这里造一个**只带
    database_url**、缺 background_maintenance_concurrency/background_light_job_concurrency/
    kg_job_concurrency/postgres_pool_max_size 四个池字段的 postgres SimpleNamespace——
    docstring 点名的正是这种最小 double,若整体 try/except 被去掉,读任一缺失字段都会
    在 AttributeError 上原样抛出而不是被吞成 None。"""
    warning = _pool_budget_warning(
        SimpleNamespace(database_url="postgresql://u:p@localhost:5432/db")
    )
    assert warning is None


def test_search_and_scale_consumers_alone_can_trigger_the_warning():
    """codex #627 R4 P2 的精确场景:三个维护池各 1、池 5——旧口径(1+1+1=3 < 5)
    误判安全,而 4 路搜索 + 2 路 scale 构建就能把需求推到 9。新口径必须告警。"""
    warning = _pool_budget_warning(
        _settings(
            background_maintenance_concurrency=1,
            background_light_job_concurrency=1,
            kg_job_concurrency=1,
            postgres_pool_max_size=5,
        )
    )
    assert warning is not None
    assert "搜索并发(4)" in warning
    assert "scale 构建并发(2)" in warning
