"""What a data room accepts: a type allowlist read from the bytes, never from the file's name
(docs/19 §3; "Data rooms, as built").

`inspect_upload(data, file_name)` returns an `Accepted` (the type, its media type, a cleaned name)
or a `Refusal` (a reason code from `models.REFUSAL_REASONS` and the sentence the user reads). The
rules, in order:

1. **Empty** files are refused, and so is anything over the size cap (the caller passes it).
2. **Executables and scripts by their magic**: Windows (`MZ`), ELF, Mach-O, Java class files and
   anything starting `#!` are refused by name of what they are.
3. **Archives**: ZIP (unless it is a Word, Excel or PowerPoint file, below), RAR, 7-Zip, gzip, bzip2,
   xz and tar are refused, and so is a file of an accepted type that is also a ZIP (a polyglot such as
   a PDF with an archive appended), because `zipfile` finds an archive in it.
4. **Legacy and password-protected Office files** (the OLE2 compound format: `.doc`, `.xls`, `.ppt`,
   and any Office file with a password) are refused: a macro inside one cannot be checked here.
5. **Accepted types**, each recognised by its own bytes:
   - **PDF** (`%PDF-` in the first 1,024 bytes). Refused when its uncompressed structure names
     `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile` or `/RichMedia` (hex escapes such as
     `/J#61vaScript` are decoded first). A name inside a compressed object stream is not seen: that
     is a malware scanner's job, and the page says what is and is not scanned.
   - **Word, Excel, PowerPoint** (`.docx`, `.xlsx`, `.pptx`): a ZIP read as an Office package
     (`_ooxml`). `[Content_Types].xml` and every relationships part (`*.rels`) are parsed as XML, so
     character references such as `macro&#69;nabled` are decoded and comments are not content; a
     DTD or an entity declaration is refused before anything is expanded (`_xml_elements`). The
     format is the content type of the part the package's one `officeDocument` relationship points
     at, which must be exactly that format's main type. Refused as macros or active content
     (`macros`): any macro-enabled, VBA, ActiveX or OLE-object content type, any content type of a
     whole document inside the package (an embedded package), parts named like a VBA project, an
     ActiveX control or `embeddings/`, and relationships of an OLE object, an embedded package, a VBA
     project, an ActiveX control or an imported chunk (`aFChunk`). Refused as loading something
     from outside the file (`external_content`): an attached template, a link to another workbook,
     and any relationship whose target is external (`TargetMode` other than `Internal`, or a target
     that is a URL or a network path), except a hyperlink, which opens only when clicked. Also
     refused: an encrypted entry, two entries of one name, and XML that is not well-formed.
     Templates, slide shows and add-ins are not accepted.
   - **PNG, JPEG, TIFF** images by their signatures. SVG is not accepted: it is XML that can carry
     script.
   - **CSV and plain text**: only when the name ends `.csv` or `.txt`, the bytes are UTF-8 (a BOM
     allowed) with no control characters but tab, line breaks and form feed, and the text does not
     start like markup (`<`), a script (`#!`) or RTF (`{\\rtf`, which can embed objects).

The type decides the `Content-Type` a download is served with; the name decides nothing but what the
page shows. A name whose extension is not one of its type's gets the type's own appended
(`report.docx` that is really a PDF is stored as `report.docx.pdf`), so a download never opens in the
wrong program.

Nothing here logs: a file's name and its bytes never reach a log line (docs/19 §5.3).
"""

from __future__ import annotations

import io
import posixpath
import re
import unicodedata
import xml.parsers.expat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn
from urllib.parse import unquote

from services.projects.models import FILE_NAME_MAX_CHARS


@dataclass(frozen=True)
class DocumentType:
    key: str
    label: str
    media_type: str
    extensions: tuple[str, ...]


