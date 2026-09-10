"""PR-5 实验通道共用的运行 manifest:纯构造 + 闭集 + 隐私断言。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§8.2/§13(交付物「冻结 manifest」);实施真源:
`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md` Q9(键集
与落点)、T-EX1(本模块的要点与验收)。

**这个模块纯逻辑、零 I/O、零 `git`、零 `Settings`**:`code_sha` / 时间戳 / 语料签名
全部由调用方(`scripts/reflect_shadow_rig.py` 三条通道各自的收尾处)算好后当关键
字参数传入——这里只管「一份 manifest 长什么样才算合法」,不管怎么把它算出来或
写到哪个文件。

`MANIFEST_KEYS` 是三条通道(E1/E2/E3)**共用**的顶层键闭集;每条通道再各自要求
其中几个键必须**在场**(值可以是 `None`,例如 E3 的 `arm_order_seed`——M4 明确
臂序按 run 无种子随机,`None` 合法,但键本身不许被漏写)。

`assert_manifest_values` 复用 `app.domain.reasoning_trace_stats._is_short_code`
——**同一个**短码判据(字符集、长度上限),不重写一份正则。但校验的**结构**与
同模块的 `assert_projection_values` 不同,是刻意的:那边的字典值只许数值(给
`actions_by_type` 这类计数表用),这边的字典天然需要「短码 → 短码」——
`corpus_signature_by_cell`(`scripts/reflect_shadow_rig.py` 的 `_ab_corpus_signature`
返回十六位十六进制摘要)、`intent_contract_digest_by_question`、
`optimization_by_arm` 都是「格/题/臂 → 一个短码摘要」的形状。允许的值形状因此是
一棵递归树:标量(`bool` / `int` / `float` / `None` / 短码字符串)、这棵树的列表、
或者键为短码字符串、值递归属于同一棵树的字典。生产凭据、`postgresql://` 连接串、
`http(s)://` 地址、题面原文——这些形状里含有短码字符集(`[A-Za-z0-9_:\\-.+→]`)
之外的字符(空白、`/`、`@`、中文标点等),在这一层递归到底都过不了 `_is_short_code`,
不需要为每一种红线单独写判断,与投影行的隐私守卫共用同一把尺子。
"""
from __future__ import annotations

from typing import Any, Mapping

from app.domain.reasoning_trace_stats import _is_short_code

#: 三条实验通道的短码(计划 §0 的三层)。
CHANNELS: tuple[str, ...] = ("e1", "e2", "e3")

#: manifest 顶层键闭集(计划 Q9)。三条通道共用同一张表——具体某条通道用不到
#: 的键就不写进它自己的 manifest,而不是给每条通道单开一张闭集。
MANIFEST_KEYS: frozenset[str] = frozenset({
    "code_sha", "channel", "arms", "optimization_by_arm", "common_baseline",
    "corpus_signature_by_cell", "intent_contract_digest_by_question",
    "model_contract", "seed", "arm_order_seed", "order", "matrix", "budgets",
    "started_at", "finished_at", "stopped_by_budget",
})

#: 每条通道各自的必填键(T-EX1 要点)。`channel` 本身不在这张表里——它是路由
#: 到这张表的钥匙,`build_manifest` 单独校验它是否在场、是否属于 `CHANNELS`。
#:
#: * E1 —— `seed`(§9.1 区组随机必须有种子,M4「E1 的区组随机则必须有种子」)
#:   + `matrix`(Q9 对 `matrix` 的定义是「题/档/臂/轮/状态点各自的计数」,E1 的
#:   「档」就是三个长度档,因此三个长度档的计数落在 `matrix` 里);
#: * E2 —— `corpus_signature_by_cell`(E2 的 case 集按语料格 A_nokg/B_kg 划分、
#:   不含任何数据库 id,Q5「一个 case 不含任何数据库 id」;这个格→签名短码字典
#:   因此就是 T-EX1 原文说的「case_set_digest」——case 集本身就是按语料格分的,
#:   它的指纹自然落在语料签名这个既有槽位里,不必另开一个顶层键)
#:   + `matrix`(状态点表:三个状态点各自的计数);
#: * E3 —— `intent_contract_digest_by_question`(既有 `ab` 每题一个冻结意图
#:   契约的摘要)+ `arm_order_seed`(允许 `None`,但键必须在)。
REQUIRED_KEYS_BY_CHANNEL: Mapping[str, frozenset[str]] = {
    "e1": frozenset({"seed", "matrix"}),
    "e2": frozenset({"corpus_signature_by_cell", "matrix"}),
    "e3": frozenset({"intent_contract_digest_by_question", "arm_order_seed"}),
}


