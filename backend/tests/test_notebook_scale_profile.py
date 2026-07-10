from unittest.mock import Mock
from app.core.config import Settings
from app.services.notebook_scale import NotebookScaleFacts, NotebookScaleProfile
from app.services.vector_cache import VectorCache

def profile(facts=None, version=None):
    facts = facts or Mock()
    return NotebookScaleProfile(Settings(index_suggest_chunk_threshold=10), facts, version or (lambda _: ('v',)), VectorCache())

def test_facts_size_dict():
    assert NotebookScaleFacts(1,2,3,4,5).as_size_dict() == {'bytes':1,'sources':2,'chunks':3,'nodes':4,'edges':5}

def test_copy_stats_cached_key_and_version():
    repo=Mock(); repo.load_notebook_scale_facts.return_value=NotebookScaleFacts(1,2,3,4,5)
    p=profile(repo, lambda _: 'v1')
    assert p.copy_stats('n') == p.copy_stats('n')
    repo.load_notebook_scale_facts.assert_called_once_with('n')
    p.version_for=lambda _: 'v2'
    p.copy_stats('n')
    assert repo.load_notebook_scale_facts.call_count == 2

def test_index_eligible_short_circuits_without_facts():
    repo=Mock(); p=profile(repo)
    assert p.index_eligible('n', tier='base', has_disk_index=False, total_chunks=0)
    assert p.index_eligible('n', tier='personal', has_disk_index=True, total_chunks=0)
    assert p.index_eligible('n', tier='personal', has_disk_index=False, total_chunks=11)
    repo.load_notebook_scale_facts.assert_not_called()

def test_requires_index_and_predicate_order():
    repo=Mock(); repo.load_notebook_scale_facts.return_value=NotebookScaleFacts(10**9,0,0,0,0)
    p=NotebookScaleProfile(Settings(notebook_copy_max_bytes=1), repo, lambda _: 'v', VectorCache())
    assert p.requires_index('n', has_disk_index=False)
    assert not p.requires_index('n', has_disk_index=True)
    assert repo.load_notebook_scale_facts.call_count == 1
