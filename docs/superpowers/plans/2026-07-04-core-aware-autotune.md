# 按核数自动调参(core-aware autotune)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 部署时自动按机器核数推导「核绑定」并行旋钮的默认值(ANN 建索引线程数、BLAS/OMP 线程、回填进程池),16→64 核零手工改动;服务端绑定与单写绑定旋钮刻意不缩放。

**Architecture:** 混合两组件。组件 1(Python/`config.py`)新增 `kg_cluster_ann_threads`,哨兵 `0`=auto → `min(cpu_count,32)`,接到 `kg_merge` 的 hnswlib `set_num_threads`(解掉硬编码 1),同时覆盖在线服务与离线重建 CLI。组件 2(shell/`scripts/autotune.sh`)在启动脚本 source `.env` 后、python 前设置 `OMP/OPENBLAS/MKL_NUM_THREADS`(必须早于 numpy 导入)。显式 env 永远优先;`AUTOTUNE=0` 关闭;启动打印调参报告。

**Tech Stack:** Python 3.13, pydantic-settings v2, hnswlib, pytest, bash(需兼容 macOS bash 3.2)。

## Global Constraints

- **pydantic-v2 alias**:新可配置项**必须**用 `validation_alias="ENV_NAME"`,**不能**用失效的 `Field(env=...)`;之后按字段名构造 Settings 会失效,测试注入用 env(`monkeypatch.setenv`)。
- **显式优先**:自动值只填「未显式设置」的量;任何已设的 env 一律不覆盖。
- **不缩放服务端旋钮**:`KG_EXTRACT_WORKERS` / `KG_JOB_CONCURRENCY` / `EMBED_CONCURRENCY` / `KG_ASK_RESERVE` 与本机核数无关,不纳入自动推导。
- **不动 `--workers 1`**:`scripts/prod.sh` 的单 worker 是既有的有理由决定;多 worker 只作 README 手动 opt-in。
- **纯函数默认安全**:`kg_merge` 的 `ann_threads` 参数默认 `1`,不传即零行为变化。
- **提交文档保持通用**:README/`.env.example` 用产品通用口径,不写本机绝对路径/端口。
- **测试**从 `backend/` 跑:`cd backend && pytest tests/... -v`。
- **每次 commit** 末尾加:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

### Task 1: 新增 `kg_cluster_ann_threads` 配置 + 按核解析

**Files:**
- Modify: `backend/app/core/config.py`(字段加在其它 KG 旋钮附近,如 `kg_job_concurrency` 之后;新增一个 `model_validator(mode="after")`)
- Test: `backend/tests/test_autotune_config.py`(Create)

**Interfaces:**
- Produces: `Settings.kg_cluster_ann_threads: int`(已解析,>=1;env `KG_CLUSTER_ANN_THREADS`,0/未设=auto=`min(cpu_count,32)`)。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_config.py`:

```python
"""按核数解析 kg_cluster_ann_threads:未设→min(cpu_count,32);显式→原样。"""
import os
import pytest
from app.core.config import Settings


