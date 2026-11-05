"""Saved shares retain exact reference identities without private source access."""
from __future__ import annotations

import json
import re

import pytest

from app.domain.share_import import encode_manifest, split_manifest
from app.services.parsers import parse_markdown_text
from app.services.public_share_import import build_saved_share


def _turn(text="结论 [k1]。", title="来源甲"):
    return {"question": "问题", "answer_md": text, "references": [
        {"key": "k1", "title": title, "snippet": "原文摘录", "location": "第 2 页",
         "evidence": {"kind": "original", "text": "前文。原文摘录。后文。", "quote": "原文摘录", "truncated": False}}]}


def test_rounds_with_same_reference_number_remain_distinct_and_portable(tmp_path):
    payload = {"turns": [_turn(), _turn(title="来源乙")]}
    content = build_saved_share(payload, kind="conversations", title="收藏")
    assert content == build_saved_share(payload, kind="conversations", title="收藏")
    manifest, body = split_manifest(content.decode())
    assert "[t1-k1]" in body and "[t2-k1]" in body
    assert manifest["turns"][0]["references"][0]["title"] == "来源甲"
    assert manifest["turns"][1]["references"][0]["title"] == "来源乙"
    path = tmp_path / "saved.md"
    path.write_bytes(content)
    from app.services.parsers import parse_markdown
    first = parse_markdown("src-copy", path)
    second = parse_markdown("src-reparsed", path)
    assert first[0].metadata["share_import"] == second[0].metadata["share_import"]
    linked = [element for element in first if element.metadata.get("share_references")]
    assert linked
    assert {ref["key"] for element in linked for ref in element.metadata["share_references"]} == {"t1-k1", "t2-k1"}
    assert not any("silicon-notebook-share:" in element.text for element in first)
    # KG reads the original file independently of the source-element parser.
    # Its raw spans/windows must begin after the portable manifest envelope.
    from app.domain.kg.parsing import parse_elements
    from app.services.kg.windowing import windows_with_elements
    raw = content.decode()
    kg_elements = parse_elements(raw, "saved.md")
    assert kg_elements and all("silicon-notebook-share:" not in item.text for item in kg_elements)
    assert all("silicon-notebook-share:" not in raw[window.char_start:window.char_end]
               for window, _elements in windows_with_elements(raw, "saved.md"))


def test_grouped_and_numeric_references_preserve_literal_code_and_math():
    turn = _turn("【k1，k2】 `[k1]` $x[k1]$\n\n```txt\n[k1]\n```\n\n$$\nx[k1]\n$$\n\n[1]")
    turn["references"] += [{"key": "k2", "title": "乙"}, {"key": "1", "title": "旧引用"}]
    _manifest, body = split_manifest(build_saved_share({"turns": [turn]}, kind="conversations", title="标题").decode())
    assert "[t1-k1][t1-k2]" in body
    assert "`[k1]`" in body and "$x[k1]$" in body and "```txt\n[k1]\n```" in body
    assert "$$\nx[k1]\n$$" in body
    assert "[t1-1]" in body


@pytest.mark.parametrize("marker,expected", [
    ("【1】", "[t1-1]"),
    ("【1，2】", "[t1-1][t1-2]"),
    ("[1、2]", "[t1-1][t1-2]"),
    ("【k1，1】", "[t1-k1][t1-1]"),
])
def test_saved_numeric_markers_match_the_public_renderer(marker, expected):
    turn = _turn(f"结论{marker}。普通数字括号 (1) 与（2）不变。")
    turn["references"] += [{"key": "1", "title": "数字引用甲"}, {"key": "2", "title": "数字引用乙"}]
    _manifest, body = split_manifest(build_saved_share({"turns": [turn]}, kind="conversations", title="保存数字引用").decode())
    assert f"结论{expected}。" in body
    assert "普通数字括号 (1) 与（2）不变。" in body
    parsed = parse_markdown_text("saved-numeric", build_saved_share({"turns": [turn]}, kind="conversations", title="保存数字引用").decode())
    linked = [ref for element in parsed if element.text.startswith("结论")
              for ref in element.metadata.get("share_references", [])]
    assert {ref["key"] for ref in linked} == set(re.findall(r"\[(t1-[^]]+)\]", expected))


@pytest.mark.parametrize("references", [None, 3, "bad", {}])
def test_malformed_user_uploaded_manifest_does_not_crash_parser(references):
    text = encode_manifest({"version": 1, "turns": [{"references": references}]}) + "# 普通文档\n\n内容"
    parsed = parse_markdown_text("src", text)
    assert parsed
    assert all("share_import" not in element.metadata for element in parsed)


