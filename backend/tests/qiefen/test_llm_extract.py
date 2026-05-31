from app.services.qiefen.models import ContextPackage
from app.services.qiefen.llm_extract import (
    safe_json, strip_fences, select_prompt_atoms, build_object_prompt,
    MAX_PROMPT_ATOMS,
)


def test_safe_json_handles_fences_and_garbage():
    assert safe_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert safe_json("not json{") == {}
    assert safe_json("") == {}
    assert safe_json("[1,2]") == {}  # non-dict top level -> {}


def test_strip_fences():
    assert strip_fences("```json\n{}\n```") == "{}"
    assert strip_fences("{}") == "{}"


def _pkg(atoms):
    return ContextPackage(id="PKG-1", profile="article_research", chunk_id="C1",
                          section_path="Abstract", document_title="Engram", atoms=atoms)


def test_select_prompt_atoms_caps_and_prioritizes_high_value():
    # "given_atom" is low-value filler; the trailing formula is high-value.
    atoms = ([{"atom_id": f"L{i}", "atom_type": "given_atom"} for i in range(40)]
             + [{"atom_id": "F1", "atom_type": "formula_atom"}])
    sel = select_prompt_atoms(_pkg(atoms))
    assert len(sel) == MAX_PROMPT_ATOMS
    # formula_atom (high-value) survives the cap even though it is last
    assert any(a["atom_id"] == "F1" for a in sel)


def test_build_object_prompt_includes_ids_text_and_types():
    pkg = _pkg([{"atom_id": "A1", "atom_type": "claim_sentence"}])
    prompt = build_object_prompt(pkg, "article_research", {"A1": "Conditional memory complements MoE."})
    assert "A1" in prompt
    assert "Conditional memory complements MoE." in prompt
    assert "ArticleClaim" in prompt          # allowed types listed
    assert "Abstract" in prompt              # section path
