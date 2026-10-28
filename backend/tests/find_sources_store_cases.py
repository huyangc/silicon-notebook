"""Backend-neutral cases for ``SourceStore.find_sources_metadata_rows``.

SQLite: ``tests/test_find_sources_store.py``; PostgreSQL twin:
``tests/postgres/test_find_sources_store_pg.py``.  Each case takes an ``Env``:
the backend's ``SourceStore``, ``run`` (a write with ``?`` placeholders) and
``connect`` (a read connection).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable

import pytest

NB = "nb-fs"
OTHER_NB = "nb-fs-other"
OWNER = "u-fs"
NOW = "2026-10-28T12:00:00+00:00"


@dataclass
class Env:
    store: Any
    run: Callable[[str, tuple], None]
    connect: Callable[[], AbstractContextManager]

    def seed(self) -> None:
        self.run(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (OWNER, f"{OWNER}@x", "fs", "user", NOW, NOW),
        )
        for notebook_id in (NB, OTHER_NB):
            self.run(
                "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (notebook_id, notebook_id, OWNER, "ready", NOW, NOW),
            )

    def source(
        self, source_id: str, *, title: str = "", file_name: str = "",
        summary: str = "", notebook_id: str = NB, source_type: str = "pdf",
        doc_type: str = "", parse_status: str = "extracted",
    ) -> None:
        self.run(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title or source_id, source_type, "extracted",
             parse_status, file_name or f"{source_id}.pdf", f"p/{source_id}",
             summary, doc_type, NOW, NOW),
        )

    def meta(
        self, source_id: str, *, is_paper: bool = True, paper_title: str = "",
        venue: str = "", year: int | None = None, keywords=(), authors=(),
        notebook_id: str = NB,
    ) -> None:
        self.store.upsert_paper_meta(source_id, notebook_id, {
            "is_paper": is_paper, "paper_title": paper_title or None,
            "venue": venue or None, "pub_year": year, "keywords": list(keywords),
            "authors": [
                {"position": index, "name": name, "affiliation": ""}
                for index, name in enumerate(authors)
            ],
            "model": "m",
        })

    def scope(self, notebook_id: str = NB, *, capture=None, **kwargs) -> dict:
        kwargs.setdefault("kind", "all")
        with self.connect() as db:
            connection = db if capture is None else capture.wrap(db)
            return self.store.find_sources_metadata_rows(
                connection, notebook_id, **kwargs)

    def find(self, notebook_id: str = NB, **kwargs) -> dict[str, dict]:
        return {
            row["source_id"]: row for row in self.scope(notebook_id, **kwargs)["rows"]
        }

    def matches(self, terms, **kwargs) -> dict[str, dict]:
        return {
            source_id: row["matches"]
            for source_id, row in self.find(terms=list(terms), **kwargs).items()
            if row["matches"]
        }


class Capture:
    """A connection proxy recording each statement and the rows it returned."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def wrap(self, connection):
        capture = self

        class _Cursor:
            def __init__(self, sql, cursor):
                self._sql, self._cursor = sql, cursor

            def fetchall(self):
                rows = [dict(row) for row in self._cursor.fetchall()]
                capture.calls.append((self._sql, rows))
                return rows

            def fetchone(self):
                row = self._cursor.fetchone()
                capture.calls.append((self._sql, [dict(row)] if row else []))
                return row

        class _Connection:
            def execute(self, sql, params=(), **kwargs):
                return _Cursor(sql, connection.execute(sql, params, **kwargs))

            def __getattr__(self, name):
                return getattr(connection, name)

        return _Connection()

    def rows_of(self, marker: str) -> list[dict]:
        return [row for sql, rows in self.calls if marker in sql for row in rows]


