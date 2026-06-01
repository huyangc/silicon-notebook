import json

from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.llm_atomizer import llm_atomize


SRC = ("An analog signal is defined over a continuous range.  "
       "A digital signal is defined only at discrete values. Filler chatter here.")


class FakeClient:
    configured = True
    def client(self): return None
    def chat_json(self, messages, schema_hint):
        # one verbatim, one whitespace-different (double space -> single), one paraphrase
        return json.dumps({"atoms": [
            {"text": "An analog signal is defined over a continuous range.",
             "type": "concept_definition_atom"},
            {"text": "A digital signal is defined only at discrete values.",
             "type": "definition_atom"},
            {"text": "Signals can be analog or digital.",  # paraphrase -> not in source
             "type": "concept_definition_atom"},
        ]})


def _para():
    # one paragraph element spanning the whole SRC
    return SourceElementQ(id="SE1", type="paragraph", file="x.md", line_start=1,
                          line_end=1, char_start=0, char_end=len(SRC), text=SRC)


def test_llm_atoms_are_verbatim_grounded_and_paraphrase_dropped():
    atoms = llm_atomize(SRC, [_para()], "SEC", "textbook", FakeClient())
    # paraphrase dropped -> 2 atoms
    assert len(atoms) == 2
    for a in atoms:
        s = a.source_span
        # the hard invariant: span maps back to verbatim source text
        assert SRC[s.char_start:s.char_end] == a.raw_text
    assert atoms[0].raw_text.startswith("An analog signal")
    assert atoms[0].atom_type == "concept_definition_atom"
    assert atoms[1].atom_type == "definition_atom"


def test_bad_type_falls_back_not_crash():
    class C(FakeClient):
        def chat_json(self, m, s):
            return json.dumps({"atoms": [
                {"text": "A digital signal is defined only at discrete values.",
                 "type": "NOT_A_TYPE"}]})
    atoms = llm_atomize(SRC, [_para()], "SEC", "textbook", C())
    assert len(atoms) == 1
    assert atoms[0].atom_type == "concept_definition_atom"  # default fallback
