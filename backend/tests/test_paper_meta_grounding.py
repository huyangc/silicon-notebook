"""接地校验(anti-hallucination)单测:不在头部文本中的字段不落库。"""
import json

from app.services.paper_meta import paper_meta_prompt, verify_paper_meta

HEAD = (
    "Attention Is All You Need\n"
    "Ashish Vaswani, Noam Shazeer, Niki Parmar\n"
    "Google Brain; Google Research\n"
    "Published at NIPS 2017. doi:10.5555/3295222\n"
    "Keywords: transformer, attention\n"
    "Abstract: The dominant sequence transduction models ..."
)


def _base(**over):
    data = {
        "is_paper": True,
        "title": "Attention Is All You Need",
        "authors": [
            {"name": "Ashish Vaswani", "affiliations": ["Google Brain"]},
            {"name": "Noam Shazeer", "affiliations": ["Google Research"]},
        ],
        "venue": "NIPS", "year": 2017, "doi": "10.5555/3295222",
        "keywords": ["transformer", "attention"],
    }
    data.update(over)
    return data


def test_grounded_fields_survive():
    meta = verify_paper_meta(_base(), HEAD, model="m1")
    assert meta["is_paper"] is True
    assert meta["paper_title"] == "Attention Is All You Need"
    assert [a["name"] for a in meta["authors"]] == ["Ashish Vaswani", "Noam Shazeer"]
    assert meta["authors"][0]["position"] == 0
    assert meta["authors"][0]["affiliation"] == "Google Brain"
    assert meta["venue"] == "NIPS" and meta["pub_year"] == 2017
    assert meta["doi"] == "10.5555/3295222"
    assert meta["keywords"] == ["transformer", "attention"]
    assert meta["model"] == "m1"
    assert json.loads(meta["raw_json"])["dropped"] == {}


def test_hallucinated_author_dropped_and_audited():
    meta = verify_paper_meta(
        _base(authors=_base()["authors"] + [{"name": "Geoffrey Hinton", "affiliations": []}]),
        HEAD, model="m")
    assert "Geoffrey Hinton" not in [a["name"] for a in meta["authors"]]
    assert json.loads(meta["raw_json"])["dropped"]["authors"] == ["Geoffrey Hinton"]
    assert meta["dropped"]["authors"] == ["Geoffrey Hinton"]


def test_unverifiable_affiliation_cleared_author_kept():
    meta = verify_paper_meta(
        _base(authors=[{"name": "Ashish Vaswani", "affiliations": ["MIT CSAIL"]}]),
        HEAD, model="m")
    assert meta["authors"][0]["name"] == "Ashish Vaswani"
    assert meta["authors"][0]["affiliation"] == ""
    assert "MIT CSAIL" in meta["dropped"]["affiliations"]


def test_name_normalization_tolerates_case_space_diacritics_and_order():
    head = "José García-López and Wei Zhang, 2023, ACM"
    meta = verify_paper_meta(
        {"is_paper": True, "title": "", "venue": "ACM", "year": 2023,
         "authors": [{"name": "Jose Garcia Lopez", "affiliations": []},
                     {"name": "Zhang, Wei", "affiliations": []}],
         "doi": "", "keywords": []},
        head, model="m")
    assert [a["name"] for a in meta["authors"]] == ["Jose Garcia Lopez", "Zhang, Wei"]


def test_venue_year_not_in_text_nulled():
    meta = verify_paper_meta(_base(venue="ICML", year=2021), HEAD, model="m")
    assert meta["venue"] is None and meta["pub_year"] is None
    assert meta["dropped"]["venue"] == "ICML" and meta["dropped"]["year"] == 2021


def test_doi_must_match_format_and_text():
    assert verify_paper_meta(_base(doi="not-a-doi"), HEAD, "m")["doi"] is None
    assert verify_paper_meta(_base(doi="10.1234/absent"), HEAD, "m")["doi"] is None


def test_year_range_guard():
    meta = verify_paper_meta(_base(year=222), HEAD, model="m")
    assert meta["pub_year"] is None


def test_not_paper_blanks_everything():
    meta = verify_paper_meta(_base(is_paper=False), HEAD, model="m")
    assert meta["is_paper"] is False
    assert meta["authors"] == [] and meta["paper_title"] is None
    assert meta["keywords"] == [] and meta["doi"] is None


def test_prompt_forbids_memory_fill():
    p = paper_meta_prompt("some text")
    assert "do NOT" in p and "memory" in p and "some text" in p