def assert_every_field_matches(env: Env) -> None:
    env.seed()
    env.source("s-title", title="Diffusion Models Beat GANs")
    env.source("s-file", title="untitled", file_name="diffusion_survey.pdf")
    env.source("s-summary", title="x1", summary="We study diffusion processes.")
    env.source("s-author", title="x2")
    env.meta("s-author", authors=["Ada Diffusion"])
    env.source("s-ptitle", title="x3")
    env.meta("s-ptitle", paper_title="Denoising Diffusion Probabilistic Models")
    env.source("s-venue", title="x4")
    env.meta("s-venue", venue="Diffusion Workshop")
    env.source("s-kw", title="x5")
    env.meta("s-kw", keywords=["score matching", "diffusion"])
    env.source("s-none", title="Transformers", summary="attention")
    got = env.matches(["diffusion"])
    assert got == {
        "s-title": {"title": True},
        "s-file": {"title": True},
        "s-summary": {"summary": True},
        "s-author": {"author": True},
        "s-ptitle": {"paper_title": True},
        "s-venue": {"venue": True},
        "s-kw": {"keywords": True},
    }
    # Every in-scope source comes back, matched or not (the filtered scope).
    assert set(env.find()) == set(got) | {"s-none"}


def assert_terms_fold_ascii_only_and_or_together(env: Env) -> None:
    env.seed()
    env.source("s-a", title="IMAGENET results")
    env.source("s-b", title="Mamba blocks")
    env.source("s-c", title="Élan vital")
    env.source("s-d", title="élan minuscule")
    assert set(env.matches(["imagenet", "MAMBA"])) == {"s-a", "s-b"}
    # SQL lower() folds A-Z only: an accented capital matches only itself.
    assert set(env.matches(["Élan"])) == {"s-c"}
    assert set(env.matches(["élan"])) == {"s-d"}


def assert_like_metacharacters_are_escaped(env: Env) -> None:
    env.seed()
    env.source("s-pct", title="Top-1 50% faster")
    env.source("s-500", title="Top-1 500 faster")
    env.source("s-us", title="model a_b test")
    env.source("s-axb", title="model axb test")
    env.source("s-bs", title="path c:\\data here")
    env.source("s-nobs", title="path c:data here")
    assert set(env.matches(["50%"])) == {"s-pct"}
    assert set(env.matches(["a_b"])) == {"s-us"}
    assert set(env.matches(["c:\\data"])) == {"s-bs"}


def assert_short_ascii_terms_need_a_word_boundary(env: Env) -> None:
    env.seed()
    env.source("s-word", title="A GAN for faces")
    env.source("s-inside", title="Organic chemistry")
    env.source("s-cjk", title="基于生成对抗网络的方法")
    env.source("s-long", title="Transformers everywhere")
    env.source("s-both", title="organ GAN")
    got = env.matches(["gan", "生成", "transformer"])
    assert got == {
        "s-word": {"title": True},
        "s-inside": {"title": False},
        "s-cjk": {"title": True},
        "s-long": {"title": True},
        "s-both": {"title": True},
    }


def assert_keyword_matches_stay_inside_one_keyword(env: Env) -> None:
    env.seed()
    env.source("s-kw", title="x")
    env.meta("s-kw", keywords=["alpha", "beta"])
    assert env.matches(['a", "b']) == {}
    assert env.matches(["beta"]) == {"s-kw": {"keywords": True}}
    assert env.matches(["alph"]) == {"s-kw": {"keywords": False}}


def assert_keywords_match_each_element_as_written(env: Env) -> None:
    """A quote or a backslash in a keyword is matched as written: the legs
    read one array element at a time, never the JSON text (where they are
    escaped)."""
    env.seed()
    env.source("s-quote", title="x1")
    env.meta("s-quote", keywords=['the "attention" trick'])
    env.source("s-bs", title="x2")
    env.meta("s-bs", keywords=["c:\\data loader"])
    assert env.matches(['"attention"']) == {"s-quote": {"keywords": True}}
    assert env.matches(["c:\\data"]) == {"s-bs": {"keywords": True}}


def assert_plural_keeps_a_short_term_on_the_word_boundary(env: Env) -> None:
    env.seed()
    env.source("s-plural", title="Training GANs at scale")
    env.source("s-es", title="Two VAEes compared")
    env.source("s-organ", title="Organ printing")
    env.source("s-organs", title="organs on chips")
    env.source("s-gansu", title="Gansu province survey")
    got = env.matches(["gan", "vae"])
    assert got == {
        "s-plural": {"title": True},
        "s-es": {"title": True},
        "s-organ": {"title": False},
        "s-organs": {"title": False},
        "s-gansu": {"title": False},
    }