#: The allowlist (`models.DOCUMENT_TYPES` holds the same keys; a test pins the two equal).
TYPES: Final[Mapping[str, DocumentType]] = {
    t.key: t
    for t in (
        DocumentType("pdf", "PDF", "application/pdf", (".pdf",)),
        DocumentType(
            "docx",
            "Word",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            (".docx",),
        ),
        DocumentType(
            "xlsx",
            "Excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            (".xlsx",),
        ),
        DocumentType(
            "pptx",
            "PowerPoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            (".pptx",),
        ),
        DocumentType("png", "PNG image", "image/png", (".png",)),
        DocumentType("jpeg", "JPEG image", "image/jpeg", (".jpg", ".jpeg")),
        DocumentType("tiff", "TIFF image", "image/tiff", (".tif", ".tiff")),
        DocumentType("csv", "CSV", "text/csv; charset=utf-8", (".csv",)),
        DocumentType("txt", "Text", "text/plain; charset=utf-8", (".txt",)),
    )
}

#: What each refusal tells the user. No sentence names the file: the page already shows which one.
REFUSAL_TEXT: Final[Mapping[str, str]] = {
    "empty": "The file is empty.",
    "too_large": "The file is larger than the limit for one file.",
    "type_not_accepted": (
        "This type of file is not accepted. Upload PDF, Word, Excel or PowerPoint (without macros), "
        "PNG, JPEG or TIFF images, or CSV or plain text."
    ),
    "executable": "Programs are not accepted.",
    "script": "Scripts are not accepted.",
    "archive": (
        "Archives (ZIP, RAR, 7-Zip and the like) are not accepted. Upload the files inside one by one."
    ),
    "macros": (
        "Office files with macros, ActiveX controls or embedded objects are not accepted. "
        "Save it without them, or as a PDF."
    ),
    "external_content": (
        "Office files that load something from outside the file when opened (an attached template, "
        "a linked picture or object, a link to another workbook) are not accepted. "
        "Remove the links, or save it as a PDF."
    ),
    "legacy_office": (
        "Older Office files (.doc, .xls, .ppt) and password-protected Office files are not accepted. "
        "Save it as .docx, .xlsx or .pptx without a password, or as a PDF."
    ),
    "encrypted": "Encrypted or password-protected files are not accepted.",
    "malware": "The virus scanner flagged this file, so it was not stored.",
    "quota": "Your storage allowance is full. Delete files you no longer need, then try again.",
}


@dataclass(frozen=True)
class Accepted:
    doc_type: DocumentType
    file_name: str


@dataclass(frozen=True)
class Refusal:
    reason: str

    @property
    def text(self) -> str:
        return REFUSAL_TEXT[self.reason]


# ------------------------------------------------------------------------------------ names
#: Characters a stored name never keeps: controls (Cc), format characters (Cf, which include the
#: bidirectional overrides that make `fdp.exe` read as `exe.pdf`), surrogates and unassigned points.
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def clean_file_name(raw: str, doc_type: DocumentType) -> str:
    """The name a member sees: the last path segment, NFC, no control or direction characters,
    whitespace collapsed, at most `FILE_NAME_MAX_CHARS`, ending in one of its type's extensions."""
    name = display_name(raw)
    lowered = name.lower()
    if not any(lowered.endswith(ext) for ext in doc_type.extensions):
        name = f"{name}{doc_type.extensions[0]}"
    if len(name) > FILE_NAME_MAX_CHARS:
        stem, dot, ext = name.rpartition(".")
        keep = FILE_NAME_MAX_CHARS - len(ext) - 2  # the dot and an ellipsis
        name = f"{stem[:keep].rstrip()}…{dot}{ext}"
    return name


def display_name(raw: str) -> str:
    """A name as cleaned for showing, without the extension rule: what a refused upload was called."""
    name = re.split(r"[/\\]", raw)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(
        " " if ch.isspace() else ch
        for ch in name
        if ch.isspace() or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )
    name = " ".join(name.split()).strip(" .") or "document"
    if len(name) > FILE_NAME_MAX_CHARS:
        name = name[: FILE_NAME_MAX_CHARS - 1].rstrip() + "…"
    return name


def _extension(raw: str) -> str:
    name = re.split(r"[/\\]", raw)[-1].strip().lower()
    _, dot, ext = name.rpartition(".")
    return f".{ext}" if dot else ""


