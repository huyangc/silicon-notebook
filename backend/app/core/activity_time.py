"""``list_user_activity`` 的时间边界归一——SQLite 与 PostgreSQL **共用一份**。

放在 ``app.core`` 的理由与 ``query_syntax`` 完全相同:两个存储适配器都要用它,
而它们互相 import 不了(store 不判断方言、也不 import 对侧 adapter)。

写成共享函数不是风格问题。两侧原本各自解释这三个参数:SQLite 把它们当**裸文本**
和 ``created_at`` 比,PostgreSQL 先解析成 aware 时刻再按 ``timestamptz`` 比。同一个
``"2026-07-31T16:00:00.000Z"``(浏览器 ``toISOString()`` 的原样输出)因此在两个后端
选出不同的行集,而两份测试恰好各喂各的输入形态,这条分叉一个断言都不会红。

契约(两个后端逐字相同):

* 入参是一个 ISO 8601 时刻串,返回 aware 的 UTC ``datetime``;调用方再把它渲染成
  本后端的比较形态(SQLite 交给 ``julianday()``,PostgreSQL 直接绑定)。
* **不带 UTC offset 的裸时间按 UTC 解释**,不是本机时区。这一条不是随手选的:
  SQLite 侧的比较形态是 ``julianday()``,而 SQLite 的日期函数正是把裸串当 UTC 读;
  更要命的是 ``before_ts`` 游标原样回传的是行自己的 ``created_at``,而历史行就是裸
  naive 的("2026-07-17T15:19:42")。把它当本机时间平移一次,
  ``julianday(created_at) = julianday(:before_ts)`` 这条并列判据就再也命中不了它
  自己那一行,翻页会跳行。裸串按 UTC 读则 ``julianday`` 前后逐位等值、精确往返。
* 畸形输入**抛 ``ValueError``**——两个后端同一种异常,不静默忽略。悄悄丢掉一个游标
  会让翻页永远停在第一页(客户端死循环);悄悄丢掉一个 since/until 会返回比请求更宽
  的窗口,而调用方以为自己限定了范围。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# 不可解析(SQLite 的空串/脏值)或 NULL(PostgreSQL 的 ``ask_jobs.created_at``,那一列
# 在 schema 里可空)的 ``created_at`` 在**排序轴**上的替身——两个后端共用同一个字面量。
#
# 三处必须是同一个值才闭环:① SQL 的 ``COALESCE`` 兜底 ② Python 归并键的兜底
# ③ **游标回传的 ts**。前两处单独看,用「julianday 0.0」或 ``'-infinity'`` 这类值当兜底
# 更自然;但它们都**写不成 ISO 时刻串**,而游标只能回传 ISO。回传不回来的后果不是
# 少一行:``next_cursor`` 原样发出一个 ``parse_activity_instant`` 解析不了的值,下一页
# 就直接抛 ValueError(实测 3 条 ``created_at=''`` 的 ask + limit=2:第一页正常并给出
# ``cursor {'ts': ''}``,第二页 500)。
#
# 年 1 是 Python ``datetime`` 的下界,也远早于任何真实数据:它排在所有真实时刻之后
# (DESC 序的最末),同时 ``julianday('0001-01-01T00:00:00+00:00')`` 与
# ``datetime.fromisoformat`` 都逐位接受它,于是「行的排序值」与「游标回传值」精确往返,
# 并列的那批行再按 id 定序,翻页不跳行也不重发。
UNRESOLVED_INSTANT_ISO = "0001-01-01T00:00:00+00:00"

# 同一个哨兵的 ``datetime`` 形态(PostgreSQL 侧 Python 归并键直接比较原生 datetime)。
UNRESOLVED_INSTANT = datetime(1, 1, 1, tzinfo=timezone.utc)


def cursor_instant_text(value: object) -> str:
    """把一行的 ``created_at`` 折成**可往返**的游标 ts。

    可解析就原样回传(``julianday()`` / ``timestamptz`` 对它与对列值逐位相等,下一页的
    并列判据因此精确命中这一行);不可解析或 NULL 则回传哨兵——它正是该行在 SQL 里
    实际参与排序的那个值,所以游标依然指向同一个位置。
    """
    if value is None:
        return UNRESOLVED_INSTANT_ISO
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        parse_activity_instant(str(value), field="cursor")
    except ValueError:
        return UNRESOLVED_INSTANT_ISO
    return str(value)


def parse_activity_instant(value: str, *, field: str) -> datetime:
    """把一个活动流时间参数解析成 aware 的 UTC 绝对时刻。

    ``field`` 只进异常文案,便于调用方分辨是哪个参数畸形。畸形输入抛 ``ValueError``
    (见模块 docstring:两个后端统一抛同一种异常,绝不静默忽略)。
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field} is not an ISO 8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 裸时间按 UTC 读(不是本机时区),与 SQLite 日期函数对裸串的解释一致。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def activity_retention_window(
    value: str | datetime, *, retention_days: int
) -> tuple[datetime, datetime]:
    """Return the normalized deletion instant and its configured expiry.

    Notebook deletion is the one point at which the live aggregate still
    exists and can be projected without races.  Both database adapters call
    this helper before entering their archive ``INSERT ... SELECT`` statements
    so a day means one exact 24-hour UTC duration on both backends.
    """
    if isinstance(value, datetime):
        deleted_at = value
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        deleted_at = deleted_at.astimezone(timezone.utc)
    else:
        deleted_at = parse_activity_instant(str(value), field="deleted_at")
    return deleted_at, deleted_at + timedelta(days=int(retention_days))