def assert_filters_compose(env: Env) -> None:
    env.seed()
    env.source("s-2019", title="a")
    env.meta("s-2019", year=2019, venue="CVPR 2019", authors=["Kaiming He"])
    env.source("s-2021", title="b")
    env.meta("s-2021", year=2021, venue="NeurIPS", authors=["Yann LeCun", "Kaiming He"])
    env.source("s-2023", title="c")
    env.meta("s-2023", year=2023, venue="cvpr workshop", authors=["Someone"])
    env.source("s-nometa", title="d")
    assert set(env.find(year_from=2020)) == {"s-2021", "s-2023"}
    assert set(env.find(year_to=2021)) == {"s-2019", "s-2021"}
    assert set(env.find(year_from=2020, year_to=2022)) == {"s-2021"}
    assert set(env.find(venue="CVPR")) == {"s-2019", "s-2023"}
    assert set(env.find(author="kaiming")) == {"s-2019", "s-2021"}
    assert set(env.find(author="kaiming", venue="cvpr")) == {"s-2019"}
    # A filter needs the metadata it filters on: a source without a meta row
    # is outside any year/venue/author filter.
    assert "s-nometa" not in env.find(year_from=1900)
    # Filter values escape LIKE metacharacters too.
    assert env.find(venue="%") == {}


def assert_kind_papers_includes_unextracted_eligible_sources(env: Env) -> None:
    env.seed()
    env.source("s-paper", title="a")
    env.meta("s-paper", is_paper=True)
    env.source("s-notpaper", title="b")
    env.meta("s-notpaper", is_paper=False)
    env.source("s-pending", title="c", doc_type="academic_paper")
    env.source("s-pending-blank", title="c2", doc_type="")
    env.source("s-manual", title="d", doc_type="manual")
    env.source("s-unparsed", title="e", parse_status="uploaded")
    env.source("s-promo", title="f", source_type="promotion")
    papers = env.find(kind="papers")
    assert set(papers) == {"s-paper", "s-pending", "s-pending-blank"}
    assert papers["s-paper"]["pending"] is False
    assert papers["s-pending"]["pending"] is True
    assert papers["s-pending-blank"]["pending"] is True
    everything = env.find(kind="all")
    assert set(everything) == {
        "s-paper", "s-notpaper", "s-pending", "s-pending-blank", "s-manual",
        "s-unparsed", "s-promo",
    }
    with pytest.raises(ValueError):
        env.find(kind="papers-and-more")


def assert_hidden_projections_and_other_libraries_never_appear(env: Env) -> None:
    env.seed()
    env.source("s-visible", title="graph neural networks")
    env.source("s-memory", title="graph memory note", source_type="memory")
    env.source("s-knowhow", title="graph knowhow table", source_type="knowhow")
    env.source("s-elsewhere", title="graph elsewhere", notebook_id=OTHER_NB)
    env.meta("s-elsewhere", authors=["graph author"], notebook_id=OTHER_NB)
    got = env.find(terms=["graph"])
    assert set(got) == {"s-visible"}
    # Even when the allow list names them (an MCP list carries the owner's
    # hidden sources): the visible predicate is the source tab's.
    got = env.find(
        terms=["graph"], allowed_source_ids=["s-visible", "s-memory", "s-knowhow"],
    )
    assert set(got) == {"s-visible"}


def assert_allow_list_narrows_the_scope(env: Env) -> None:
    env.seed()
    for index in range(5):
        env.source(f"s-{index}", title=f"vision paper {index}")
    got = env.find(terms=["vision"], allowed_source_ids=["s-1", "s-3", "s-gone"])
    assert set(got) == {"s-1", "s-3"}
    assert env.find(terms=["vision"], allowed_source_ids=[]) == {}


def assert_row_shape(env: Env) -> None:
    env.seed()
    env.source("s-a", title="Stored title", doc_type="academic_paper")
    env.meta("s-a", paper_title="Paper Title", venue="ICLR", year=2024)
    env.source("s-b", title="Plain title")
    assert env.find()["s-a"] == {
        "source_id": "s-a", "notebook_id": NB, "pub_year": 2024,
        "pending": False, "matches": {},
    }
    titled = env.find(with_titles=True)
    assert titled["s-a"]["sort_title"] == "Paper Title"
    assert titled["s-b"]["sort_title"] == "Plain title"


