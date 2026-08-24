from __future__ import annotations

import io
import zipfile

import pytest

from app.services.parsers import (
    MARKDOWN_BUNDLE_MAX_ENTRIES,
    parse_markdown_bundle,
    parse_source_file,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
JPEG = b"\xff\xd8\xff" + b"payload"


def _zip(tmp_path, entries: dict[str, bytes], name: str = "bundle.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry_name, payload in entries.items():
            archive.writestr(entry_name, payload)
    return path


def test_bundle_resolves_relative_and_percent_encoded_images(tmp_path):
    path = _zip(
        tmp_path,
        {
            "质量和流程/notes.filled.md": (
                "![流程图](images/图片%2012.jpg)\n\n"
                "![架构图](../shared/arch.png?rev=2#top)\n"
            ).encode(),
            "质量和流程/images/图片 12.jpg": JPEG,
            "shared/arch.png": PNG,
        },
    )
    persisted: list[tuple[bytes, str]] = []

    def persist(data: bytes, name: str) -> str:
        persisted.append((data, name))
        return f"asset-{len(persisted)}"

    elements = parse_markdown_bundle(
        "src-1", path, persist_image=persist, max_uncompressed_bytes=4096
    )

    images = [element for element in elements if element.element_type == "image"]
    assert [element.metadata["asset_id"] for element in images] == [
        "asset-1", "asset-2"
    ]
    assert all(
        element.metadata["bundle_path"] == "质量和流程/notes.filled.md"
        for element in images
    )
    assert all("data:" not in repr(element.metadata) for element in images)
    assert persisted == [(JPEG, "bundle-img-1.jpg"), (PNG, "bundle-img-2.png")]


def test_bundle_parses_every_markdown_member_as_one_source(tmp_path):
    path = _zip(
        tmp_path,
        {
            "a/README.md": b"# A\n\nalpha\n",
            "b/README.markdown": b"# B\n\nbeta\n",
            "ignored.txt": b"not a source document",
        },
    )

    elements = parse_source_file("src-1", str(path), "notes.zip")

    assert {element.metadata["bundle_path"] for element in elements} == {
        "a/README.md", "b/README.markdown"
    }
    assert any(element.text == "alpha" for element in elements)
    assert any(element.text == "beta" for element in elements)
    assert all("README" in element.location_label for element in elements)


def test_bundle_missing_or_unsupported_image_fails_open_to_caption(tmp_path):
    path = _zip(
        tmp_path,
        {
            "notes.md": b"![missing](images/no.png)\n\n![svg](images/a.svg)\n",
            "images/a.svg": b"<svg/>",
        },
    )

    elements = parse_markdown_bundle(
        "src-1", path, persist_image=lambda *_: "unexpected",
        max_uncompressed_bytes=4096,
    )

    images = [element for element in elements if element.element_type == "image"]
    assert [element.text for element in images] == ["missing", "svg"]
    assert all("asset_id" not in element.metadata for element in images)
    assert [element.metadata["src"] for element in images] == [
        "images/no.png", "images/a.svg"
    ]


@pytest.mark.parametrize(
    "entries, message",
    [
        ({"readme.txt": b"plain"}, "contains no Markdown"),
        ({"../notes.md": b"unsafe"}, "unsafe path"),
    ],
)
def test_bundle_rejects_invalid_archive_shapes(tmp_path, entries, message):
    path = _zip(tmp_path, entries)
    with pytest.raises(ValueError, match=message):
        parse_markdown_bundle("src-1", path, max_uncompressed_bytes=4096)


def test_bundle_rejects_declared_total_and_entry_count(tmp_path):
    too_large = _zip(tmp_path, {"notes.md": b"12345"}, "large.zip")
    with pytest.raises(ValueError, match="too large after decompression"):
        parse_markdown_bundle("src-1", too_large, max_uncompressed_bytes=4)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.md", b"ok")
        for index in range(MARKDOWN_BUNDLE_MAX_ENTRIES):
            archive.writestr(f"images/{index}.png", PNG)
    many = tmp_path / "many.zip"
    many.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match="too many files"):
        parse_markdown_bundle(
            "src-1", many, max_uncompressed_bytes=len(buffer.getvalue()) * 2
        )