def test_manifest_unknown_fields_and_private_handles_are_not_trusted():
    text = encode_manifest({"version": 1, "turns": [{"references": [None, 4, {"key": False},
        {"key": "t1-k1", "source_id": "secret", "evidence": {"kind": "original", "text": "内容", "url": "private"}}]}]}) + "正文 [t1-k1]"
    parsed = parse_markdown_text("src", text)
    metadata = parsed[0].metadata["share_import"]
    assert "secret" not in json.dumps(metadata)
    assert "private" not in json.dumps(metadata)
    assert len(metadata["turns"][0]["references"]) == 1


def test_excessively_nested_user_manifest_is_ordinary_markdown():
    import base64
    nested = b'{"version":1,"turns":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    text = "<!-- silicon-notebook-share:" + base64.b64encode(nested).decode() + " -->\n# 普通文档"
    assert split_manifest(text) == (None, text)
    assert parse_markdown_text("src", text)[0].text == "普通文档"


def test_saved_warning_and_failed_citation_remain_visible_and_structured():
    turn = _turn()
    turn["completeness_notice"] = "部分资料未包含。"
    turn["omitted_result_sets"] = 1
    turn["references"][0]["verification"] = "changed"
    data = build_saved_share({"turns": [turn]}, kind="conversations", title="核对结果").decode()
    manifest, body = split_manifest(data)
    ref = manifest["turns"][0]["references"][0]
    assert ref["verification"] == "changed" and "evidence" not in ref
    assert "部分资料未包含。" in body and "未通过核对：原文已改动" in body
    assert "结果卡未包含" in body


def test_saved_image_bytes_survive_original_link_and_asset_removal():
    turn = _turn()
    turn["images"] = [{"alias": "public-alias", "reference_keys": ["k1"], "caption": "图片说明"}]
    images = {"public-alias": ("image/png", b"image-content")}
    content = build_saved_share({"turns": [turn]}, kind="conversations", title="带图", read_image=images.__getitem__)
    images.clear()
    persisted = []
    def persist(data, name):
        persisted.append(data)
        return "local-asset"
    parsed = parse_markdown_text("local-source", content.decode(), persist_image=persist)
    assert persisted == [b"image-content"]
    image = next(item for item in parsed if item.element_type == "image")
    assert image.metadata["asset_id"] == "local-asset"
    assert "public-alias" not in json.dumps(image.metadata)
    with pytest.raises(ValueError, match="分享图片保存失败"):
        parse_markdown_text("src", content.decode(), persist_image=lambda *_: "")


def test_limits_reject_without_silent_omission():
    turn = _turn()
    turn["images"] = [{"alias": "image"}]
    with pytest.raises(ValueError, match="图片数量"):
        build_saved_share({"turns": [turn]}, kind="conversations", title="标题", max_images=0)
    with pytest.raises(ValueError, match="保存上限"):
        build_saved_share({"turns": [_turn()]}, kind="conversations", title="标题", max_bytes=1)


def test_report_and_single_turn_save_select_only_public_content():
    report = {"question": "研究", "content_md": "结论 [k1]", "references": _turn()["references"]}
    manifest, _body = split_manifest(build_saved_share(report, kind="reports", title="报告").decode())
    assert manifest["kind"] == "reports"
    payload = {"turns": [_turn(title="甲"), _turn(title="乙")]}
    selected, body = split_manifest(build_saved_share(payload, kind="conversations", title="单轮", turn_index=1).decode())
    assert len(selected["turns"]) == 1 and selected["turns"][0]["references"][0]["title"] == "乙"
    assert "[t2-k1]" in body and "[t1-k1]" not in body
    with pytest.raises(ValueError):
        build_saved_share(payload, kind="conversations", title="错误", turn_index=2)


@pytest.mark.parametrize("flag,notice", [
    ("question_truncated", "研究问题过长，已截断"),
    ("truncated_references", "引用清单不完整"),
])
def test_saved_report_preserves_public_truncation_disclosures(flag, notice):
    report = {"question": "公开问题前缀", "content_md": "公开正文 [k1]",
              "references": _turn()["references"], flag: True}
    content = build_saved_share(report, kind="reports", title="保存报告").decode()
    manifest, body = split_manifest(content)
    assert notice in body
    assert manifest["turns"][0][flag] is True
    parsed = parse_markdown_text("saved-report", content)
    assert parsed[0].metadata["share_import"]["turns"][0][flag] is True


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_public_conversation_asset_api import _build_client
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    _build_client(monkeypatch, tmp_path)
    return TestClient(create_app())