# ------------------------------------------------------------------------------------ magic
_EXECUTABLE_MAGIC: Final = (
    b"MZ",  # Windows PE/COFF and DOS programs
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # Mach-O universal binaries and Java class files
    b"dex\n",  # Android
    b"\x00asm",  # WebAssembly
)
_ARCHIVE_MAGIC: Final = (
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",  # gzip
    b"BZh",
    b"\xfd7zXZ\x00",
    b"MSCF",  # Windows cabinet
    b"\x28\xb5\x2f\xfd",  # zstd
)
_OLE2_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC: Final = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: Final = b"\xff\xd8\xff"
_TIFF_MAGIC: Final = (b"II*\x00", b"MM\x00*")
_PDF_MAGIC: Final = b"%PDF-"
#: A PDF header may follow up to 1,024 bytes of other data (ISO 32000-1, annex H.3 note 13).
_PDF_HEADER_WINDOW: Final = 1024

#: A PDF name token (ISO 32000 7.3.5): `/` then regular characters.
_PDF_NAME = re.compile(rb"/([^\s/<>\[\]()%{}]{2,32})")
_PDF_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_ACTIVE_NAMES: Final = frozenset(
    {b"JavaScript", b"JS", b"Launch", b"EmbeddedFile", b"EmbeddedFiles", b"RichMedia"}
)

#: OOXML: the main part's content type, by format. Templates (`.dotx`, `.xltx`, `.potx`), slide shows
#: and add-ins have other main content types and are not accepted.
_OOXML_MAIN: Final[Mapping[str, str]] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
#: A content type, a relationship type or a part name that means macros or active content, read
#: after XML decoding (`macro&#69;nabled` is `macroEnabled`): macro-enabled formats, VBA projects and
#: their data and signatures, ActiveX controls, OLE objects.
_OOXML_ACTIVE = re.compile(r"macroenabled|vba|activex|oleobject", re.IGNORECASE)
_OOXML_ACTIVE_PARTS = re.compile(
    r"(?:^|/)(?:vbaProject[^/]*\.bin|vbaData\.xml|activeX/|embeddings/)", re.IGNORECASE
)
#: The content types of a whole document. A part of one of these types inside a package is an
#: embedded package (a workbook inside a deck, a document inside a document), which is not checked.
_OOXML_PACKAGE_TYPES: Final = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
        "application/vnd.openxmlformats-officedocument.presentationml.template",
        "application/vnd.openxmlformats-officedocument.presentationml.slide",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.visio",
        "application/vnd.ms-visio.drawing",
        "application/x-msdownload",
        "application/zip",
        "application/x-zip-compressed",
        "application/java-archive",
    }
)
#: Relationship types refused, by the last segment of the type's URI (lower case), whichever schema
#: namespace names them (transitional, strict or Microsoft's): an embedded object, an embedded
#: package, an ActiveX control, an imported chunk of other content (`aFChunk`, which can carry RTF
#: with objects in it) and a sub-document. Any type matching `_OOXML_ACTIVE` is refused too.
_OOXML_ACTIVE_RELATIONSHIPS: Final = frozenset({"oleobject", "package", "control", "afchunk", "subdocument"})
#: Relationship types that load something from outside the file when it is opened, refused even
#: with an internal target: the attached template (remote-template injection: opening the file
#: fetches it, and a network path hands over the user's Windows credentials) and links to other
#: workbooks (which also carry DDE and OLE links).
_OOXML_EXTERNAL_RELATIONSHIPS: Final = frozenset({"attachedtemplate", "externallink", "externallinkpath"})
#: The one relationship type allowed an external target: a hyperlink opens only when clicked.
_OOXML_HYPERLINK: Final = "hyperlink"
#: The package relationship that names the main part (transitional and strict schemas; compared in
#: lower case).
_OOXML_OFFICE_DOCUMENT: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officedocument/2006/relationships/officedocument",
        "http://purl.oclc.org/ooxml/officedocument/relationships/officedocument",
    }
)
_OOXML_RELATIONSHIPS_TYPE: Final = "application/vnd.openxmlformats-package.relationships+xml"
_OOXML_CONTENT_TYPES_PART: Final = "[content_types].xml"
_OOXML_PACKAGE_RELATIONSHIPS_PART: Final = "_rels/.rels"
#: A target that is not a part of this package: a URI scheme (`https:`, `file:`, a drive letter)
#: or a network path (`//host`, `\\host`).
_EXTERNAL_TARGET = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|\\\\)", re.IGNORECASE)
#: Zip-bomb and abuse bounds: the parts are never extracted, but their declared sizes are capped,
#: and only `[Content_Types].xml` and the relationships parts are read, each at most
#: `_OOXML_XML_PART_MAX_BYTES` and all together at most `_OOXML_XML_TOTAL_MAX_BYTES`.
_OOXML_MAX_ENTRIES: Final = 10_000
_OOXML_MAX_DECLARED_BYTES: Final = 2 * 1024**3
_OOXML_XML_PART_MAX_BYTES: Final = 1024**2
_OOXML_XML_TOTAL_MAX_BYTES: Final = 16 * 1024**2

