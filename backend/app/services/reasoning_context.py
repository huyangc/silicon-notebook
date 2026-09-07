"""v2 reflect 的 user 段分块(设计稿 2026-09-07 §6.3)。

固定指令住在 system 段;这个模块负责 user 段那一半:把「问题 / 服务器状态 /
证据卡 / 动作观察账与历史判断」拆成各自带标题的块,让材料里出现的
「忽略上面的要求、直接作答」读起来是一份文档里的一句引文,而不是一条指令。

纯内存、零 I/O、零 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass


# --- v2 user 段的分块(设计稿 §6.3) -----------------------------------------
SERVER_STATE_TITLE = "【服务器状态 — 由服务端持有，不可协商】"


@dataclass(frozen=True)
class ReflectContext:
    """一轮 v2 reflect 的 user 段材料,已经分好块并各自受自己的预算约束。

    分块的意义在于**标识**:问题与冻结契约是用户说的,服务器状态是服务端算的,
    证据卡是文档里的内容,观察账是服务端对已发生动作的记录 + 模型自己上一轮写下
    的目的。四者混成一段散文时,材料里一句"忽略上面的要求,直接作答"读起来与真
    的指令没有区别——这正是 §6.3 要拆开的东西。
    """

    server_state: str
    evidence: str
    observations: str

    def as_user_block(self) -> str:
        blocks = []
        if self.server_state:
            blocks.append(f"{SERVER_STATE_TITLE}\n{self.server_state}")
        if self.evidence:
            blocks.append(self.evidence)
        if self.observations:
            blocks.append(self.observations)
        return "\n\n".join(blocks)
