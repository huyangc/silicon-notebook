def test_reextract_notebook_loops_all_sources_in_order():
    from app.services.reextract import reextract_notebook

    class FakeRepo:
        def __init__(self):
            self.extracted = []
            self.maintenance = self
        def user_source_ids_page(self, notebook_id, *, after_id="", limit=500):
            return [sid for sid in ("s1", "s2") if sid > after_id][:limit]
        def extract_source(self, source_id): self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s1", "s2"]
    assert repo.extracted == ["s1", "s2"]


def test_reextract_notebook_continues_on_source_error():
    from app.services.reextract import reextract_notebook

    class FakeRepo:
        def __init__(self):
            self.extracted = []
            self.maintenance = self
        def user_source_ids_page(self, notebook_id, *, after_id="", limit=500):
            return [sid for sid in ("s1", "s2") if sid > after_id][:limit]
        def extract_source(self, source_id):
            if source_id == "s1":
                raise RuntimeError("boom")
            self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s2"]