_TEXT_EXTENSIONS: Final[Mapping[str, str]] = {".csv": "csv", ".txt": "txt"}
#: Allowed in text besides printable characters: tab, line feed, carriage return, form feed.
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


def _is_zip(data: bytes) -> bool:
    try:
        return zipfile.is_zipfile(io.BytesIO(data))
    except Exception:  # any parse failure means "not an archive zipfile can open"
        return False


def _is_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


def _pdf_refusal(data: bytes) -> Refusal | None:
    for match in _PDF_NAME.finditer(data):
        name = match.group(1)
        if b"#" in name:
            name = _PDF_HEX.sub(lambda m: bytes([int(m.group(1), 16)]), name)
        if name in _PDF_ACTIVE_NAMES:
            return Refusal("script")
    return None


class _NotAPackage(Exception):
    """An XML part that is not well-formed, declares a DTD or an entity, is ambiguous or is larger
    than the bounds, or a part that cannot be read: not an Office package this module accepts."""


def _refuse_declaration(*_args: object) -> NoReturn:
    raise _NotAPackage("a DTD or an entity declaration")


def _local(name: str) -> str:
    """`uri local` (the parser's namespace form) or `local` -> `local`."""
    return name.rsplit(" ", 1)[-1]


#: One element: its local name and its attributes as (local name, value) pairs. A list, not a dict:
#: `ContentType` and `x:ContentType` on one element are two values, and both are read.
_Element = tuple[str, list[tuple[str, str]]]


def _xml_elements(data: bytes) -> list[_Element]:
    """Every element of one XML part, in order, with character and predefined entity references
    decoded and comments dropped.

    Defused as `defusedxml` defuses `expat` (which is not a dependency here): a document type
    declaration, an entity or notation declaration and an external entity reference are refused
    before anything is expanded, and parameter entities are never parsed, so nothing is expanded but
    the five predefined entities and character references. Office packages carry no DTD. XML that
    is not well-formed raises `_NotAPackage`."""
    elements: list[_Element] = []
    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_declaration
    parser.EntityDeclHandler = _refuse_declaration
    parser.UnparsedEntityDeclHandler = _refuse_declaration
    parser.NotationDeclHandler = _refuse_declaration
    parser.ExternalEntityRefHandler = _refuse_declaration

    def start(name: str, attributes: dict[str, str]) -> None:
        elements.append((_local(name), [(_local(key), value) for key, value in attributes.items()]))

    parser.StartElementHandler = start
    try:
        parser.Parse(data, True)
    except xml.parsers.expat.ExpatError:
        raise _NotAPackage("not well-formed XML") from None
    return elements


def _values(attributes: list[tuple[str, str]], name: str) -> list[str]:
    """Every value of the attributes of this local name, in any namespace and any case."""
    return [value for key, value in attributes if key.lower() == name.lower()]


def _attribute(attributes: list[tuple[str, str]], name: str) -> str | None:
    """The one value of the attribute of this local name (`_values`), or `None`. Two of them (`Type`
    and `x:Type`) are ambiguous, since which one a reader takes is the reader's choice; every value
    is checked for active content before this is asked."""
    values = _values(attributes, name)
    if len(values) > 1:
        raise _NotAPackage(f"two {name} attributes on one element")
    return values[0] if values else None


def _part_name(raw: str) -> str:
    """A ZIP entry name or an OPC part name in one comparable form: forward slashes, percent-escapes
    decoded once, no leading slash, lower case (OPC compares part names case-insensitively)."""
    return unquote(raw.replace("\\", "/")).lstrip("/").lower()


