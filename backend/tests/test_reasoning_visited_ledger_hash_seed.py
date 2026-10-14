"""已展开节点账目跨 PYTHONHASHSEED 逐字节一致(真子进程契约)。

同一组 expand_graph 动作在两个哈希种子下各跑一个全新进程,reflect prompt 里的
账目必须逐字节相同。账目里的节点顺序若取自 `set`,字符串哈希随种子变,同一个
run 在不同进程里会得到不同的 prompt(破坏 LLM 缓存重放与可复现性);进程内的顺序
用例只能概率性抓到它,这里用固定的两个种子把它钉死。

变异:把 `ReasoningRetriever._new_run_state` 里的 `visited` 改回 `set()`、
`_action_expand_graph` 的落账改回 `.add` ⇒ 两个种子的输出不同 ⇒ 红。
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.child_env_testkit import BYTECODE_ENV

ROOT = Path(__file__).resolve().parents[2]

_ORDER = ("n-07", "n-31", "n-02", "n-19", "n-11", "n-25", "n-03", "n-17")

# 子进程:真 SQLiteRepository + 真 ReasoningRetriever.run,只把检索与模型换成桩,
# 每轮 reflect 展开 _ORDER 里的下一个节点;打印最后一轮 reflect 输入里的账目行。
_CHILD = r"""
import json, sys
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import NeighborExpansion, RetrievedKnowledge
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

repo = SQLiteRepository(Settings())
bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
nb = repo.create_notebook(NotebookCreate(name="nb"))
repo.store_kg(nb.id, None, [
    {"local_id": "C1", "object_type": "claim",
     "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    {"local_id": "P1", "object_type": "procedure",
     "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
], [
    {"source_local_id": "C1", "target_local_id": "P1",
     "edge_type": "depends_on", "evidence": []},
])
repo.settings.reasoning_stale_limit = 50
seq = {"n": 0}

def fake_neighbors(self, n, oid, edge_type=None, direction="both"):
    seq["n"] += 1
    key = "nb%d" % seq["n"]
    return NeighborExpansion([RetrievedKnowledge(
        object_id=key, object_type="claim", payload={"name": key})])

ReasoningRetriever.search = (
    lambda self, n, q, types=None, prefer="balanced": [])
ReasoningRetriever.neighbors = fake_neighbors
reflects = [{"next_action": "expand_graph", "expand": {"object_id": oid}}
            for oid in sys.argv[1:]]
reflects.append({"next_action": "answer", "sufficient": True})
prompts = []

class _Llm:
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "q"}]})
        prompts.append(messages[-1]["content"])
        return json.dumps(reflects.pop(0))

bind_chat_client(repo, "reasoning_agent", _Llm())
ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "q", "")
marker = "勿重复 expand_graph 请求它们:"
lines = [l for l in prompts[-1].splitlines() if marker in l]
if not lines:
    raise SystemExit("no ledger line in %d prompts; tail=%r" % (
        len(prompts), prompts[-1][-400:]))
sys.stdout.write(lines[0])
"""


def _ledger_line_under(seed: str, tmp_path: Path) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "backend"),
        "PYTHONHASHSEED": seed,
        "SILICON_NOTEBOOK_ENV_FILE": "",
        "DATABASE_URL": f"sqlite:///{tmp_path / ('t-' + seed + '.db')}",
        "SILICON_NOTEBOOK_STORAGE_DIR": str(tmp_path / ("s-" + seed)),
        "LLM_LOG_ENABLED": "false",
        "EMBED_DIM": "16",
        **BYTECODE_ENV,
    }
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, *_ORDER],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.process_contract
def test_visited_ledger_is_identical_across_hash_seeds(tmp_path):
    first = _ledger_line_under("1", tmp_path)
    second = _ledger_line_under("2", tmp_path)
    assert first == second
    assert first.endswith("请求它们: " + ", ".join(_ORDER) + "）")
