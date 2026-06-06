from app.services.concept_merge_review import review_merge_candidates


class _ReviewLLM:
    configured = True

    def chat_json(self, messages, response_schema_hint):
        return """
        {
          "decisions": [
            {
              "candidate_id": "mc-1",
              "decision": "merge",
              "canonical_name": "voltage-controlled oscillator",
              "confidence": 0.96,
              "rationale": "VCO is the common acronym for voltage-controlled oscillator."
            },
            {
              "candidate_id": "mc-2",
              "decision": "keep_separate",
              "canonical_name": "",
              "confidence": 0.91,
              "rationale": "current mirror and current source are related but not identical."
            }
          ]
        }
        """


def test_review_merge_candidates_parses_decisions():
    candidates = [
        {"id": "mc-1", "canonical_a": "K-vco", "canonical_b": "K-voltage controlled oscillator", "score": 0.93},
        {"id": "mc-2", "canonical_a": "K-current mirror", "canonical_b": "K-current source", "score": 0.88},
    ]

    decisions = review_merge_candidates(_ReviewLLM(), candidates)

    assert decisions[0]["candidate_id"] == "mc-1"
    assert decisions[0]["decision"] == "merge"
    assert decisions[0]["confidence"] == 0.96
    assert decisions[1]["decision"] == "keep_separate"