def test_ann_threads_auto_from_cpu_count_capped_at_32(monkeypatch):
    monkeypatch.delenv("KG_CLUSTER_ANN_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert Settings().kg_cluster_ann_threads == 32  # min(64, 32)


def test_ann_threads_auto_tracks_small_machines(monkeypatch):
    monkeypatch.delenv("KG_CLUSTER_ANN_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert Settings().kg_cluster_ann_threads == 16  # min(16, 32)


def test_ann_threads_explicit_env_wins(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "4")
    assert Settings().kg_cluster_ann_threads == 4


def test_ann_threads_zero_means_auto(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "0")
    assert Settings().kg_cluster_ann_threads == 8  # 0 sentinel → min(8,32)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_config.py -v`
Expected: FAIL(`AttributeError: 'Settings' object has no attribute 'kg_cluster_ann_threads'`)

- [ ] **Step 3: 加字段 + 解析器**

在 `config.py` 的 KG 旋钮区(`kg_job_concurrency: int = Field(8, env="KG_JOB_CONCURRENCY")` 那一带)加字段:

```python
    # 0 = auto:未显式设置时按核数解析为 min(cpu_count, 32)(见 _resolve_core_bound_defaults)。
    # 这是唯一按本机核数缩放的 KG 旋钮——服务端绑定旋钮(EXTRACT/JOB/EMBED)刻意不跟核数走。
    kg_cluster_ann_threads: int = Field(0, validation_alias="KG_CLUSTER_ANN_THREADS")
```

在其它 `model_validator(mode="after")` 旁(如 `_validate_runtime_dim` 之后)加解析器(pydantic 模型可变,沿用现有 `self.x = ...` 直赋风格):

```python
    @model_validator(mode="after")
    def _resolve_core_bound_defaults(self) -> "Settings":
        """核绑定旋钮的按核数默认。仅在未显式设置(<=0 哨兵)时生效;显式值原样保留。
        封顶 32:HNSW 图构建争用 + rep 矩阵内存带宽,超 ~32 线程收益递减。"""
        if self.kg_cluster_ann_threads <= 0:
            self.kg_cluster_ann_threads = min(os.cpu_count() or 1, 32)
        return self
```

在 `config.py` 顶部 import 区补 `import os`(该文件目前未导入 os):

```python
import os
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_config.py -v`
Expected: PASS(4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_autotune_config.py
git commit -m "$(cat <<'EOF'
feat(config): kg_cluster_ann_threads 按核数自动解析(min(cpu,32),0=auto)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 把 `ann_threads` 穿进 kg_merge,解掉单核 pin

**Files:**
- Modify: `backend/app/services/kg_merge.py`(`_ann_candidates` @178、内层 `_run_shard`、`cluster_seeds` @296/@323)
- Test: `backend/tests/test_autotune_kg_merge.py`(Create)

**Interfaces:**
- Consumes: 无(纯函数,默认 `ann_threads=1`)。
- Produces:
  - `_ann_candidates(seeds, reps, k=5, lo=0.82, max_reps=None, ann_threads=1)`
  - `cluster_seeds(..., ann_threads=1)`(keyword-only 段追加),内部以 `ann_threads=ann_threads` 调 `_ann_candidates`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_kg_merge.py`:

```python
"""_run_shard 用传入的 ann_threads 调 hnswlib.set_num_threads,而非硬编码 1。"""
import numpy as np
import pytest
from app.services import kg_merge


def _reps(names):
    # 造两组明显分离的向量,保证有 ≥1 条近邻候选边(sim≥lo)。
    rng = np.arange(len(names), dtype=np.float32)
    return {n: np.array([1.0, 0.001 * i], dtype=np.float32) for i, n in enumerate(names)}


def test_ann_candidates_passes_thread_count(monkeypatch):
    seen = {}
    real_index = kg_merge.__dict__.get("hnswlib")  # not imported at module top; patch the module

    import hnswlib

    class _SpyIndex(hnswlib.Index):
        def set_num_threads(self, n):
            seen["threads"] = n
            return super().set_num_threads(n)

    monkeypatch.setattr(hnswlib, "Index", _SpyIndex)
    names = [f"c{i}" for i in range(6)]
    kg_merge._ann_candidates(names, _reps(names), k=3, lo=0.0, ann_threads=7)
    assert seen["threads"] == 7


def test_ann_candidates_defaults_to_single_thread(monkeypatch):
    seen = {}
    import hnswlib

    class _SpyIndex(hnswlib.Index):
        def set_num_threads(self, n):
            seen["threads"] = n
            return super().set_num_threads(n)

    monkeypatch.setattr(hnswlib, "Index", _SpyIndex)
    names = [f"c{i}" for i in range(6)]
    kg_merge._ann_candidates(names, _reps(names), k=3, lo=0.0)
    assert seen["threads"] == 1  # 默认零行为变化


def test_cluster_seeds_forwards_ann_threads(monkeypatch):
    captured = {}
    real = kg_merge._ann_candidates

    def spy(*a, **k):
        captured["ann_threads"] = k.get("ann_threads")
        return real(*a, **k)

    monkeypatch.setattr(kg_merge, "_ann_candidates", spy)
    names = ["a", "b", "c"]
    kg_merge.cluster_seeds(
        names, _reps(names), {n: 1 for n in names},
        {n: n for n in names}, set(), set(), ann_threads=5,
    )
    assert captured["ann_threads"] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_kg_merge.py -v`
Expected: FAIL(`TypeError: _ann_candidates() got an unexpected keyword argument 'ann_threads'`)

- [ ] **Step 3: 改 kg_merge**

3a. `_ann_candidates` 签名(kg_merge.py:178-181)追加参数:

```python
def _ann_candidates(seeds: List[str], reps: Dict[str, "np.ndarray"],
                    k: int = 5, lo: float = 0.82,
                    max_reps: int | None = None,
                    ann_threads: int = 1) -> List[tuple]:
```

3b. 内层 `_run_shard` 里(kg_merge.py:203)把硬编码 1 换成入参:

```python
        index.set_num_threads(max(1, ann_threads))
```

(`_run_shard` 是 `_ann_candidates` 的内嵌闭包,直接闭包捕获外层 `ann_threads`,无需再改其签名。)

3c. `cluster_seeds` 的 keyword-only 段(kg_merge.py:296 的 `*` 之后,`rep_ann_max` 旁)追加:

```python
    rep_ann_max: int | None = None,
    ann_threads: int = 1,
```

3d. `cluster_seeds` 内调用点(kg_merge.py:323)透传:

```python
    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo, max_reps=rep_ann_max,
                          ann_threads=ann_threads)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_kg_merge.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 回归 kg_merge 既有测试**

Run: `cd backend && pytest tests/test_cross_doc_merge.py tests/test_unified_kg_repository.py -q`
Expected: PASS(无回归——默认 ann_threads=1 保持旧行为)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg_merge.py backend/tests/test_autotune_kg_merge.py
git commit -m "$(cat <<'EOF'
feat(kg): cluster_seeds/_ann_candidates 支持 ann_threads,解掉 set_num_threads(1) 单核 pin

默认仍为 1(零行为变化);由调用方传入按核数解析的线程数。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: repository 重建调用点接入 settings

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(cluster_seeds 三处调用:约 6305 / 6343 / 6475)
- Test: `backend/tests/test_autotune_wiring.py`(Create)

**Interfaces:**
- Consumes: `Settings.kg_cluster_ann_threads`(Task 1)、`cluster_seeds(..., ann_threads=...)`(Task 2)。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_wiring.py`(复用 `test_rebuild_desc_cache.py` 的 repo/证据构造法,让跨文档合并触发 cluster_seeds):

```python
"""rebuild_unified_kg 把 settings.kg_cluster_ann_threads 透传给 cluster_seeds。"""
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services import kg_merge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _concept(local_id, name, source_title):
    return {
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": source_title, "element_id": "e",
            "element_type": "p", "location_label": "1", "quoted_span": f"span-{name}",
            "confidence": 1.0,
        }],
    }


