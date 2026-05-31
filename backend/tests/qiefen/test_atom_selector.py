import json

from app.services.qiefen.models import EvidenceAtom, SourceSpan
from app.services.qiefen.atom_selector import select_core_atoms


def _atom(aid, t="concept_definition_atom", text="some text here that is long enough"):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type=t, source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1, line_end=1,
                                               char_start=0, char_end=1),
                        raw_text=text, normalized_text=text)


class FakeClient:
    def __init__(self, core): self._core = core
    def chat_json(self, messages, schema_hint):
        return json.dumps({"core_atom_ids": self._core})


def test_selects_returned_ids_filtered_to_batch():
    atoms = [_atom("A1"), _atom("A2"), _atom("A3")]
    keep = select_core_atoms(FakeClient(["A1", "A3", "BOGUS"]), atoms, "textbook")
    assert keep == {"A1", "A3"}  # BOGUS not an atom -> dropped


def test_fail_open_keeps_batch_on_empty_or_error():
    atoms = [_atom("A1"), _atom("A2")]
    # model returns nothing usable -> keep the whole batch (never lose everything)
    assert select_core_atoms(FakeClient([]), atoms, "textbook") == {"A1", "A2"}

    class Boom:
        def chat_json(self, m, s): raise RuntimeError("x")
    assert select_core_atoms(Boom(), atoms, "textbook") == {"A1", "A2"}
