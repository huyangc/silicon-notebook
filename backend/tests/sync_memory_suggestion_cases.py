"""Backend-neutral cases for cross-environment sync of auto user-memory
suggestions (plan T11; design §6.6 / D15 (a)): only ADOPTED suggestions
travel, their three child tables follow the parent, and an adoption re-sends
the provenance and revisions the earlier windows had to leave behind.
SQLite: ``tests/test_sync_memory_suggestions.py``; PostgreSQL twin:
``tests/postgres/test_sync_memory_suggestions_pg.py``.

Each case takes a ``Pair``: a SOURCE environment whose ``MemoryService`` is the
T8 world (``tests/memory_suggest_adoption_cases.py``: the backend's real Memory
stores, recording fakes around them) and a TARGET environment, both real
databases, with the real ``export_notebooks`` / ``import_package`` between
them. ``MEMBER`` reads ``NB`` (``OWNER``'s notebook) through a membership row,
exactly as in the store cases, and both users exist on both sides under the
same username, so the identity mapping joins them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.migration.sync.export import ExportReport, export_notebooks
from app.migration.sync.import_ import MEMORY_SYNC_ORIGIN_KEY, import_package
from app.migration.sync.package import DELETES_NAME, MODE_FULL, MODE_INCREMENTAL, rows_path
from tests import memory_suggest_adoption_cases as adoption
from tests import memory_suggest_write_cases as writes
from tests.memory_suggest_write_cases import CONV, MEMBER, NB, NOW, OWNER, suggestion

SOURCE_ENV = "dev"
TARGET_ENV = "prod"
EDIT = {"title": "Adopted", "content_md": "adopted body", "tags": ["kept"]}
MEMORY_TABLES = ("memory_items", "memory_provenance", "memory_revisions", "memory_embeddings")


@dataclass
class Side:
    settings: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]
    json: Callable[[Any], Any]

    def users(self) -> None:
        for user in (OWNER, MEMBER):
            self.run(
                "INSERT INTO users(id,username,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user, user, f"{user}@x", user, "user", NOW, NOW),
            )

    def memory_ids(self) -> set[str]:
        return {row["id"] for row in self.rows("SELECT id FROM memory_items", ())}

    def children(self, table: str, memory_id: str) -> list[dict]:
        return self.rows(f"SELECT * FROM {table} WHERE memory_id = ?", (memory_id,))

    def status(self, memory_id: str) -> str | None:
        found = self.rows("SELECT status FROM memory_items WHERE id = ?", (memory_id,))
        return found[0]["status"] if found else None


@dataclass
class Pair:
    source: Side
    target: Side
    world: adoption.World
    out: Path
    packages: list[ExportReport] = field(default_factory=list)

    # ---------------------------------------------------------------- setup
    def seed(self) -> None:
        """Both users on both sides; ``NB`` (with ``MEMBER``'s membership and
        a conversation for answers to hang off) on the source only."""
        self.source.users()
        self.target.users()
        self.source.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "Shared", OWNER, "ready", NOW, NOW),
        )
        self.source.run(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (NB, MEMBER, "reader", writes.MEMBER_SINCE),
        )
        self.source.run(
            "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (CONV, NB, "t", MEMBER, NOW, NOW),
        )

    def capture(self, enabled: bool) -> None:
        if self.source.rows("SELECT 1 AS x FROM sync_capture_control WHERE singleton = 1", ()):
            self.source.run(
                "UPDATE sync_capture_control SET enabled = ? WHERE singleton = 1", (enabled,))
        else:
            self.source.run(
                "INSERT INTO sync_capture_control(singleton, enabled, enabled_at) "
                "VALUES (?, ?, ?)",
                (1, enabled, NOW),
            )

    def baseline(self) -> None:
        """Capture on, one full package exported and imported: the target is a
        real mirror, so later windows are incremental."""
        self.capture(True)
        self.sync(expect=MODE_FULL)

    # ---------------------------------------------------------------- sync
    def export(self, *, full: bool = False) -> ExportReport:
        report = export_notebooks(
            self.source.settings,
            target_env=TARGET_ENV,
            out_dir=self.out,
            notebook_ids=None,
            source_env=SOURCE_ENV,
            full=full,
        )
        self.packages.append(report)
        return report

    def sync(self, *, expect: str, full: bool = False) -> tuple[ExportReport, Any]:
        report = self.export(full=full)
        assert report.mode == expect
        imported = import_package(self.target.settings, report.package_dir)
        assert imported.error == "", imported.error
        return report, imported

    # ---------------------------------------------------------------- source edits
    def suggest(self, *writes_: Any) -> None:
        """Write suggestions through ``MemoryService.create_suggestions`` (switch
        on, a claimed job row, read access re-checked in the write)."""
        created = self.world.suggest_rows(*writes_)
        assert [item.id for item in created] == [write.id for write in writes_]

    def answer(self, answer_id: str) -> None:
        self.world.env.answer(answer_id)

    def raw_suggestion(self, side: Side, memory_id: str, *, created_by: str = MEMBER) -> None:
        """An unadopted suggestion with its provenance and first revision,
        written straight into ``side`` (a TARGET has no claimed job to write
        through, and a source window only needs the rows and their log)."""
        side.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,suggested_signal,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (memory_id, NB, created_by, "ask_answer", "candidate", "draft", "draft body",
             "clarified", NOW, NOW),
        )
        side.run(
            "INSERT INTO memory_provenance(id,memory_id,origin,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (f"prov-{memory_id}", memory_id, "ask_answer", side.json({"question": "q"}), NOW),
        )
        side.run(
            "INSERT INTO memory_revisions(id,memory_id,revision,title,content_md,tags_json,"
            "status,promotion_state,changed_by,change_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"rev-{memory_id}", memory_id, 1, "draft", "draft body", side.json([]),
             "candidate", "none", created_by, "suggested", NOW),
        )

    def embedding(self, memory_id: str) -> None:
        self.source.run(
            "INSERT INTO memory_embeddings(memory_id,model,dimension,vector,updated_at) "
            "VALUES (?,?,?,?,?)",
            (memory_id, "m", 2, b"\x00\x00\x00\x00\x00\x00\x80\x3f", NOW),
        )


# ------------------------------------------------------------------ package readers
def package_rows(report: ExportReport, table: str) -> list[dict]:
    text = (report.package_dir / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def package_ids(report: ExportReport, table: str) -> set[str]:
    column = "id" if table == "memory_items" else "memory_id"
    return {str(row[column]) for row in package_rows(report, table)}


def package_revisions(report: ExportReport, memory_id: str) -> list[int]:
    return sorted(
        int(row["revision"]) for row in package_rows(report, "memory_revisions")
        if row["memory_id"] == memory_id
    )


def package_deletes(report: ExportReport) -> list[dict]:
    text = (report.package_dir / DELETES_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _memory_log_rows(side: Side) -> int:
    placeholders = ",".join("?" for _ in MEMORY_TABLES)
    return int(side.rows(
        f"SELECT COUNT(*) AS n FROM sync_change_log WHERE table_name IN ({placeholders})",
        MEMORY_TABLES,
    )[0]["n"])


def _payload(value: Any) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _assert_nothing_of(report: ExportReport, memory_ids: set[str]) -> None:
    for table in MEMORY_TABLES:
        assert not package_ids(report, table) & memory_ids, table


# ------------------------------------------------------------------ cases
def assert_full_package_excludes_unadopted_and_children(pair: Pair) -> None:
    """A full package leaves every unadopted suggestion (candidate AND
    rejected) and all three of its child tables behind; an adopted one and a
    plain Agent candidate travel, and the target imports the package."""
    pair.seed()
    pair.suggest(suggestion("s-cand"), suggestion("s-rej"), suggestion("s-conf"))
    pair.world.service.reject("s-rej", MEMBER)
    pair.world.service.confirm("s-conf", MEMBER, dict(EDIT))
    # An Agent candidate is not a system suggestion: the filter leaves it be.
    pair.world.memory("agent-cand", title="agent", status="candidate", origin="external_agent")
    # No draft has a vector in practice (they are embedded on adoption); one
    # here proves the vector table follows its parent too.
    pair.embedding("s-cand")
    pair.embedding("s-rej")

    report, _ = pair.sync(expect=MODE_FULL)
    assert package_ids(report, "memory_items") == {"s-conf", "agent-cand"}
    _assert_nothing_of(report, {"s-cand", "s-rej"})
    assert "s-conf" in package_ids(report, "memory_provenance")
    assert package_revisions(report, "s-conf") == [1, 2]
    assert pair.target.memory_ids() == {"s-conf", "agent-cand"}
    assert pair.target.status("s-conf") == "confirmed"


def _adopt_through_windows(pair: Pair, via: str) -> tuple[ExportReport, ExportReport]:
    """Baseline, then a suggestion created in one window (left behind), then
    adopted through ``via`` in the next: returns the two incremental reports."""
    pair.seed()
    pair.baseline()
    pair.answer("ans-1")
    pair.suggest(suggestion("s-1", signal="repeated", answer_id="ans-1"))
    before, _ = pair.sync(expect=MODE_INCREMENTAL)
    _assert_nothing_of(before, {"s-1"})
    assert "s-1" not in pair.target.memory_ids()
    if via == "confirm":
        adopted = pair.world.service.confirm("s-1", MEMBER, dict(EDIT))
    else:
        adopted = pair.world.service.create_from_answer(
            NB, MEMBER, "ans-1", EDIT["title"], EDIT["content_md"], EDIT["tags"])
    assert (adopted.id, adopted.status) == ("s-1", "confirmed")
    after, _ = pair.sync(expect=MODE_INCREMENTAL)
    return before, after


def _assert_adoption_carried(pair: Pair, after: ExportReport) -> None:
    assert "s-1" in package_ids(after, "memory_items")
    assert "s-1" in package_ids(after, "memory_provenance")
    # BOTH revisions: the first (written while it was a draft, filtered out
    # of its own window) only travels because adoption re-sent it.
    assert package_revisions(after, "s-1") == [1, 2]
    assert pair.target.status("s-1") == "confirmed"
    assert len(pair.target.children("memory_provenance", "s-1")) == 1
    assert sorted(
        int(row["revision"]) for row in pair.target.children("memory_revisions", "s-1")
    ) == [1, 2]


def assert_confirm_adoption_resends_parent_provenance_and_revisions(pair: Pair) -> None:
    _, after = _adopt_through_windows(pair, "confirm")
    _assert_adoption_carried(pair, after)


def assert_answer_adoption_resends_parent_provenance_and_revisions(pair: Pair) -> None:
    _, after = _adopt_through_windows(pair, "create_from_answer")
    _assert_adoption_carried(pair, after)


def assert_target_sync_origin_stamped_on_adopted(pair: Pair) -> None:
    """The re-sent provenance is what lets the target say "the source answer
    is in <env>": without it ``_stamp_memory_sync_origin`` has nothing to
    stamp."""
    _adopt_through_windows(pair, "confirm")
    (provenance,) = pair.target.children("memory_provenance", "s-1")
    assert _payload(provenance["payload_json"])[MEMORY_SYNC_ORIGIN_KEY] == SOURCE_ENV


def assert_capture_disabled_no_touch(pair: Pair) -> None:
    """With capture closed, adopting writes nothing to the change log (the
    re-send UPDATEs are skipped and the triggers log nothing); the full
    package an operator takes after reopening still carries the adopted
    memory whole."""
    pair.seed()
    pair.baseline()
    pair.suggest(suggestion("s-1"))
    pair.sync(expect=MODE_INCREMENTAL)
    pair.capture(False)
    head = _memory_log_rows(pair.source)
    pair.world.service.confirm("s-1", MEMBER, dict(EDIT))
    assert pair.source.status("s-1") == "confirmed"
    assert _memory_log_rows(pair.source) == head
    pair.capture(True)
    report, _ = pair.sync(expect=MODE_FULL, full=True)
    assert package_revisions(report, "s-1") == [1, 2]
    assert pair.target.status("s-1") == "confirmed"
    assert len(pair.target.children("memory_provenance", "s-1")) == 1


def assert_draft_deleted_replays_as_absent(pair: Pair) -> None:
    """Deletes are never filtered: a draft the target never received is
    deleted at the source, the package says so, and the target counts the
    replay ``absent`` instead of failing."""
    pair.seed()
    pair.baseline()
    pair.suggest(suggestion("s-1"))
    pair.sync(expect=MODE_INCREMENTAL)
    pair.world.service.delete("s-1", MEMBER)
    report, imported = pair.sync(expect=MODE_INCREMENTAL)
    deleted = {
        (entry["table"], str(entry["key"].get("id") or entry["key"].get("memory_id")))
        for entry in package_deletes(report)
    }
    assert ("memory_items", "s-1") in deleted
    assert imported.deletes_absent >= 1
    assert imported.deletes_applied == 0
    assert "s-1" not in pair.target.memory_ids()


def assert_verify_row_scopes_accepts_filtered_package(pair: Pair) -> None:
    """Both export paths hand the importer's scope check a package with no
    child row whose parent stayed behind: the full one, and a window whose
    only memory changes are a new draft's three rows."""
    pair.seed()
    pair.suggest(suggestion("s-old"), suggestion("s-conf"))
    pair.world.service.confirm("s-conf", MEMBER, dict(EDIT))
    pair.baseline()
    pair.raw_suggestion(pair.source, "s-new")
    report, _ = pair.sync(expect=MODE_INCREMENTAL)
    _assert_nothing_of(report, {"s-old", "s-new"})
    # The draft's notebook is not even declared by this window: nothing else
    # changed, so there is no notebook to carry.
    assert NB not in report.notebooks
    assert pair.target.memory_ids() == {"s-conf"}


