"""PR#334:空库(ask_available=False)对 /ask、/ask/stream 的提问被后端以 409 硬拒绝
(权威闸门,挡前端快照陈旧/跨标签/直连旁路)。凡"只为测别的行为(会话生命周期、取消、
流式机制、会话归属…)而对空库发 ask"的用例,先塞一条最小可检索证据,使 ask_available=True。
"""
from __future__ import annotations

_NOW = "2026-07-20T00:00:00"


def seed_ask_evidence(repo, notebook_id: str) -> None:
    """插一个 source + 一个 chunk,使 QueryStore.notebook_has_chunk→True→ask_available=True。

    直插最小行(不跑解析/嵌入),与 test_notebook_ask_availability 的做法一致;id 带
    notebook_id 后缀,便于同一进程内为多个库各 seed 一条不撞主键。
    """
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"seed-src-{notebook_id}", notebook_id, "seed", "document",
             "ready", _NOW, _NOW),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
            "VALUES (?,?,?,?,?)",
            (f"seed-chunk-{notebook_id}", notebook_id,
             f"seed-src-{notebook_id}", "seed text", _NOW),
        )