def test_rebuild_forwards_ann_threads(repo, monkeypatch):
    repo.settings.kg_cluster_ann_threads = 7  # sentinel value

    seen = []
    real = kg_merge.cluster_seeds

    def spy(*a, **k):
        seen.append(k.get("ann_threads"))
        return real(*a, **k)

    monkeypatch.setattr(kg_merge, "cluster_seeds", spy)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 同一归一化概念名,两个来源 → 跨文档合并 → 触发 cluster_seeds。
    repo.store_kg(nb.id, [_concept("l1", "Bandgap Reference", "A")])
    repo.store_kg(nb.id, [_concept("l2", "Bandgap Reference", "B")])
    repo.rebuild_unified_kg(nb.id)

    assert seen, "cluster_seeds 未被调用——检查 rebuild 是否短路"
    assert all(v == 7 for v in seen), f"期望全部透传 7,实得 {seen}"
```

> 注:`store_kg` / `create_notebook` / `rebuild_unified_kg` 若签名与此处不符,以仓库实际签名为准(先 `grep -n "def store_kg\|def create_notebook\|def rebuild_unified_kg" backend/app/services/sqlite_repository.py` 核对,再对齐调用)。`kg_merge.cluster_seeds` 在 rebuild 内是「函数内 import」,故 `monkeypatch.setattr(kg_merge, "cluster_seeds", spy)` 在调用时才绑定、能被拦到。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_wiring.py -v`
Expected: FAIL(`seen` 全为 `None`——调用点尚未传 ann_threads → `k.get("ann_threads")` 得 None)