def test_save_endpoint_owns_destination_rechecks_link_and_reuses_source(client, monkeypatch):
    from pathlib import Path

    from app.services.kg import scheduler
    from tests.test_public_conversation_api import _new_user, _notebook, _reasoning_payload, _seed_shared_conversation
    monkeypatch.setattr(scheduler, "submit_job", lambda *_args, **_kwargs: None)
    with client:
        author, author_id = _new_user(client)
        recipient, _recipient_id = _new_user(client)
        original = _notebook(client, author)
        target = _notebook(client, recipient)
        answer = _reasoning_payload()
        answer["anchors"][0]["images"] = []
        cid = _seed_shared_conversation(original, author_id, answer)
        share = client.post(f"/api/notebooks/{original}/conversations/{cid}/share", headers=author)
        assert share.status_code == 200, share.text
        token = share.json()["share_token"]
        url = f"/api/public/conversations/{token}/save"
        page = client.get(f"/api/public/conversations/{token}").json()
        from app.domain.public_share_revision import public_share_revision
        body = {"notebook_id": target, "title": "我的收藏", "expected_revision": public_share_revision(page)}
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, headers=recipient, json={**body, "notebook_id": original}).status_code == 404
        assert client.post(url, headers=recipient, json={**body, "notebook_id": "missing-notebook"}).status_code == 404
        outdated = client.post(url, headers=recipient, json={**body, "expected_revision": "stale"})
        assert outdated.status_code == 409
        assert client.get(f"/api/notebooks/{target}/sources", headers=recipient).json()["items"] == []
        saved = client.post(url, json=body, headers=recipient)
        assert saved.status_code == 200, saved.text
        again = client.post(url, json=body, headers=recipient)
        assert again.status_code == 200, again.text
        assert again.json()["source_id"] == saved.json()["source_id"]
        assert again.json()["reused"] is True
        source = client.get(f"/api/sources/{saved.json()['source_id']}", headers=recipient).json()
        manifest, _text = split_manifest(Path(source["file_path"]).read_text())
        assert manifest["turns"][0]["references"][0]["snippet"] == "锚点摘录内容"
        # A visitor still viewing the old page must not save a newer window.
        from app.api.deps import repository
        with repository()._runtime.database.write() as db:
            db.execute(
                "INSERT INTO answers (id, notebook_id, conversation_id, question, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("new-public-answer", original, cid, "后来的问题", json.dumps(answer), "2026-01-02T00:00:01"),
            )
        advanced = client.post(f"/api/notebooks/{original}/conversations/{cid}/share", headers=author)
        assert advanced.status_code == 200, advanced.text
        assert client.post(url, json=body, headers=recipient).status_code == 409
        revoke = client.delete(f"/api/notebooks/{original}/conversations/{cid}/share", headers=author)
        assert revoke.status_code == 204
        assert client.post(url, json=body, headers=recipient).status_code == 404
        assert Path(source["file_path"]).exists()


def test_save_api_copies_public_images_before_original_assets_disappear(client, monkeypatch):
    from pathlib import Path

    from app.services.kg import scheduler
    from tests.test_public_conversation_api import _new_user, _notebook, _seed_shared_conversation
    from tests.test_public_conversation_asset_api import _IMAGE_BYTES, _payload_citing_asset, _save_asset
    monkeypatch.setattr(scheduler, "submit_job", lambda *_args, **_kwargs: None)
    with client:
        author, author_id = _new_user(client)
        recipient, _recipient_id = _new_user(client)
        original, target = _notebook(client, author), _notebook(client, recipient)
        asset_id = _save_asset(original, author_id)
        cid = _seed_shared_conversation(original, author_id, _payload_citing_asset(asset_id))
        shared = client.post(f"/api/notebooks/{original}/conversations/{cid}/share", headers=author)
        token = shared.json()["share_token"]
        page = client.get(f"/api/public/conversations/{token}").json()
        from app.domain.public_share_revision import public_share_revision
        saved = client.post(f"/api/public/conversations/{token}/save", headers=recipient,
                            json={"notebook_id": target, "title": "图片收藏", "expected_revision": public_share_revision(page)})
        assert saved.status_code == 200, saved.text
        source = client.get(f"/api/sources/{saved.json()['source_id']}", headers=recipient).json()
        copied = []
        parse_markdown_text("target-source", Path(source["file_path"]).read_text(),
                            persist_image=lambda data, _name: copied.append(data) or "target-asset")
        assert copied == [_IMAGE_BYTES]
