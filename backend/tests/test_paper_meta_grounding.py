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


# --- Fix round 1: malformed-shape guards, anchored year/DOI, byline rotations ---


def test_authors_as_plain_strings_grounded():
    meta = verify_paper_meta(
        _base(authors=["Ashish Vaswani", "Noam Shazeer"]), HEAD, model="m"
    )
    assert [a["name"] for a in meta["authors"]] == ["Ashish Vaswani", "Noam Shazeer"]
    assert meta["authors"][0]["position"] == 0
    assert meta["authors"][1]["position"] == 1
    assert meta["authors"][0]["affiliation"] == ""
    assert meta["authors"][1]["affiliation"] == ""


def test_malformed_shapes_degrade_without_crashing():
    for bad_authors in ("garbage", 123):
        meta = verify_paper_meta(_base(authors=bad_authors), HEAD, model="m")
        assert meta["authors"] == []
    for bad_keywords in ("transformer", 123):
        meta = verify_paper_meta(_base(keywords=bad_keywords), HEAD, model="m")
        assert meta["keywords"] == []


def test_year_as_float_coerced():
    meta = verify_paper_meta(_base(year=2017.0), HEAD, model="m")
    assert meta["pub_year"] == 2017


def test_year_inside_doi_not_grounded_but_standalone_is():
    head = "X paper\ndoi:10.1109/ABCD.2031.999999\n"
    meta = verify_paper_meta(_base(year=2031), head, model="m")
    assert meta["pub_year"] is None
    assert meta["dropped"]["year"] == 2031

    head_with_year = head + "Published in 2031.\n"
    meta2 = verify_paper_meta(_base(year=2031), head_with_year, model="m")
    assert meta2["pub_year"] == 2031


def test_doi_requires_exact_token_match_not_prefix():
    meta = verify_paper_meta(_base(doi="10.5555/329"), HEAD, model="m")
    assert meta["doi"] is None
    assert meta["dropped"]["doi"] == "10.5555/329"

    meta_exact = verify_paper_meta(_base(doi="10.5555/3295222"), HEAD, model="m")
    assert meta_exact["doi"] == "10.5555/3295222"

    head_trailing = "Some text\ndoi:10.9999/abc.def.\n"
    meta_trailing = verify_paper_meta(
        _base(doi="10.9999/abc.def"), head_trailing, model="m"
    )
    assert meta_trailing["doi"] == "10.9999/abc.def"


def test_author_name_rotation_handles_last_first_middle_byline():
    head = "Vaswani, Ashish Noam"
    meta = verify_paper_meta(
        {
            "is_paper": True,
            "title": "",
            "venue": "",
            "year": None,
            "authors": [{"name": "Ashish Noam Vaswani", "affiliations": []}],
            "doi": "",
            "keywords": [],
        },
        head,
        model="m",
    )
    assert [a["name"] for a in meta["authors"]] == ["Ashish Noam Vaswani"]


# --- Fix round 2: nested affiliations shape, arXiv-ID year blanking, unicode DOI punct ---


def test_affiliations_as_plain_string_treated_as_single_entry():
    meta = verify_paper_meta(
        _base(authors=[{"name": "Ashish Vaswani", "affiliations": "Google Brain"}]),
        HEAD,
        model="m",
    )
    assert meta["authors"][0]["name"] == "Ashish Vaswani"
    assert meta["authors"][0]["affiliation"] == "Google Brain"


def test_affiliations_as_int_degrades_to_empty_without_crashing():
    meta = verify_paper_meta(
        _base(authors=[{"name": "Ashish Vaswani", "affiliations": 7}]),
        HEAD,
        model="m",
    )
    assert meta["authors"][0]["name"] == "Ashish Vaswani"
    assert meta["authors"][0]["affiliation"] == ""


def test_year_inside_arxiv_id_not_grounded_but_standalone_is():
    head = "Y paper\narXiv:2007.12345v2\n"
    meta = verify_paper_meta(_base(year=2007), head, model="m")
    assert meta["pub_year"] is None
    assert meta["dropped"]["year"] == 2007

    meta2 = verify_paper_meta(
        _base(year=2007), head + "Published in 2007.\n", model="m"
    )
    assert meta2["pub_year"] == 2007


def test_doi_wrapped_in_unicode_quotes_accepted():
    head = "Some text\n“10.9999/abc”\n"
    meta = verify_paper_meta(_base(doi="10.9999/abc"), head, model="m")
    assert meta["doi"] == "10.9999/abc"