def assert_manifest_closed(row: Mapping) -> None:
    """manifest 的形状自检:闭集外的键当场报错。rig 写文件之前调它一次。"""
    extra = set(row) - MANIFEST_KEYS
    if extra:
        raise ValueError(
            "manifest carries keys outside MANIFEST_KEYS: "
            + ", ".join(sorted(extra))
        )


def _is_manifest_scalar(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    return _is_short_code(value)


def _assert_manifest_value(path: str, value: object) -> None:
    if _is_manifest_scalar(value):
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_manifest_value(f"{path}[{index}]", item)
        return
    if isinstance(value, Mapping):
        for inner_key, inner_value in value.items():
            if not _is_short_code(inner_key):
                raise ValueError(
                    f"manifest value at {path!r} carries a non-short-code "
                    f"dict key: {inner_key!r}"
                )
            _assert_manifest_value(f"{path}.{inner_key}", inner_value)
        return
    raise ValueError(
        f"manifest value at {path!r} has an unsupported shape: {value!r}"
    )


def assert_manifest_values(row: Mapping) -> None:
    """manifest 的**值**形状自检(与 `assert_manifest_closed` 互补)。

    允许的值形状(闭集,不允许别的,递归):

    - 标量:`bool` / `int` / `float` / `None`,或一个短码字符串;
    - 上述标量的列表(`arms` / `order` 这类枚举);
    - 键为短码字符串、值递归属于这棵树的字典(`matrix` / `budgets` 这类计数表,
      也包括 `corpus_signature_by_cell` / `intent_contract_digest_by_question` /
      `optimization_by_arm` 这类「格/题/臂 → 短码摘要」表)。

    往任何一个允许的键里塞一段题面原文、一个 `postgresql://` 连接串、一个
    `http(s)://` 地址,都会在递归到底时撞上 `_is_short_code` 的字符集/长度门槛
    ——不需要为每一种红线单独写判断。
    """
    for key, value in row.items():
        _assert_manifest_value(key, value)


def build_manifest(**facts: Any) -> dict[str, Any]:
    """从调用方传入的事实构造一份闭集校验过的 manifest。

    调用方(三条通道各自的 rig 收尾处)负责算好每一项事实——`code_sha` 来自
    一次 `git rev-parse HEAD`,时间戳来自调用方自己的时钟,语料签名/意图摘要
    来自各自通道的既有函数。这个函数只管形状:缺了这条通道的必填键、多了闭集
    外的键、或者哪个值形状不对,一律在这里响亮失败,不静默兜底、不猜省略值。
    """
    row: dict[str, Any] = dict(facts)
    assert_manifest_closed(row)
    if "channel" not in row:
        raise ValueError("manifest is missing required key(s): channel")
    channel = row["channel"]
    if channel not in CHANNELS:
        raise ValueError(
            f"manifest channel must be one of {sorted(CHANNELS)}, got {channel!r}"
        )
    missing = REQUIRED_KEYS_BY_CHANNEL[channel] - set(row)
    if missing:
        raise ValueError(
            f"manifest for channel {channel!r} is missing required key(s): "
            + ", ".join(sorted(missing))
        )
    assert_manifest_values(row)
    return row
