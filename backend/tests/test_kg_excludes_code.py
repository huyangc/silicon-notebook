from app.services.kg.parsing import parse_elements
from app.services.kg.windowing import windows_with_elements

MD = (
    "## Section\n\n"
    "Some prose about ENGRAM and floorplanning.\n\n"
    "```tcl\nset_db design_process_node secret_var_42\nadd_ring -width 5\n```\n\n"
    "More prose about timing.\n"
)


def test_code_block_typed_as_code_block_not_prose():
    els = parse_elements(MD, "doc.md", None)
    code = [e for e in els if "secret_var_42" in e.text]
    assert code, "code element should still exist (for retrieval/citation)"
    assert code[0].type == "code_block", f"got {code[0].type}"


def test_code_content_never_enters_kg_windows():
    pairs = windows_with_elements(MD, "doc.md", None, 9000, 450)
    for _w, wels in pairs:
        for e in wels:
            assert "secret_var_42" not in e.text, "code must not be fed to KG extraction"
    # sanity: prose IS still windowed
    assert any("ENGRAM" in e.text for _w, wels in pairs for e in wels)
