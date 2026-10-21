"""The report a conversation names, projected from stored rows.

Pure leaf module shared by both ``ask_state_store`` adapters (they must not
import each other). A conversation names a report in two places, and the two
adapters must read both the same way:

* the conversation's LAST answer -- the receipt (``answers.payload.linked_report``)
  that turn stored. ``list_conversations`` pulls it out in SQL (JSON text on
  SQLite, an already-decoded object on PostgreSQL) and ``get_conversation``
  takes it from the last payload it loaded; both end in
  :func:`receipt_linked_report`, so a summary and the detail of the same
  conversation can never disagree. The title is the receipt's snapshot, so it
  does not change when the report is later deleted.
* the in-flight job (``ask_jobs.report_id``). The job row stores the id only;
  the title is the report's CURRENT question, read by a ``LEFT JOIN`` when the
  conversation is opened, so :func:`job_linked_report` gets an empty title once
  the report is gone.

A turn that attached no report yields ``None`` on both. Nothing here reads a
database.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.models.ask import LinkedReportRef


def job_linked_report(report_id: object, title: object) -> Optional[LinkedReportRef]:
    """The report a stored id and title name, ``None`` when there is no id.

    ``title`` is whatever the row (or receipt) held; anything that is not a
    string reads as empty rather than failing the whole conversation read."""
    if not isinstance(report_id, str) or not report_id:
        return None
    return LinkedReportRef(
        report_id=report_id, title=title if isinstance(title, str) else ""
    )


def receipt_linked_report(raw: Any) -> Optional[LinkedReportRef]:
    """The report a stored ``linked_report`` receipt names.

    ``raw`` is the receipt as one adapter holds it: an object, the JSON text
    SQLite's ``json_extract`` returns for an object, or ``None`` (the key is
    absent, ``null``, or the turn attached nothing). A value that is not an
    object with a non-empty ``report_id`` string reads as no report -- a
    malformed historical payload must not break listing the conversations."""
    value = raw
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    return job_linked_report(value.get("report_id"), value.get("title"))
