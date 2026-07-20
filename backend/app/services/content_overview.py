from __future__ import annotations

import re

from app.models.schemas import (
    KnowhowOverviewSummary,
    KnowhowOverviewTable,
    KnowhowTableSummary,
    MemoryOverviewSummary,
    NotebookContentOverview,
)
from app.repositories.ports import (
    ContentOverviewKnowhowStorePort,
    ContentOverviewMemoryStorePort,
)
from app.services.knowhow.api import cell_content_hash


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _code_freshness_hash(content_md: str) -> str:
    return cell_content_hash(_MARKDOWN_IMAGE_RE.sub("", content_md).strip())


class ContentOverviewService:
    def __init__(
        self,
        memory_store: ContentOverviewMemoryStorePort,
        knowhow_store: ContentOverviewKnowhowStorePort,
    ) -> None:
        self.memory_store = memory_store
        self.knowhow_store = knowhow_store

    def knowhow_tables(self, notebook_id: str) -> list[KnowhowTableSummary]:
        summaries = []
        for row in self.knowhow_store.knowhow_table_health_inputs(notebook_id):
            code_inputs = row["code_inputs"]
            stale_count = sum(
                item["saved_hash"] != _code_freshness_hash(item["current_content_md"])
                for item in code_inputs
            )
            activity = [
                row["created_at"],
                row["updated_at"],
                row["row_activity_at"],
                row["cell_activity_at"],
                *(item["updated_at"] for item in code_inputs),
            ]
            summaries.append(KnowhowTableSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"],
                description=row["description"],
                row_count=row["row_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                projection_pending=row["projection_pending"],
                projection_failed=row["projection_failed"],
                stale_code_count=stale_count,
                last_activity_at=max(value for value in activity if value),
            ))
        return summaries

    def get(self, notebook_id: str, user_id: str) -> NotebookContentOverview:
        memory = self.memory_store.notebook_content_overview(
            user_id, notebook_id, limit=3
        )
        tables = self.knowhow_tables(notebook_id)
        recent_tables = sorted(
            tables,
            key=lambda table: (table.last_activity_at, table.id),
            reverse=True,
        )[:3]
        return NotebookContentOverview(
            memory=MemoryOverviewSummary(**memory),
            knowhow=KnowhowOverviewSummary(
                table_count=len(tables),
                row_count=sum(table.row_count for table in tables),
                projection_pending=sum(
                    table.projection_pending for table in tables
                ),
                projection_failed=sum(table.projection_failed for table in tables),
                stale_code_count=sum(table.stale_code_count for table in tables),
                recent_tables=[
                    KnowhowOverviewTable(
                        id=table.id,
                        title=table.title,
                        row_count=table.row_count,
                        last_activity_at=table.last_activity_at,
                    )
                    for table in recent_tables
                ],
            ),
        )