def _media(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _relationship_tail(relationship_type: str) -> str:
    """The last segment of a relationship type's URI, in lower case: `oleobject` for any schema's
    `.../relationships/oleObject`."""
    return unquote(relationship_type).strip().rstrip("/").rsplit("/", 1)[-1].lower()


@dataclass
class _Package:
    archive: zipfile.ZipFile
    #: Comparable part name (`_part_name`) -> its entry.
    entries: dict[str, zipfile.ZipInfo]
    defaults: dict[str, str]
    overrides: dict[str, str]
    xml_bytes_read: int = 0

    def read_xml(self, info: zipfile.ZipInfo) -> list[_Element]:
        if info.file_size > _OOXML_XML_PART_MAX_BYTES:
            raise _NotAPackage("an XML part over the bound")
        try:
            with self.archive.open(info) as handle:
                data = handle.read(_OOXML_XML_PART_MAX_BYTES + 1)
        except (zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError, RuntimeError):
            raise _NotAPackage("a part that cannot be read") from None
        self.xml_bytes_read += len(data)
        if len(data) > _OOXML_XML_PART_MAX_BYTES or self.xml_bytes_read > _OOXML_XML_TOTAL_MAX_BYTES:
            raise _NotAPackage("XML parts over the bound")
        return _xml_elements(data)

    def content_type(self, part: str) -> str | None:
        """OPC's rule: the part's `Override`, else the `Default` for its extension."""
        if part in self.overrides:
            return self.overrides[part]
        _, dot, extension = posixpath.basename(part).rpartition(".")
        return self.defaults.get(extension) if dot else None


def _content_types(package: _Package, info: zipfile.ZipInfo) -> Refusal | None:
    """Reads `[Content_Types].xml` into the package: a refusal (`macros`) when any content type it
    declares is active content or a whole document (an embedded package)."""
    for _name, attributes in package.read_xml(info):
        for value in _values(attributes, "ContentType"):
            if _OOXML_ACTIVE.search(value) or _media(value) in _OOXML_PACKAGE_TYPES:
                return Refusal("macros")
        declared = _attribute(attributes, "ContentType")
        if declared is None:
            continue
        extension = _attribute(attributes, "Extension")
        if extension is not None and package.defaults.setdefault(extension.lower(), declared) != declared:
            raise _NotAPackage("two defaults for one extension")
        part = _attribute(attributes, "PartName")
        if part is not None and package.overrides.setdefault(_part_name(part), declared) != declared:
            raise _NotAPackage("two overrides for one part")
    return None


def _relationships(package: _Package) -> Refusal | None:
    """Reads every relationships part (named `*.rels`, or declared as one): a refusal for an active
    relationship type (`macros`), or for one that loads something from outside the file
    (`external_content`)."""
    external = False
    for part, info in sorted(package.entries.items()):
        declared = package.content_type(part) or ""
        if not part.endswith(".rels") and _media(declared) != _OOXML_RELATIONSHIPS_TYPE:
            continue
        for _name, attributes in package.read_xml(info):
            for value in _values(attributes, "Type"):
                if _relationship_tail(value) in _OOXML_ACTIVE_RELATIONSHIPS or _OOXML_ACTIVE.search(
                    unquote(value)
                ):
                    return Refusal("macros")
            relationship_type = _attribute(attributes, "Type")
            if relationship_type is None:
                continue
            tail = _relationship_tail(relationship_type)
            mode = _attribute(attributes, "TargetMode")
            target = unquote(_attribute(attributes, "Target") or "").strip()
            leaves = (mode is not None and mode.strip().lower() != "internal") or bool(
                _EXTERNAL_TARGET.match(target)
            )
            if tail in _OOXML_EXTERNAL_RELATIONSHIPS or (leaves and tail != _OOXML_HYPERLINK):
                external = True
    return Refusal("external_content") if external else None


def _main_part_type(package: _Package) -> DocumentType | Refusal:
    """The format: the content type of the part the package's one `officeDocument` relationship
    points at, which must be exactly one format's main type, and the part must be there."""
    info = package.entries.get(_OOXML_PACKAGE_RELATIONSHIPS_PART)
    if info is None:
        return Refusal("type_not_accepted")
    targets = []
    for _name, attributes in package.read_xml(info):
        if (_attribute(attributes, "Type") or "").strip().lower() not in _OOXML_OFFICE_DOCUMENT:
            continue
        if (_attribute(attributes, "TargetMode") or "Internal").strip().lower() != "internal":
            return Refusal("external_content")
        targets.append(_attribute(attributes, "Target") or "")
    if len(targets) != 1:
        return Refusal("type_not_accepted")
    resolved = posixpath.normpath(posixpath.join("/", unquote(targets[0]).replace("\\", "/")))
    main = resolved.lstrip("/").lower()
    declared = package.content_type(main)
    if main not in package.entries or declared is None:
        return Refusal("type_not_accepted")
    found = [key for key, main_type in _OOXML_MAIN.items() if _media(declared) == main_type]
    return TYPES[found[0]] if found else Refusal("type_not_accepted")


def _ooxml(data: bytes) -> DocumentType | Refusal:
    """A Word, Excel or PowerPoint file without active content, or why not (module docstring)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return Refusal("archive")
    if len(infos) > _OOXML_MAX_ENTRIES or sum(i.file_size for i in infos) > _OOXML_MAX_DECLARED_BYTES:
        return Refusal("archive")
    if any(i.flag_bits & 0x1 for i in infos):
        return Refusal("encrypted")
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if info.is_dir():
            continue
        if entries.setdefault(_part_name(info.filename), info) is not info:
            # Two entries a reader may take for one part: which is "the" part is the reader's choice,
            # so the one checked here need not be the one Office opens.
            return Refusal("type_not_accepted")
    content_types = entries.get(_OOXML_CONTENT_TYPES_PART)
    if content_types is None:
        return Refusal("archive")
    if any(_OOXML_ACTIVE_PARTS.search(part) for part in entries):
        return Refusal("macros")
    package = _Package(archive=archive, entries=entries, defaults={}, overrides={})
    try:
        return _content_types(package, content_types) or _relationships(package) or _main_part_type(package)
    except _NotAPackage:
        return Refusal("type_not_accepted")


def _text(data: bytes, file_name: str) -> DocumentType | Refusal:
    key = _TEXT_EXTENSIONS.get(_extension(file_name))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return Refusal("type_not_accepted")
    head = text.lstrip()
    if head.startswith("#!"):
        return Refusal("script")
    if key is None:
        return Refusal("type_not_accepted")
    if _TEXT_CONTROL.search(text) or head.startswith(("<", "{\\rtf")):
        return Refusal("type_not_accepted")
    return TYPES[key]


def detect(data: bytes, file_name: str) -> DocumentType | Refusal:
    """The accepted type the bytes are, or why not (module docstring, rules 2 to 5)."""
    head = data[:16]
    if head.startswith(b"#!"):
        return Refusal("script")
    if head.startswith(_EXECUTABLE_MAGIC):
        return Refusal("executable")
    if head.startswith(_OLE2_MAGIC):
        return Refusal("legacy_office")
    if head.startswith(_ARCHIVE_MAGIC) or _is_tar(data):
        return Refusal("archive")
    if head.startswith(_ZIP_MAGIC):
        return _ooxml(data)
    found: DocumentType | Refusal
    if _PDF_MAGIC in data[:_PDF_HEADER_WINDOW]:
        found = _pdf_refusal(data) or TYPES["pdf"]
    elif head.startswith(_PNG_MAGIC):
        found = TYPES["png"]
    elif head.startswith(_JPEG_MAGIC):
        found = TYPES["jpeg"]
    elif head.startswith(_TIFF_MAGIC):
        found = TYPES["tiff"]
    else:
        found = _text(data, file_name)
    if isinstance(found, DocumentType) and _is_zip(data):
        # An accepted type with an archive inside it (a polyglot): refused as an archive.
        return Refusal("archive")
    return found


def inspect_upload(data: bytes, file_name: str, *, max_bytes: int) -> Accepted | Refusal:
    """Rule 1, then `detect`, then the cleaned name (module docstring)."""
    if not data:
        return Refusal("empty")
    if len(data) > max_bytes:
        return Refusal("too_large")
    found = detect(data, file_name)
    if isinstance(found, Refusal):
        return found
    return Accepted(doc_type=found, file_name=clean_file_name(file_name, found))


def accepted_types() -> list[dict[str, object]]:
    """The allowlist as the API and the page describe it."""
    return [{"type": t.key, "label": t.label, "extensions": list(t.extensions)} for t in TYPES.values()]
