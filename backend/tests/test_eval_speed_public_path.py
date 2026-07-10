from app.eval.speed import _insert_source
from app.repositories.ports import UploadedSourceFile

class Repo:
    def upload_sources(self, notebook_id, files, scheduler=None):
        files = list(files)
        assert isinstance(files[0], UploadedSourceFile)
        return [type('S', (), {'id':'src'})()]

def test_speed_uses_public_upload_path():
    assert _insert_source(Repo(), 'nb', 'x.md', 'text', '/tmp') == 'src'