def assert_deprecated_adopted_syncs(pair: Pair) -> None:
    """``deprecated`` is still the user's memory: adopted, then retired, it
    travels with every revision, in a window and in a full package alike."""
    pair.seed()
    pair.baseline()
    pair.suggest(suggestion("s-1"))
    pair.world.service.confirm("s-1", MEMBER, dict(EDIT))
    pair.world.service.deprecate("s-1", MEMBER)
    window, _ = pair.sync(expect=MODE_INCREMENTAL)
    assert package_revisions(window, "s-1") == [1, 2, 3]
    assert pair.target.status("s-1") == "deprecated"
    full, _ = pair.sync(expect=MODE_FULL, full=True)
    assert package_ids(full, "memory_items") == {"s-1"}
    assert package_revisions(full, "s-1") == [1, 2, 3]
    assert pair.target.status("s-1") == "deprecated"


def assert_full_import_keeps_target_own_suggestions(pair: Pair) -> None:
    """A full package does not carry the source's drafts, and must not take
    the TARGET's own drafts on the mirror with it either: memory is exempt
    from full-package reconciliation."""
    pair.seed()
    pair.baseline()
    pair.raw_suggestion(pair.target, "t-own")
    pair.suggest(suggestion("s-1"))
    report, _ = pair.sync(expect=MODE_FULL, full=True)
    _assert_nothing_of(report, {"s-1"})
    assert pair.target.status("t-own") == "candidate"
    assert len(pair.target.children("memory_provenance", "t-own")) == 1
    assert len(pair.target.children("memory_revisions", "t-own")) == 1
    assert "s-1" not in pair.target.memory_ids()