- [ ] **Step 3: 三处调用点补 kwarg**

在 `sqlite_repository.py` 的三处 `cluster_seeds(...)` 调用(约 6305、6343、6475;先 `grep -n "cluster_seeds(" backend/app/services/sqlite_repository.py` 定位)各补一个 kwarg:

```python
            ann_threads=self.settings.kg_cluster_ann_threads,
```

(加在每个 `cluster_seeds(...)` 调用的 kwargs 里,如 `rep_ann_max=...` 同侧。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_wiring.py -v`
Expected: PASS(1 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_autotune_wiring.py
git commit -m "$(cat <<'EOF'
feat(kg): rebuild 三处 cluster_seeds 透传 settings.kg_cluster_ann_threads

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 回填进程池默认 min(8→32)

**Files:**
- Modify: `backend/app/services/batch_ingest.py`(常量 :595 + `--workers` help :869-873)
- Test: `backend/tests/test_autotune_backfill.py`(Create)

**Interfaces:**
- Produces: `_BACKFILL_DEFAULT_WORKERS == min(32, os.cpu_count() or 1)`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_backfill.py`:

```python
"""回填进程池默认放宽到 min(32, cpu)——64 核不再白闲 56 核。"""
import os
from app.services import batch_ingest


def test_backfill_default_workers_cap_32():
    assert batch_ingest._BACKFILL_DEFAULT_WORKERS == min(32, os.cpu_count() or 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_backfill.py -v`
Expected: FAIL(当前为 `min(8, ...)`;仅当本机 >8 核时该断言失败——若开发机 ≤8 核,先临时 `assert ... == min(32, ...)` 仍会等于 cpu_count 而「假过」,故 review 时以代码常量值为准,不要只看本机结果)

- [ ] **Step 3: 改常量 + help 文案**

batch_ingest.py:595:

```python
_BACKFILL_DEFAULT_WORKERS = min(32, os.cpu_count() or 1)
```

batch_ingest.py:869-873 的 `--workers` help,把 `min(8, CPU核数)` 改为 `min(32, CPU核数)`:

```python
    p.add_argument("--workers", type=int, default=None,
                   help="all 阶段同时抽取的文档数(覆盖 KG_JOB_CONCURRENCY,其余摄取阶段为"
                        "文件级并发,默认 4);vectors-to-blob 阶段为 json.loads/编码并行进程数"
                        f"(默认 min(32, CPU核数)={_BACKFILL_DEFAULT_WORKERS},<=1 走原串行路径,"
                        "不启动进程池;别到 64——单写 SQLite executemany + IPC 在 ~16-24 处封顶)")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_backfill.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_autotune_backfill.py
git commit -m "$(cat <<'EOF'
feat(cli): 向量→BLOB 回填默认进程数放宽 min(8→32, cpu)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `scripts/autotune.sh` + 接入 dev.sh/prod.sh

**Files:**
- Create: `scripts/autotune.sh`
- Modify: `scripts/dev.sh`(`.env` 加载后 source)、`scripts/prod.sh`(同)
- Test: `backend/tests/test_autotune_sh.py`(Create)

**Interfaces:**
- Produces: 一个可被 `source` 的 shell 片段;未设时 export `OMP_NUM_THREADS`/`OPENBLAS_NUM_THREADS`/`MKL_NUM_THREADS`/`NUMEXPR_NUM_THREADS`=`min(cores,8)`;`AUTOTUNE=0` 关闭;`CORES` 可注入覆盖核数探测(测试用)。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_sh.py`:

```python
"""scripts/autotune.sh:未设→export min(cores,8);显式优先;AUTOTUNE=0 关闭。"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]  # backend/tests -> repo root
SCRIPT = ROOT / "scripts" / "autotune.sh"


def _run(env):
    base = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    base.update(env)
    return subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'; echo \"OMP=${{OMP_NUM_THREADS-}}\""],
        capture_output=True, text=True, env=base,
    ).stdout.strip()


def test_autotune_64_cores_caps_blas_at_8():
    assert _run({"CORES": "64"}) == "OMP=8"


def test_autotune_small_machine_uses_all():
    assert _run({"CORES": "4"}) == "OMP=4"


def test_autotune_disabled_sets_nothing():
    assert _run({"CORES": "64", "AUTOTUNE": "0"}) == "OMP="


def test_explicit_omp_is_preserved():
    assert _run({"CORES": "64", "OMP_NUM_THREADS": "2"}) == "OMP=2"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_sh.py -v`
Expected: FAIL(`autotune.sh` 不存在 → `source` 报错 → 全部输出 `OMP=`)

- [ ] **Step 3: 写 autotune.sh**

Create `scripts/autotune.sh`:

```bash
# scripts/autotune.sh — 按核数设置「必须早于 numpy/BLAS 导入」的核绑定线程变量。
# 被 dev.sh / prod.sh 在 `source .env` 之后 source。原则:显式已设的一律不动;
# AUTOTUNE=0 整体关闭;CORES 可注入以覆盖核数探测(测试用)。
# 只设 BLAS/OpenMP 线程——ANN 建索引线程数由 config.py 的 kg_cluster_ann_threads
# 按核解析(进程内,同时覆盖离线 CLI);服务端并发旋钮刻意不按核缩放。
if [ "${AUTOTUNE:-1}" = "1" ]; then
  if command -v nproc >/dev/null 2>&1; then
    _at_cores="$(nproc)"
  else
    _at_cores="$(sysctl -n hw.ncpu 2>/dev/null || echo 1)"
  fi
  _at_cores="${CORES:-$_at_cores}"

  # GEMV/SpMV ~2-4 线程即压满带宽;封顶 8,既够用又给请求级并发留核。
  if [ "$_at_cores" -lt 8 ]; then _at_blas="$_at_cores"; else _at_blas=8; fi

  for _at_v in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS; do
    eval "_at_cur=\${$_at_v-}"
    if [ -z "$_at_cur" ]; then export "$_at_v=$_at_blas"; fi
  done

  echo "autotune: cores=${_at_cores} → BLAS(OMP/OPENBLAS/MKL/NUMEXPR)=${_at_blas}; ANN 线程由 config 按核解析(见后端首行日志); 模型端旋钮(EXTRACT/JOB/EMBED)不随核数变。" >&2
  unset _at_cores _at_blas _at_v _at_cur
fi
```

> 用 POSIX `[ ]` + `eval` 间接取值,兼容 macOS bash 3.2;循环变量用完 `unset`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_sh.py -v`
Expected: PASS(4 passed)

- [ ] **Step 5: shellcheck**

Run: `shellcheck scripts/autotune.sh || true`
Expected: 无 error(`eval` 间接赋值的 SC2086/SC2163 若报,属受控用法,可加 `# shellcheck disable=` 注释)

- [ ] **Step 6: 接入 dev.sh / prod.sh**

`scripts/dev.sh` 与 `scripts/prod.sh` 中,在 `.env` 加载块(`set -a; source ...; set +a` 那段的 `fi` 之后)、`cd "$ROOT_DIR/backend"` 之前,各加一行:

```bash
# 按核数自动调参:设 OMP/BLAS 线程(须早于 python 起)。AUTOTUNE=0 关闭。
# shellcheck source=scripts/autotune.sh
source "$ROOT_DIR/scripts/autotune.sh"
```

- [ ] **Step 7: 手工 smoke(可选但推荐)**

Run: `CORES=64 bash -c 'source scripts/autotune.sh; echo omp=$OMP_NUM_THREADS'`
Expected: 打印 `autotune: cores=64 ...`(stderr)+ `omp=8`(stdout)

- [ ] **Step 8: Commit**

```bash
git add scripts/autotune.sh scripts/dev.sh scripts/prod.sh backend/tests/test_autotune_sh.py
git commit -m "$(cat <<'EOF'
feat(scripts): autotune.sh 按核数设 OMP/BLAS 线程,接入 dev.sh/prod.sh

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 启动日志 + 文档(.env.example / README)

**Files:**
- Modify: `backend/app/main.py`(startup 日志 :55 后加一行)
- Modify: `.env.example`、`README.md`、`README_zh.md`
- Test: `backend/tests/test_autotune_startup_log.py`(Create)

**Interfaces:**
- Consumes: `Settings.kg_cluster_ann_threads`、`batch_ingest._BACKFILL_DEFAULT_WORKERS`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_autotune_startup_log.py`:

```python
"""create_app 启动日志打印已解析的核绑定旋钮值。"""
import logging
import pytest


def test_startup_logs_autotune_line(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "13")
    from app.main import create_app
    with caplog.at_level(logging.INFO, logger="silicon_notebook.startup"):
        create_app()
    assert "kg_cluster_ann_threads=13" in caplog.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_autotune_startup_log.py -v`
Expected: FAIL(日志无该字段)

- [ ] **Step 3: 加启动日志行**

`main.py` 现有 `logger.info("paths: db=%s storage=%s log_dir=%s", ...)`(约 :55)之后加:

```python
    from app.services.batch_ingest import _BACKFILL_DEFAULT_WORKERS
    logger.info(
        "autotune: kg_cluster_ann_threads=%d backfill_default_workers=%d "
        "(模型端并发旋钮 EXTRACT/JOB/EMBED 与本机核数无关,不自动缩放)",
        settings.kg_cluster_ann_threads, _BACKFILL_DEFAULT_WORKERS,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_autotune_startup_log.py -v`
Expected: PASS

- [ ] **Step 5: `.env.example` 补两项**

在 `.env.example` 的 KG/并发相关区,追加:

```dotenv
# 概念聚类/合并的 hnswlib ANN 建索引线程数。0=按机器核数自动(min(cpu,32))。
# 这是唯一按本机核数缩放的旋钮;KG_EXTRACT_WORKERS/KG_JOB_CONCURRENCY/EMBED_CONCURRENCY
# 是远程模型端并发,与本机核数无关,升级核数时不要动它们。
KG_CLUSTER_ANN_THREADS=0
# 启动脚本(dev.sh/prod.sh)按核数自动设 OMP/OPENBLAS/MKL 线程(未显式设时)。
# 设 AUTOTUNE=0 关闭该自动行为。
# AUTOTUNE=1
```

- [ ] **Step 6: README 新增「按核数自动调参」小节(通用口径)**

在 `README.md` 与 `README_zh.md` 的部署/配置章节各加一节,内容要点(通用,不写本机路径):
- 自动按核数推导的**只有核绑定**旋钮:`KG_CLUSTER_ANN_THREADS`(0=auto,`min(cpu,32)`)、启动脚本设的 `OMP/OPENBLAS/MKL_NUM_THREADS`(`min(cpu,8)`)、离线回填进程池默认(`min(cpu,32)`)。
- **刻意不按核缩放**:`KG_EXTRACT_WORKERS`/`KG_JOB_CONCURRENCY`/`EMBED_CONCURRENCY`——它们是远程模型/embed 端点的并发,升级核数不改;需要更高吞吐要先扩模型服务端。
- `AUTOTUNE=0` 关闭 shell 自动调参;所有值都可用显式 env 覆盖(显式优先)。
- 多 worker 是**手动 opt-in**:默认 `--workers 1`;若手动开多 worker,注意每 worker N× 内存(一个大索引 ≈ GB 级)且后台 KG job 落点不定。

- [ ] **Step 7: 全量回归**

Run: `cd backend && pytest tests/test_autotune_config.py tests/test_autotune_kg_merge.py tests/test_autotune_wiring.py tests/test_autotune_backfill.py tests/test_autotune_sh.py tests/test_autotune_startup_log.py -q`
Expected: 全 PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/main.py backend/tests/test_autotune_startup_log.py .env.example README.md README_zh.md
git commit -m "$(cat <<'EOF'
feat(obs+docs): 启动打印 autotune 报告 + README/.env.example 说明核数自适应

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 收尾——rebase + PR

**Files:** 无(集成步骤)

- [ ] **Step 1: 全后端测试快照(受影响面)**

Run: `cd backend && pytest tests/test_autotune_config.py tests/test_autotune_kg_merge.py tests/test_autotune_wiring.py tests/test_autotune_backfill.py tests/test_autotune_sh.py tests/test_autotune_startup_log.py tests/test_cross_doc_merge.py tests/test_rebuild_desc_cache.py -q`
Expected: 全 PASS

- [ ] **Step 2: rebase 到 master 保持线性**(遵「PR 走 Rebase 合并」)

```bash
git fetch origin
git rebase --onto origin/master origin/master  # 或 git rebase origin/master;冲突则解后 --continue
```

- [ ] **Step 3: push + 开 PR(--base master)**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat: 按核数自动调参(core-aware autotune)" --body "$(cat <<'EOF'
## 摘要
部署时按机器核数自动推导「核绑定」并行旋钮,16→64 核零手工改动;服务端/单写绑定旋钮刻意不缩放。

- config: `KG_CLUSTER_ANN_THREADS`(0=auto=min(cpu,32)),解掉 kg_merge.py 的 `set_num_threads(1)` 单核 pin(全仓唯一「天花板是人为 pin」的热点)。
- scripts: `autotune.sh` 按核设 OMP/OPENBLAS/MKL(须早于 numpy 导入),接入 dev.sh/prod.sh。
- cli: 回填进程池默认 min(8→32, cpu)。
- obs+docs: 启动打印调参报告;README/.env.example 说明「不按核缩放的是哪些、为什么」。

显式 env 永远优先;`AUTOTUNE=0` 关闭;不动 `--workers 1`。

设计与计划:docs/superpowers/specs/2026-07-04-core-aware-autotune-design.md、docs/superpowers/plans/2026-07-04-core-aware-autotune.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review(作者自查,已过)

**Spec coverage:**
- 组件 1(config `kg_cluster_ann_threads` + 解析)→ Task 1 ✓
- kg_merge 接线 + 解 pin → Task 2 ✓
- repository 三处调用点 → Task 3 ✓
- 回填 min(8→32) → Task 4 ✓
- 组件 2(autotune.sh + dev/prod.sh)→ Task 5 ✓
- 可观测性(启动日志)+ 文档(.env.example/README)→ Task 6 ✓
- rebase+PR(遵开发流程)→ Task 7 ✓
- 「不缩放服务端旋钮 / 不动 --workers 1」→ 无代码改动即满足,并在 README/日志显式声明 ✓
- 缓存尺寸(非目标)→ 刻意不含 ✓

**Placeholder scan:** 无 TBD/TODO;Task 3 对 `store_kg`/`rebuild_unified_kg` 签名给了「以实际签名为准 + grep 核对」的明确指令(因跨版本签名可能漂移),非占位。

**Type consistency:** `ann_threads`(int)在 `_ann_candidates`/`cluster_seeds`/三处调用点一致;`kg_cluster_ann_threads`、`_BACKFILL_DEFAULT_WORKERS` 命名跨 Task 一致。
