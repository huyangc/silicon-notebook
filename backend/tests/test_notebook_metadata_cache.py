"""Notebook synthesis reuses validated model responses across corpus changes."""
import json
import re
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.services.notebook_metadata import synthesize_metadata


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value, *, tag=""):
        self.values[key] = value


@pytest.fixture
def cached_client(monkeypatch):
    cache = MemoryCache()
    blocks = []

    def complete(**kwargs):
        prompt = kwargs["messages"][-1]["content"]
        block = prompt.split("Sources:\n", 1)[1]
        blocks.append(block)
        topics = list(dict.fromkeys(re.findall(r"topic-\d+(?:-revised)?", block)))
        response = json.dumps({"name": "资料综合", "description": ", ".join(topics)})
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=response), finish_reason="stop",
            )],
            usage=None,
        )

    client = OpenAICompatibleClient(
        Settings(llm_cache_enabled=True, llm_log_enabled=False),
        base_url="https://metadata.example.test",
        api_key="test-key",
        model="metadata-test",
        cache=cache,
    )
    wire = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    monkeypatch.setattr(client, "client", lambda: wire)
    return client, cache, blocks


def summarize(client, records):
    return synthesize_metadata(client, records, batch_chars=4096, batch_sources=2)


def topics_in(description):
    return re.findall(r"topic-\d+(?:-revised)?", description)


def test_sequential_appends_reuse_completed_groups_and_retain_all_topics(cached_client):
    client, _, blocks = cached_client
    records = [f"- Source {index}: topic-{index}" for index in range(8)]
    _, description = summarize(client, records)
    assert topics_in(description) == [f"topic-{index}" for index in range(8)]
    original_leaf_prompts = set(block for block in blocks if "Source " in block)
    assert len(original_leaf_prompts) == 4

    for index in range(8, 12):
        before = len(blocks)
        records.append(f"- Source {index}: topic-{index}")
        _, description = summarize(client, records)
        new_calls = blocks[before:]
        assert topics_in(description) == [f"topic-{number}" for number in range(index + 1)]
        assert not original_leaf_prompts.intersection(new_calls)
        # Only the changed tail and its ancestors need a fresh model response.
        assert len(new_calls) <= 4
        assert any(f"Source {index}: topic-{index}" in block for block in new_calls)


def test_metadata_change_recomputes_only_affected_ancestors(cached_client):
    client, _, blocks = cached_client
    records = [f"- Source {index}: topic-{index}" for index in range(8)]
    summarize(client, records)
    before = len(blocks)
    records[2] = "- Source 2: topic-2-revised"

    _, description = summarize(client, records)

    expected = [f"topic-{index}" for index in range(8)]
    expected[2] = "topic-2-revised"
    assert topics_in(description) == expected
    changed_calls = blocks[before:]
    assert len(changed_calls) == 3  # Changed pair, its parent, and the final result.
    assert all("topic-2-revised" in block for block in changed_calls)
    assert sum("Source " in block for block in changed_calls) == 1
    after_change = len(blocks)
    assert summarize(client, records)[1] == description
    assert len(blocks) == after_change


@pytest.mark.parametrize("invalid", [
    "not json",
    "[]",
    '{"name": "", "description": "topic-0"}',
    '{"name": "标题", "description": "   "}',
    '{"name": 42, "description": "topic-0"}',
    json.dumps({"name": "x" * 121, "description": "topic-0"}),
    json.dumps({"name": "标题", "description": "x" * 1001}),
])
def test_malformed_cached_metadata_is_rejected(cached_client, invalid):
    client, cache, blocks = cached_client
    records = ["- Source 0: topic-0"]
    expected = summarize(client, records)
    assert len(cache.values) == 1
    key = next(iter(cache.values))
    cache.values[key] = invalid
    before = len(blocks)

    assert summarize(client, records) == expected

    assert len(blocks) == before + 1
    assert cache.values[key] != invalid
