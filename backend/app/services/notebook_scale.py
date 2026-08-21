from __future__ import annotations
from typing import Callable, Hashable, Protocol

from app.core.config import Settings
from app.domain.notebook_scale import NotebookScaleFacts
from app.services.vector_cache import VectorCache

class NotebookScaleFactsRepository(Protocol):
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...
    def is_mounted_by_anyone(self, notebook_id: str) -> bool: ...

class NotebookScaleProfile:
    def __init__(self, settings: Settings, facts: NotebookScaleFactsRepository, version_for: Callable[[str], Hashable], cache: VectorCache) -> None:
        self.settings, self.facts_repo, self.version_for, self.cache = settings, facts, version_for, cache
    def facts(self, notebook_id: str) -> NotebookScaleFacts:
        return self.facts_repo.load_notebook_scale_facts(notebook_id)
    def copy_stats(self, notebook_id: str) -> dict:
        # bytes + chunks+nodes only — the cheap, KG-version-cached copyability
        # verdict retrieval reads on the hot path. The deep-copy total-
        # materialisation bound (which also depends on assets/sources that do NOT
        # bump this cache's version) is enforced FRESH one layer up, in the
        # share-routing service (NotebookSharingService.notebook_copy_stats), so it
        # can never go stale here (codex PR#354 r2 P2).
        version = (self.version_for(notebook_id), self.settings.notebook_copy_max_bytes, self.settings.notebook_copy_max_rows)
        def load():
            f = self.facts(notebook_id)
            return {"copyable": f.bytes <= self.settings.notebook_copy_max_bytes and f.chunks + f.nodes <= self.settings.notebook_copy_max_rows, "size": f.as_size_dict()}
        return self.cache.get(f"{notebook_id}:copystats", version, load)
    def is_copyable(self, notebook_id: str) -> bool:
        return bool(self.copy_stats(notebook_id)["copyable"])
    def requires_index(self, notebook_id: str, *, has_disk_index: bool) -> bool:
        if self.is_copyable(notebook_id): return False
        return not has_disk_index
    def index_eligible(self, notebook_id: str, *, tier: str, has_disk_index: bool, total_chunks: int) -> bool:
        # 被任何笔记本挂载即构成建索引资格(Task 6)——镜像 ScaleArtifactRuntime.eligible
        # 的同一分支,两处必须保持一致(否则建索引与用索引的判定会分叉)。
        if tier == "base" or has_disk_index or self.facts_repo.is_mounted_by_anyone(notebook_id): return True
        if total_chunks > self.settings.index_suggest_chunk_threshold: return True
        return not self.is_copyable(notebook_id)
