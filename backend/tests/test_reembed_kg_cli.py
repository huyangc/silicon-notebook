from contextlib import contextmanager

import pytest

from app.scripts import reembed_kg


class _Maintenance:
    def __init__(self):
        self.purged = False

    def purge_kg_embeddings(self, notebook_id):
        self.purged = True


class _Repo:
    def __init__(self, configured):
        self._configured = configured
        self.maintenance = _Maintenance()

    def configured(self, workload_id):
        return workload_id in self._configured


@pytest.mark.parametrize(
    "configured,missing",
    [
        ({"relation_embedding"}, "knowledge_object_embedding"),
        ({"knowledge_object_embedding"}, "relation_embedding"),
    ],
)
def test_reembed_refuses_to_purge_until_both_workloads_are_bound(
    configured, missing, monkeypatch, capsys
):
    repo = _Repo(configured)
    @contextmanager
    def _open(_settings, **_kwargs):
        yield repo

    monkeypatch.setattr(reembed_kg, "open_maintenance_cli_repository", _open)
    monkeypatch.setattr(reembed_kg.sys, "argv", ["reembed_kg", "nb-1"])

    assert reembed_kg.main() == 2
    assert repo.maintenance.purged is False
    assert missing in capsys.readouterr().out
