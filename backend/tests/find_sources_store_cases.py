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

    def find(self, notebook_id: str = NB, **kwargs) -> dict[str, dict]:
        kwargs.setdefault("kind", "all")
        with self.connect() as db:
            rows = self.store.find_sources_metadata_rows(db, notebook_id, **kwargs)
        return {row["source_id"]: row for row in rows}

    def matches(self, terms, **kwargs) -> dict[str, dict]:
        return {
            source_id: row["matches"]
            for source_id, row in self.find(terms=list(terms), **kwargs).items()
            if row["matches"]
        }


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
    assert papers["s-paper"]["has_meta"] is True
    assert papers["s-paper"]["is_paper"] is True
    assert papers["s-pending"]["has_meta"] is False
    assert papers["s-pending"]["is_paper"] is None
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
    row = env.find()["s-a"]
    assert row == {
        "source_id": "s-a", "notebook_id": NB, "title": "Stored title",
        "source_type": "pdf", "doc_type": "academic_paper",
        "parse_status": "extracted", "has_meta": True, "is_paper": True,
        "paper_title": "Paper Title", "venue": "ICLR", "pub_year": 2024,
        "matches": {},
    }


ALL_CASES = [
    assert_every_field_matches,
    assert_terms_fold_ascii_only_and_or_together,
    assert_like_metacharacters_are_escaped,
    assert_short_ascii_terms_need_a_word_boundary,
    assert_keyword_matches_stay_inside_one_keyword,
    assert_filters_compose,
    assert_kind_papers_includes_unextracted_eligible_sources,
    assert_hidden_projections_and_other_libraries_never_appear,
    assert_allow_list_narrows_the_scope,
    assert_row_shape,
]
