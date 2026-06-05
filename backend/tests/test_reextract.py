def test_reextract_notebook_loops_all_sources_in_order():
    from app.services.reextract import reextract_notebook

    class _Summary:
        def __init__(self, sid): self.id = sid

    class FakeRepo:
        def __init__(self): self.extracted = []
        def list_sources(self, notebook_id): return [_Summary("s1"), _Summary("s2")]
        def extract_source(self, source_id): self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s1", "s2"]
    assert repo.extracted == ["s1", "s2"]


def test_reextract_notebook_continues_on_source_error():
    from app.services.reextract import reextract_notebook

    class _Summary:
        def __init__(self, sid): self.id = sid

    class FakeRepo:
        def __init__(self): self.extracted = []
        def list_sources(self, notebook_id): return [_Summary("s1"), _Summary("s2")]
        def extract_source(self, source_id):
            if source_id == "s1":
                raise RuntimeError("boom")
            self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s2"]
