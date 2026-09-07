# backend/tests/test_mount_sql_contract.py
"""挂载有效性谓词(`MOUNT_VALID_EXPR`)唯一定义点的结构性守卫。

`sqlite/mount_sql.py` 与 `postgres/mount_sql.py` 是这条谓词的一对唯一定义点
(互为镜像,双后端同修——见两份文件的模块 docstring)。参与集解析、KG 可用性门、
summary 投影、社区扩展、晋升目标五处消费点一律 import 这两份文件里的常量,不许
另抄一份。这份契约钉的是 A8:「公共库 ∨ 同 owner」这条最容易被顺手复刻的分支
(`tier='base' OR created_by=created_by`)只应出现在这两个定义点,出现在第三处就是
一条静默分叉的挂载判定——今天语义相同,某天这里扩了一个 OR 分支,复刻处就漏掉。

判据形状从 `MOUNT_VALID_EXPR` 运行时取值里按两个锚点(`tier=base` 与
`created_by=a.created_by`)提取:锚点**之间**的文本自动跟随谓词变化;锚点本身
(列名、别名)改动时提取失败并响亮报错,需要人工重估这条契约。守卫只覆盖**逐字
复刻**(同别名、同 OR 顺序);换别名或交换 OR 两边的语义复刻不在覆盖面内,与
`test_access_sql_contract.py` 同一类文本守卫的既有边界一致。
"""
import re
from pathlib import Path

from app.repositories.sqlite.mount_sql import MOUNT_VALID_EXPR as SQLITE_MOUNT_VALID_EXPR
from app.repositories.postgres.mount_sql import (
    MOUNT_VALID_EXPR as PG_MOUNT_VALID_EXPR,
)

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# 两个唯一定义点自身(互为镜像),按相对路径豁免——按文件名豁免会让将来任何
# 同名模块(比如 services/kg/mount_sql.py)里的复刻被静默放行。
_DEFINITION_POINTS = {
    "repositories/sqlite/mount_sql.py",
    "repositories/postgres/mount_sql.py",
}

_SHAPE_PATTERN = re.compile(r"tier=base.*?created_by=a\.created_by")


def _collapsed(text: str) -> str:
    """去掉引号与全部空白:让跨行字符串拼接与源码换行都现出连续的 SQL 形状。"""
    return re.sub(r"[\s'\"]+", "", text)


def _mount_valid_shape(expr: str, backend: str) -> str:
    """从某一侧 `MOUNT_VALID_EXPR` 的运行时取值里提取「公共库 ∨ 同 owner」分支的形状。"""
    match = _SHAPE_PATTERN.search(_collapsed(expr))
    assert match is not None, (
        f"{backend} MOUNT_VALID_EXPR 里找不到「tier='base' OR created_by=created_by」"
        "这支——谓词形状变了,先确认这条契约测试是否还有意义,再更新判据"
    )
    return match.group(0)


def test_sqlite_and_postgres_mount_valid_expr_share_the_tier_or_owner_shape():
    """两份镜像在「公共库 ∨ 同 owner」这支上必须逐字相同(双后端同修的可执行证据)。

    两侧各自独立提取再比对:任一侧单独改形状都会红,而不是只有 PG 侧会红。
    """
    sqlite_shape = _mount_valid_shape(SQLITE_MOUNT_VALID_EXPR, "sqlite")
    pg_shape = _mount_valid_shape(PG_MOUNT_VALID_EXPR, "postgres")
    assert sqlite_shape == pg_shape


def test_mount_valid_tier_or_owner_shape_lives_only_in_mount_sql():
    """「公共库 ∨ 同 owner」的挂载有效性形状只许出现在两份 mount_sql.py。

    唯一定义点的价值在于没有第三份复刻:手写一份逐字相同的判定,今天语义相同,
    未来 MOUNT_VALID_EXPR 扩展第五支可挂范围那天就是一条静默分叉的挂载判定。这条
    移动变异守卫保证「把定义搬回消费点」会报红,而不是靠 docstring 清单的自觉。
    """
    pattern = re.compile(re.escape(_mount_valid_shape(SQLITE_MOUNT_VALID_EXPR, "sqlite")))
    offenders = []
    seen_definition_points = set()
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        rel = str(path.relative_to(_BACKEND_APP))
        if rel in _DEFINITION_POINTS:
            seen_definition_points.add(rel)
            continue
        if pattern.search(_collapsed(path.read_text(encoding="utf-8"))):
            offenders.append(rel)
    # 定义点改名/搬家时这条守卫会退化成永真,先钉住两份文件都被扫到过。
    assert seen_definition_points == _DEFINITION_POINTS, (
        f"唯一定义点未被扫到:{sorted(_DEFINITION_POINTS - seen_definition_points)},"
        "mount_sql.py 搬家后请同步 _DEFINITION_POINTS"
    )
    assert offenders == [], (
        f"挂载有效性谓词的「公共库 ∨ 同 owner」形状出现在唯一定义点之外:{offenders}。"
        "请改用 mount_sql.MOUNT_VALID_EXPR / MOUNT_VALID,不要手写内联复刻。"
        "(扫描的是源码全文:注释或 docstring 里逐字引用这条谓词同样会命中,"
        "请改为引用常量名。)"
    )