def assert_the_scope_statement_reads_ids_not_display_rows(env: Env) -> None:
    """The ranking reads ids, years and the pending flag; titles only for the
    pure-filter ordering (``with_titles``)."""
    env.seed()
    env.source("s-a", title="vision a", summary="a long summary " * 20)
    env.meta("s-a", paper_title="Vision", venue="CVPR", year=2024)
    capture = Capture()
    env.scope(terms=["vision"], capture=capture)
    scope_rows = capture.rows_of("AS pending FROM")
    assert scope_rows and set(scope_rows[0]) == {"id", "pub_year", "pending"}


def assert_counts_report_the_unjudgeable_papers(env: Env) -> None:
    env.seed()
    env.source("s-meta", title="a")
    env.meta("s-meta", year=2022, venue="CVPR", authors=["Kaiming He"])
    env.source("s-pending-1", title="b", doc_type="academic_paper")
    env.source("s-pending-2", title="c")
    env.source("s-manual", title="d", doc_type="manual")
    env.source("s-memory", title="e", source_type="memory")
    for filters in ({"year_from": 2020}, {"venue": "cvpr"}, {"author": "he"}):
        scope = env.scope(kind="papers", **filters)
        assert [row["source_id"] for row in scope["rows"]] == ["s-meta"]
        assert scope["meta_pending_excluded"] == 2, filters
        assert scope["scope_total"] == 4
        assert scope["visible_total"] == 4
    scope = env.scope(kind="papers")
    assert scope["meta_pending_excluded"] == 0
    assert sorted(row["source_id"] for row in scope["rows"] if row["pending"]) == [
        "s-pending-1", "s-pending-2"]
    # The allow list bounds the counts too.
    scope = env.scope(kind="all", year_from=2020,
                      allowed_source_ids=["s-meta", "s-pending-1"])
    assert (scope["scope_total"], scope["visible_total"]) == (2, 4)
    assert scope["meta_pending_excluded"] == 1


def assert_term_legs_are_bound_only_to_a_narrowed_scope(env: Env) -> None:
    env.seed()
    for index in range(12):
        env.source(f"s-{index:02d}", title=f"vision paper {index}", doc_type="manual")
    env.source("s-paper", title="vision paper", doc_type="academic_paper")
    env.meta("s-paper", year=2024)
    narrowed = Capture()
    got = env.scope(terms=["vision"], kind="papers", capture=narrowed)
    assert [row["source_id"] for row in got["rows"]] == ["s-paper"]
    assert {row["source_id"] for row in narrowed.rows_of("UNION ALL")} == {"s-paper"}
    listed = Capture()
    env.scope(terms=["vision"], allowed_source_ids=["s-01"], capture=listed)
    assert {row["source_id"] for row in listed.rows_of("UNION ALL")} == {"s-01"}
    # A list covering a large share of the library is not bound (it would
    # cost more than the hits it drops), nor is an unnarrowed scope.
    wide = Capture()
    env.scope(terms=["vision"], allowed_source_ids=[f"s-{i:02d}" for i in range(6)],
              capture=wide)
    assert len({row["source_id"] for row in wide.rows_of("UNION ALL")}) == 13
    whole = Capture()
    env.scope(terms=["vision"], kind="all", capture=whole)
    assert len({row["source_id"] for row in whole.rows_of("UNION ALL")}) == 13


ALL_CASES = [
    assert_every_field_matches,
    assert_terms_fold_ascii_only_and_or_together,
    assert_like_metacharacters_are_escaped,
    assert_short_ascii_terms_need_a_word_boundary,
    assert_keyword_matches_stay_inside_one_keyword,
    assert_keywords_match_each_element_as_written,
    assert_plural_keeps_a_short_term_on_the_word_boundary,
    assert_filters_compose,
    assert_kind_papers_includes_unextracted_eligible_sources,
    assert_hidden_projections_and_other_libraries_never_appear,
    assert_allow_list_narrows_the_scope,
    assert_row_shape,
    assert_the_scope_statement_reads_ids_not_display_rows,
    assert_counts_report_the_unjudgeable_papers,
    assert_term_legs_are_bound_only_to_a_narrowed_scope,
]
