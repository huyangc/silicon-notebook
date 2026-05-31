"""Profile vocabularies for the LLM extraction stages.

Object type -> ordered payload-field list (mirrors gold `objects[].payload`
keys, scored type-strict). Relation type lists mirror gold `relations[]`. The
LLM is constrained to exactly these types; anything else is dropped in code.
"""
from __future__ import annotations

from typing import Dict, List

ARTICLE_OBJECTS: Dict[str, List[str]] = {
    "ArticleClaim": ["statement", "problem_addressed", "novelty"],
    "ArticleMethod": ["name", "description", "mechanism", "scale"],
    "ArchitectureComponent": ["name", "role", "mechanism"],
    "ScalingLaw": ["name", "statement", "governs"],
    "ExperimentSetup": ["setup", "controls", "metric"],
    # before/after/knowledge/reasoning/code_math hold ATOMIC numeric values that
    # match gold by substring; finding stays a short synthesized sentence.
    "ExperimentResult": ["setup", "finding", "metric", "before", "after",
                         "knowledge", "reasoning", "code_math", "note"],
    "AblationFinding": ["component", "finding", "evidence"],
    "MechanisticExplanation": ["mechanism", "explains"],
    "SystemDesignClaim": ["claim", "mechanism", "benefit"],
    "Limitation": ["statement"],
    "Implication": ["statement"],
}
ARTICLE_RELATIONS: List[str] = [
    "method_has_component", "component_mitigates_risk", "method_addresses_problem",
    "result_supports_claim", "experiment_tests_claim",
    "ablation_supports_component_importance", "mechanism_explains_result",
    "system_design_enables_efficiency", "claim_guided_by_scaling_law",
]

TEXTBOOK_OBJECTS: Dict[str, List[str]] = {
    "Concept": ["term", "definition", "contrasts_with"],
    "Definition": ["term", "definition"],
    "Formula": ["name", "expression", "variables", "applies_to"],
    "Variable": ["symbol", "meaning"],
    "Derivation": ["name", "from", "to", "steps"],
    "ExampleProblem": ["title", "problem", "given"],
    "ExampleSolution": ["title", "approach", "result"],
    "TechnologyProcess": ["name", "purpose"],
    "ProcessFlow": ["name", "steps"],
    "ComponentModel": ["name", "properties"],
    "PhysicalEffect": ["name", "description"],
    "DesignPrinciple": ["statement", "rationale", "applies_to"],
    "DesignRule": ["statement", "condition"],
    "ProblemStatement": ["statement"],
}
TEXTBOOK_RELATIONS: List[str] = [
    "concept_defines_term", "concept_contrasts_with_concept", "formula_defines_variable",
    "formula_depends_on_variable", "formula_derived_from_formula", "formula_used_in_example",
    "example_uses_formula", "process_flow_has_step", "process_step_precedes_step",
    "circuit_block_composed_of_block", "component_has_property",
    "design_principle_applies_to_scenario",
]


# Typical endpoint types per relation (source -> target), to steer the LLM to
# pick type-correct endpoints. Advisory, not enforced.
ARTICLE_REL_SIGNATURES = {
    "method_has_component": "ArticleMethod -> ArchitectureComponent",
    "component_mitigates_risk": "ArchitectureComponent -> Limitation",
    "method_addresses_problem": "ArticleMethod -> ArticleClaim",
    "result_supports_claim": "ExperimentResult|ScalingLaw -> ArticleClaim",
    "experiment_tests_claim": "ExperimentResult -> ArticleMethod",
    "ablation_supports_component_importance": "AblationFinding -> ArchitectureComponent",
    "mechanism_explains_result": "MechanisticExplanation -> ExperimentResult",
    "system_design_enables_efficiency": "ArticleMethod -> SystemDesignClaim",
    "claim_guided_by_scaling_law": "ArticleMethod -> ScalingLaw",
}
TEXTBOOK_REL_SIGNATURES = {
    "concept_defines_term": "Concept -> Definition",
    "concept_contrasts_with_concept": "Concept -> Concept",
    "formula_defines_variable": "Formula -> Variable",
    "formula_depends_on_variable": "Formula -> Variable",
    "formula_derived_from_formula": "Formula -> Formula",
    "formula_used_in_example": "Formula -> ExampleProblem",
    "example_uses_formula": "ExampleProblem -> Formula",
    "process_flow_has_step": "ProcessFlow -> TechnologyProcess",
    "process_step_precedes_step": "TechnologyProcess -> TechnologyProcess",
    "circuit_block_composed_of_block": "ComponentModel -> ComponentModel",
    "component_has_property": "ComponentModel -> PhysicalEffect",
    "design_principle_applies_to_scenario": "DesignPrinciple -> Concept",
}


def relation_signatures(profile: str) -> Dict[str, str]:
    return ARTICLE_REL_SIGNATURES if profile == "article_research" else TEXTBOOK_REL_SIGNATURES


def object_types(profile: str) -> Dict[str, List[str]]:
    return ARTICLE_OBJECTS if profile == "article_research" else TEXTBOOK_OBJECTS


def relation_types(profile: str) -> List[str]:
    return ARTICLE_RELATIONS if profile == "article_research" else TEXTBOOK_RELATIONS


def extraction_targets(profile: str) -> List[str]:
    return list(object_types(profile).keys())
