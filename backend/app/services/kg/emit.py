"""KnowledgeGraph -> ordered YAML."""
from __future__ import annotations
import yaml
from app.services.kg.models import KnowledgeGraph

def to_yaml(g: KnowledgeGraph) -> str:
    return yaml.safe_dump(g.to_dict(), sort_keys=False, allow_unicode=True, width=1000)
