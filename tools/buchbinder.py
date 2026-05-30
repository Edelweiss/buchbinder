#!/usr/bin/env python3
"""
Buchbinder – merge the single ODT section files of the novel "Edelweiß" into a
complete book (or per chapter) and export to PDF, EPUB, HTML or ODT.

The section files live in ``data/edelweiss`` and are named

    NNNN_Type_Title.odt        e.g.  0203_Story_Bibliothek.odt

where the first two digits are the chapter number, digits three and four are the
section index inside the chapter, ``Type`` is ``Story`` or ``Journal`` and
``Title`` is a CamelCase version of the section title.

Approach
--------
Every section is an OpenDocument Text file that shares the same paragraph- and
character-style template (serif "Story" body, italic "Journal" body, the
``eo_*`` character styles for foreign-language passages, poem formatting via
automatic paragraph styles, …).  Instead of re-implementing that formatting we
*merge* the sections into a single self-contained ODT, carrying every style
along.  Because the automatic style names (``P1``, ``T3``, ``L1`` …) collide
between files, each section's automatic styles are renamed with a per-section
prefix and the references in that section's body are rewritten accordingly.

The merged ODT is then handed to LibreOffice in headless mode
(``soffice --convert-to``) to produce the requested output format.  A single
merged document converts cleanly to PDF, EPUB and HTML alike (a LibreOffice
*master* document, by contrast, only expands its linked sections for PDF/HTML
but not for EPUB).

See ``docs/USAGE.md`` for the full command-line reference and behaviour notes.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Paths / configuration
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_SOURCE = os.path.join(PROJECT_ROOT, "data", "edelweiss")
DEFAULT_PUBLISH = os.path.join(PROJECT_ROOT, "data", "publish")

# Candidate locations for the LibreOffice "soffice" binary.
SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
    "soffice",
    "libreoffice",
]

# LibreOffice export filter names per target format.
EXPORT_FILTERS = {
    "pdf": "pdf:writer_pdf_Export",
    "epub": "epub:EPUB",
    "html": "html:HTML (StarWriter)",
}

SECTION_RE = re.compile(r"^(?P<num>\d{4})_(?P<type>Story|Journal)_(?P<title>.+)\.odt$")

# Named styles (from the shared template) referenced by the paragraphs we
# generate ourselves.  ``_5b_``/``_5d_``/``_7e_`` are ODF encodings of ``[]~``.
STYLE_STORY_JOURNAL = "Textk\u00f6rper_20__5b_Story_7e_Journal_5d_"

# Default location of the book configuration (titles, metadata, …).
DEFAULT_CONFIG = os.path.join(
    DEFAULT_SOURCE, "Sammelbindungen", "Buchbinder", "buchbinder.ini"
)


# --------------------------------------------------------------------------- #
# Configuration (buchbinder.ini)
# --------------------------------------------------------------------------- #


@dataclass
class Meta:
    """Document metadata, sourced from the ini file."""

    title: str = ""
    subject: str = ""
    author: str = ""
    website: str = ""
    year: str = ""
    keywords: List[str] = field(default_factory=list)
    disclaimer: str = ""


@dataclass
class BookConfig:
    book: Meta
    journal: Meta
    chapter_titles: Dict[int, str]      # chapter number -> title
    entry_titles: Dict[int, Tuple[str, str]]  # entry number -> (title, place/date)


def _split_keywords(value: str) -> List[str]:
    return [k.strip() for k in re.split(r"[,\n\t]+", value or "") if k.strip()]


def load_config(path: Optional[str]) -> BookConfig:
    """Read the buchbinder.ini.  Missing file/sections degrade gracefully."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # keep keys case-sensitive
    if path and os.path.exists(path):
        cp.read(path, encoding="utf-8")

    def meta_from(section: str, base: Optional[Meta] = None) -> Meta:
        base = base or Meta()
        s = cp[section] if cp.has_section(section) else {}
        return Meta(
            title=s.get("title", base.title),
            subject=s.get("subject", base.subject),
            author=s.get("author", base.author),
            website=s.get("website", base.website),
            year=s.get("year", base.year),
            keywords=_split_keywords(s["keywords"]) if s.get("keywords") else list(base.keywords),
            disclaimer=s.get("disclaimer", base.disclaimer),
        )

    book = meta_from("book")
    journal = meta_from("journal", base=book)  # journal inherits unspecified values

    chapter_titles: Dict[int, str] = {}
    if cp.has_section("book_chapters"):
        for key, val in cp["book_chapters"].items():
            m = re.match(r"chapter_(\d+)$", key)
            if m:
                chapter_titles[int(m.group(1))] = val.strip()

    entry_titles: Dict[int, Tuple[str, str]] = {}
    if cp.has_section("journal_entries"):
        for key, val in cp["journal_entries"].items():
            m = re.match(r"entry_(\d+)$", key)
            if m:
                title, _, place = val.partition(";")
                entry_titles[int(m.group(1))] = (title.strip(), place.strip())

    return BookConfig(book=book, journal=journal,
                      chapter_titles=chapter_titles, entry_titles=entry_titles)


_ROMAN = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def roman(n: int) -> str:
    out = []
    for value, sym in _ROMAN:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)



# --------------------------------------------------------------------------- #
# Section discovery
# --------------------------------------------------------------------------- #


@dataclass
class Section:
    num: str          # "0203"
    chapter: int      # 2
    index: int        # 3
    kind: str         # "Story" | "Journal"
    title: str        # CamelCase title from the file name
    path: str
    entry_index: Optional[int] = None   # 1-based journal-entry number (journals only)
    entry_first: bool = False           # True for the first part of a journal entry

    @property
    def display_title(self) -> str:
        # Turn "ListeDerWinter" into "Liste Der Winter".
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", self.title)
        spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", spaced)
        return spaced

    @property
    def base_title(self) -> str:
        # Drop a trailing part number: "DasBlaueBand2" -> "DasBlaueBand".
        return re.sub(r"\d+$", "", self.title)



def discover_sections(source_dir: str) -> List[Section]:
    """Return the section files in ``source_dir`` sorted by their numeric prefix."""
    sections: List[Section] = []
    for name in os.listdir(source_dir):
        m = SECTION_RE.match(name)
        if not m:
            continue
        num = m.group("num")
        sections.append(
            Section(
                num=num,
                chapter=int(num[:2]),
                index=int(num[2:]),
                kind=m.group("type"),
                title=m.group("title"),
                path=os.path.join(source_dir, name),
            )
        )
    sections.sort(key=lambda s: s.num)
    return sections


def group_by_chapter(sections: List[Section]) -> "Dict[int, List[Section]]":
    chapters: Dict[int, List[Section]] = {}
    for s in sections:
        chapters.setdefault(s.chapter, []).append(s)
    return dict(sorted(chapters.items()))


def assign_journal_entries(sections: List[Section]) -> None:
    """Number the journal entries across the whole book.

    Journal section files that share the same base title with an appended part
    number (e.g. ``DasBlaueBand`` + ``DasBlaueBand2``) form a single diary entry.
    Walking the journal sections in reading order, a new entry begins whenever
    the base title changes; ``entry_first`` marks the first part of each entry.
    """
    entry = 0
    prev_base: Optional[str] = None
    for s in sorted((s for s in sections if s.kind == "Journal"), key=lambda s: s.num):
        if s.base_title != prev_base:
            entry += 1
            s.entry_first = True
            prev_base = s.base_title
        else:
            s.entry_first = False
        s.entry_index = entry



# --------------------------------------------------------------------------- #
# ODT parsing helpers
# --------------------------------------------------------------------------- #


def _inner(tag: str, xml: str) -> str:
    """Return the inner XML of the first ``<office:TAG> ... </office:TAG>`` block.

    Handles the self-closing ``<office:TAG/>`` case (returns "").
    """
    m = re.search(r"<office:%s\b[^>]*?/>" % tag, xml)
    if m:
        return ""
    m = re.search(r"<office:%s\b[^>]*?>(.*?)</office:%s>" % (tag, tag), xml, re.S)
    return m.group(1) if m else ""


def _root_namespaces(xml: str, root_tag: str) -> str:
    """Extract the ``xmlns:*`` attribute soup from a document root element."""
    m = re.search(r"<%s\s+(.*?)office:version" % re.escape(root_tag), xml, re.S)
    return m.group(1) if m else ""


@dataclass
class ParsedSection:
    fonts: str            # inner of office:font-face-decls (content.xml)
    auto_styles: str      # inner of office:automatic-styles (content.xml), namespaced
    body: str             # section content (namespaced), without forms/sequence-decls
    styles_named: str     # inner of office:styles (styles.xml)
    styles_fonts: str     # inner of office:font-face-decls (styles.xml)
    styles_auto: str      # inner of office:automatic-styles (styles.xml)
    master_styles: str    # inner of office:master-styles (styles.xml)
    content_ns: str       # namespace soup of document-content
    styles_ns: str        # namespace soup of document-styles


def _namespace_auto_styles(auto_inner: str, body: str, prefix: str) -> Tuple[str, str]:
    """Rename every automatic style defined in ``auto_inner`` with ``prefix`` and
    rewrite the references both inside the style block and in ``body``.
    """
    names = set(re.findall(r'style:name="([^"]+)"', auto_inner))
    if not names:
        return auto_inner, body

    attr_re = re.compile(r'(style:name|[\w-]+:[\w-]*style-name)="([^"]*)"')

    def repl(m: "re.Match") -> str:
        attr, val = m.group(1), m.group(2)
        if val in names:
            return '%s="%s%s"' % (attr, prefix, val)
        return m.group(0)

    return attr_re.sub(repl, auto_inner), attr_re.sub(repl, body)


def parse_section(path: str, index: int, keep_comments: bool) -> ParsedSection:
    with zipfile.ZipFile(path) as z:
        content = z.read("content.xml").decode("utf-8")
        styles_xml = z.read("styles.xml").decode("utf-8")

    content_ns = _root_namespaces(content, "office:document-content")
    styles_ns = _root_namespaces(styles_xml, "office:document-styles")

    fonts = _inner("font-face-decls", content)
    auto_inner = _inner("automatic-styles", content)

    # Body: inner of <office:text>, minus the forms element and the
    # sequence-decls (those are emitted once for the whole merged document).
    m = re.search(r"<office:text\b[^>]*>(.*)</office:text>", content, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r"<office:forms\b[^>]*?/>", "", body, count=1)
    body = re.sub(r"<office:forms\b.*?</office:forms>", "", body, count=1, flags=re.S)
    body = re.sub(r"<text:sequence-decls>.*?</text:sequence-decls>", "", body, count=1, flags=re.S)

    if not keep_comments:
        body = re.sub(r"<office:annotation\b.*?</office:annotation>", "", body, flags=re.S)
        body = re.sub(r"<office:annotation-end\b[^>]*?/>", "", body)

    # The chapter-opening headings carry style:master-page-name="Right_20_Page",
    # which forces a page break before them.  Drop it so chapters flow
    # continuously; the only deliberate break is the one after the front matter.
    auto_inner = re.sub(r'\s*style:master-page-name="[^"]*"', "", auto_inner)

    prefix = "bx%d_" % index
    auto_inner, body = _namespace_auto_styles(auto_inner, body, prefix)

    return ParsedSection(
        fonts=fonts,
        auto_styles=auto_inner,
        body=body,
        styles_named=_inner("styles", styles_xml),
        styles_fonts=_inner("font-face-decls", styles_xml),
        styles_auto=_inner("automatic-styles", styles_xml),
        master_styles=_inner("master-styles", styles_xml),
        content_ns=content_ns,
        styles_ns=styles_ns,
    )


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def _union_fonts(*chunks: str) -> str:
    seen = set()
    out = []
    font_re = re.compile(r"<style:font-face\b[^>]*?(?:/>|>.*?</style:font-face>)", re.S)
    name_re = re.compile(r'style:name="([^"]+)"')
    for chunk in chunks:
        for m in font_re.finditer(chunk or ""):
            frag = m.group(0)
            nm = name_re.search(frag)
            key = nm.group(1) if nm else frag
            if key in seen:
                continue
            seen.add(key)
            out.append(frag)
    return "".join(out)


def _union_named_styles(base: str, *others: str) -> str:
    """Union of top-level named styles, keyed by ``style:name``.

    ``base`` is kept verbatim (including unnamed entries such as default-style
    and notes configuration); from the other sections only styles whose name is
    not already present are appended.
    """
    have = set(re.findall(r'style:name="([^"]+)"', base))
    extra = []
    block_re = re.compile(
        r"<(style:style|text:list-style)\b[^>]*?>.*?</\1>"
        r"|<(style:style|text:list-style)\b[^>]*?/>",
        re.S,
    )
    name_re = re.compile(r'style:name="([^"]+)"')
    for chunk in others:
        for m in block_re.finditer(chunk or ""):
            frag = m.group(0)
            nm = name_re.search(frag)
            if not nm:
                continue
            if nm.group(1) in have:
                continue
            have.add(nm.group(1))
            extra.append(frag)
    return base + "".join(extra)


# -- generated paragraphs / styles ----------------------------------------- #

GENERATED_AUTO_STYLES = """\
<style:style style:name="BB_Chapter" style:family="paragraph" \
style:parent-style-name="Heading_20_1"><style:paragraph-properties \
fo:text-align="center" style:justify-single-word="false" fo:margin-top="3cm" \
fo:margin-bottom="1.5cm"/></style:style>\
<style:style style:name="BB_ChapterTitle" style:family="text">\
<style:text-properties fo:font-size="70%" fo:font-weight="normal" \
fo:font-variant="small-caps"/></style:style>\
<style:style style:name="BB_JournalEntry" style:family="paragraph" \
style:parent-style-name="Heading" style:default-outline-level="2"\
><style:paragraph-properties fo:margin-top="2cm" fo:margin-bottom="1cm" \
fo:text-align="end" style:justify-single-word="false" fo:keep-together="always" \
fo:keep-with-next="always" fo:border-bottom="0.06pt solid #cccccc" \
fo:padding-bottom="0.1cm"/><style:text-properties fo:color="#333333" \
fo:font-size="14pt" fo:font-style="italic" fo:font-weight="bold"/></style:style>\
<style:style style:name="BB_JournalWhen" style:family="text">\
<style:text-properties fo:font-size="80%" fo:font-weight="normal"/></style:style>\
<style:style style:name="BB_CoverPage" style:family="paragraph" \
style:parent-style-name="Standard"><style:paragraph-properties \
fo:margin-top="0cm" fo:margin-bottom="0cm" fo:margin-left="0cm" \
fo:margin-right="0cm" fo:padding="0cm" fo:border="none" \
fo:text-align="center" fo:break-after="page"/></style:style>\
<style:style style:name="BB_PageBreak" style:family="paragraph" \
style:parent-style-name="Standard"><style:paragraph-properties \
fo:break-after="page"/></style:style>\
<style:style style:name="BB_Tilde" style:family="paragraph" \
style:parent-style-name="__SJ__"><style:paragraph-properties \
fo:text-align="center" style:justify-single-word="false"/></style:style>\
<style:style style:name="BB_Title" style:family="paragraph" \
style:parent-style-name="Heading_20_1"><style:paragraph-properties \
fo:text-align="center" fo:margin-top="8cm"/><style:text-properties \
fo:font-size="40pt" style:font-size-asian="40pt" style:font-size-complex="40pt"/>\
</style:style>\
<style:style style:name="BB_Subtitle" style:family="paragraph" \
style:parent-style-name="Standard"><style:paragraph-properties \
fo:text-align="center"/><style:text-properties fo:font-size="16pt" \
fo:font-style="italic"/></style:style>\
""".replace("__SJ__", STYLE_STORY_JOURNAL)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _cover_page(img_inner_path: str, content_w: float, content_h: float) -> str:
    """ODF paragraph containing a full-content-area cover image frame.

    ``draw:name="cover"`` is the name LibreOffice's EPUB exporter uses to
    identify the cover image.  The frame is anchored *as-char* so it sits in
    the reading flow (necessary for EPUB export to pick it up).
    """
    w = "%.3fcm" % content_w
    h = "%.3fcm" % content_h
    return (
        '<text:p text:style-name="BB_CoverPage">'
        '<draw:frame draw:name="cover"'
        ' text:anchor-type="as-char"'
        ' svg:y="0cm"'
        ' svg:width="%s" svg:height="%s"'
        ' draw:z-index="0">'
        '<draw:image'
        ' xlink:href="%s"'
        ' xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>'
        '</draw:frame>'
        '</text:p>'
    ) % (w, h, img_inner_path)


def _chapter_heading(chapter: int, chapter_title: Optional[str]) -> str:
    """Chapter heading: the chapter number as a Roman numeral with, on a second
    line, the chapter title taken from the configuration."""
    head = (
        '<text:h text:style-name="BB_Chapter" text:outline-level="1">'
        '<text:bookmark text:name="chapter_%d"/>%s'
        % (chapter, roman(chapter))
    )
    if chapter_title:
        head += '<text:line-break/><text:span text:style-name="BB_ChapterTitle">%s</text:span>' % _esc(chapter_title)
    head += "</text:h>"
    return head


def _journal_entry_heading(title: str, when: str, level: int) -> str:
    """A small two-line journal-entry title (entry name + place/date)."""
    inner = _esc(title)
    if when:
        inner += '<text:line-break/><text:span text:style-name="BB_JournalWhen">%s</text:span>' % _esc(when)
    return (
        '<text:h text:style-name="BB_JournalEntry" text:outline-level="%d">%s</text:h>'
        % (level, inner)
    )



def _tilde() -> str:
    return '<text:p text:style-name="BB_Tilde">~</text:p>'


def _page_break() -> str:
    """An empty paragraph that forces a page break after it (front matter)."""
    return '<text:p text:style-name="BB_PageBreak"/>'


def _title_page(title: str, subtitle: Optional[str]) -> str:
    out = '<text:p text:style-name="BB_Title">%s</text:p>' % _esc(title)
    if subtitle:
        out += '<text:p text:style-name="BB_Subtitle">%s</text:p>' % _esc(subtitle)
    return out


def _toc(chapters: "Dict[int, List[Section]]", chapter_titles: "Dict[int, str]") -> str:
    """A generated table of contents (German: "Inhaltsverzeichnis").

    Rather than an ODF ``text:table-of-content`` field (which only renders once
    LibreOffice updates fields, something headless conversion does not do), the
    contents are written as ordinary paragraphs that link to a bookmark on each
    chapter heading.  This renders reliably in PDF, EPUB and HTML and stays
    clickable.
    """
    out = [
        '<text:h text:style-name="BB_Chapter" text:outline-level="1">'
        "Inhaltsverzeichnis</text:h>"
    ]
    for chapter in chapters:
        title = chapter_titles.get(chapter)
        label = "%s\u2003%s" % (roman(chapter), _esc(title)) if title else roman(chapter)
        out.append(
            '<text:p text:style-name="Contents_20_1">'
            '<text:a xlink:type="simple" xlink:href="#chapter_%d" '
            'text:style-name="Internet_20_link">%s</text:a></text:p>'
            % (chapter, label)
        )
    return "".join(out)


# -- ODT package writing ---------------------------------------------------- #

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
 <manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""

META = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" office:version="1.3">
 <office:meta>
%(fields)s  <meta:generator>Buchbinder</meta:generator>
 </office:meta>
</office:document-meta>
"""


def _build_meta_xml(meta: Meta) -> str:
    """Render the office:meta fields from a :class:`Meta` record."""
    lines: List[str] = []
    if meta.title:
        lines.append("  <dc:title>%s</dc:title>" % _esc(meta.title))
    if meta.subject:
        lines.append("  <dc:subject>%s</dc:subject>" % _esc(meta.subject))
    if meta.author:
        lines.append("  <meta:initial-creator>%s</meta:initial-creator>" % _esc(meta.author))
        lines.append("  <dc:creator>%s</dc:creator>" % _esc(meta.author))
    if meta.disclaimer:
        lines.append("  <dc:description>%s</dc:description>" % _esc(meta.disclaimer))
    for kw in meta.keywords:
        lines.append("  <meta:keyword>%s</meta:keyword>" % _esc(kw))
    if meta.year:
        lines.append('  <meta:user-defined meta:name="Jahr">%s</meta:user-defined>' % _esc(meta.year))
    if meta.website:
        lines.append('  <meta:user-defined meta:name="Webseite">%s</meta:user-defined>' % _esc(meta.website))
    return "".join(line + "\n" for line in lines)


def build_merged_odt(
    sections: List[Section],
    chapters: "Dict[int, List[Section]]",
    out_path: str,
    *,
    complete: bool,
    with_toc: bool,
    with_title_page: bool,
    chapter_headings: bool,
    title: str,
    subtitle: Optional[str],
    keep_comments: bool,
    meta: Meta,
    chapter_titles: "Dict[int, str]",
    entry_titles: "Dict[int, Tuple[str, str]]",
    entry_level: int,
    cover: Optional[str] = None,
) -> None:
    parsed = [parse_section(s.path, i, keep_comments) for i, s in enumerate(sections)]
    index_of = {id(s): i for i, s in enumerate(sections)}

    content_ns = parsed[0].content_ns
    styles_ns = parsed[0].styles_ns

    # ---- cover image (read before building body) --------------------------
    cover_page_xml = ""
    cover_data: Optional[Tuple[bytes, str, str]] = None   # (bytes, inner_path, mime)
    if cover:
        ext = os.path.splitext(cover)[1].lower()
        mime = _COVER_MIME.get(ext)
        if mime is None:
            raise SystemExit("Unsupported cover image format %r; use JPG or PNG." % ext)
        with open(cover, "rb") as _fh:
            img_bytes = _fh.read()
        inner_path = "Pictures/cover" + ext
        # Extract content-area dimensions from the first section's page layout.
        _dim = lambda attr, default: (
            float(m.group(1)) if (m := re.search(r'%s="([\d.]+)cm"' % attr, parsed[0].styles_auto)) else default
        )
        pw = _dim("fo:page-width", 21.0)
        ph = _dim("fo:page-height", 29.7)
        cw = pw - _dim("fo:margin-left", 2.0) - _dim("fo:margin-right", 2.0)
        ch = ph - _dim("fo:margin-top", 2.0) - _dim("fo:margin-bottom", 2.0)
        cover_page_xml = _cover_page(inner_path, cw, ch)
        cover_data = (img_bytes, inner_path, mime)

    # ---- styles.xml -------------------------------------------------------
    styles_fonts = _union_fonts(*(p.styles_fonts for p in parsed))
    named = _union_named_styles(parsed[0].styles_named, *(p.styles_named for p in parsed[1:]))
    styles_auto = parsed[0].styles_auto      # page layouts from the first file
    master = parsed[0].master_styles         # master pages from the first file

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-styles %soffice:version="1.3">'
        "<office:font-face-decls>%s</office:font-face-decls>"
        "<office:styles>%s</office:styles>"
        "<office:automatic-styles>%s</office:automatic-styles>"
        "<office:master-styles>%s</office:master-styles>"
        "</office:document-styles>"
        % (styles_ns, styles_fonts, named, styles_auto, master)
    )

    # ---- content.xml ------------------------------------------------------
    content_fonts = _union_fonts(*(p.fonts for p in parsed))
    all_auto = GENERATED_AUTO_STYLES + "".join(p.auto_styles for p in parsed)

    seq_decls = (
        "<text:sequence-decls>"
        '<text:sequence-decl text:display-outline-level="0" text:name="Illustration"/>'
        '<text:sequence-decl text:display-outline-level="0" text:name="Table"/>'
        '<text:sequence-decl text:display-outline-level="0" text:name="Text"/>'
        '<text:sequence-decl text:display-outline-level="0" text:name="Drawing"/>'
        '<text:sequence-decl text:display-outline-level="0" text:name="Figure"/>'
        "</text:sequence-decls>"
    )

    def emit(sec: Section, need_sep: bool) -> List[str]:
        """One section, prefixed by its journal-entry title and/or a separator."""
        parts: List[str] = []
        is_entry_start = (
            sec.kind == "Journal"
            and sec.entry_first
            and sec.entry_index in entry_titles
        )
        if need_sep and not is_entry_start:
            # The entry title heading is itself a separator; only add a tilde
            # between plain sections / continuation parts.
            parts.append(_tilde())
        if is_entry_start:
            etitle, ewhen = entry_titles[sec.entry_index]
            parts.append(_journal_entry_heading(etitle, ewhen, entry_level))
        parts.append(parsed[index_of[id(sec)]].body)
        return parts

    body_parts: List[str] = [seq_decls]
    if cover_page_xml:
        body_parts.append(cover_page_xml)
    if complete and with_title_page:
        body_parts.append(_title_page(title, subtitle))
        body_parts.append(_page_break())
    if complete and with_toc and chapter_headings:
        body_parts.append(_toc(chapters, chapter_titles))
        body_parts.append(_page_break())

    if chapter_headings:
        # Chapter headings flow continuously; no page break before chapters.
        for chapter, secs in chapters.items():
            body_parts.append(_chapter_heading(chapter, chapter_titles.get(chapter)))
            for j, sec in enumerate(secs):
                body_parts.extend(emit(sec, need_sep=j > 0))
    else:
        # Continuous sequence (e.g. the diary); entry titles + tilde separators.
        for j, sec in enumerate(sections):
            body_parts.extend(emit(sec, need_sep=j > 0))

    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content %soffice:version="1.3">'
        "<office:scripts/>"
        "<office:font-face-decls>%s</office:font-face-decls>"
        "<office:automatic-styles>%s</office:automatic-styles>"
        '<office:body><office:text text:use-soft-page-breaks="true">%s'
        "</office:text></office:body>"
        "</office:document-content>"
        % (content_ns, content_fonts, all_auto, "".join(body_parts))
    )

    _write_odt(out_path, content_xml, styles_xml, meta, cover_data)


def _write_odt(
    out_path: str,
    content_xml: str,
    styles_xml: str,
    meta: Meta,
    cover_data: Optional[Tuple[bytes, str, str]] = None,
) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    # Build manifest dynamically so the cover image entry is included when needed.
    mf_entries = [
        ' <manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.text"/>',
        ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>',
        ' <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>',
        ' <manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>',
    ]
    if cover_data:
        _, inner_path, mime = cover_data
        mf_entries.append(
            ' <manifest:file-entry manifest:full-path="%s" manifest:media-type="%s"/>' % (inner_path, mime)
        )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
        ' manifest:version="1.3">\n'
        + "\n".join(mf_entries) + "\n"
        + "</manifest:manifest>\n"
    )
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype must be the first entry and stored uncompressed.
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, "application/vnd.oasis.opendocument.text")
        z.writestr("META-INF/manifest.xml", manifest)
        z.writestr("meta.xml", META % {"fields": _build_meta_xml(meta)})
        z.writestr("content.xml", content_xml)
        z.writestr("styles.xml", styles_xml)
        if cover_data:
            img_bytes, inner_path, _ = cover_data
            z.writestr(inner_path, img_bytes)


# --------------------------------------------------------------------------- #
# Markdown (plain-text) export
# --------------------------------------------------------------------------- #

_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'", "&quot;": '"'}


def _unescape(text: str) -> str:
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)
    return text


def _para_to_text(inner: str) -> str:
    """Turn the inner XML of a paragraph into plain text.

    Line breaks and tab/space elements are preserved (a poem keeps its short
    lines); every other markup is dropped.
    """
    inner = re.sub(r"<text:line-break\b[^>]*?/>", "\n", inner)
    inner = re.sub(r"<text:tab\b[^>]*?/>", " ", inner)

    def _spaces(m: "re.Match") -> str:
        c = m.group(1)
        return " " * (int(c) if c else 1)

    inner = re.sub(r'<text:s\b(?:[^>]*?text:c="(\d+)")?[^>]*?/>', _spaces, inner)
    inner = re.sub(r"<[^>]+>", "", inner)          # strip remaining tags
    inner = _unescape(inner)
    # Trim trailing spaces on each line but keep intentional line breaks.
    return "\n".join(line.rstrip() for line in inner.split("\n")).strip()


def _section_paragraphs(path: str, keep_comments: bool) -> List[str]:
    """Return the non-empty paragraphs of a section as plain-text blocks."""
    with zipfile.ZipFile(path) as z:
        content = z.read("content.xml").decode("utf-8")
    m = re.search(r"<office:text\b[^>]*>(.*)</office:text>", content, re.S)
    body = m.group(1) if m else ""
    if not keep_comments:
        body = re.sub(r"<office:annotation\b.*?</office:annotation>", "", body, flags=re.S)
        body = re.sub(r"<office:annotation-end\b[^>]*?/>", "", body)

    paras: List[str] = []
    for m in re.finditer(r"<text:(p|h)\b[^>]*?(?:/>|>(.*?)</text:\1>)", body, re.S):
        inner = m.group(2) or ""
        text = _para_to_text(inner)
        if text:
            paras.append(text)
    return paras


def _md_block(text: str) -> str:
    """Render a paragraph for Markdown, keeping poem line breaks as hard breaks."""
    if "\n" in text:
        return "  \n".join(line for line in text.split("\n"))
    return text


def build_markdown(
    sections: List[Section],
    chapters: "Dict[int, List[Section]]",
    *,
    complete: bool,
    with_title_page: bool,
    chapter_headings: bool,
    title: str,
    subtitle: Optional[str],
    keep_comments: bool,
    chapter_titles: "Dict[int, str]",
    entry_titles: "Dict[int, Tuple[str, str]]",
    entry_level: int = 2,
) -> str:
    out: List[str] = []
    top_title = complete and with_title_page
    if top_title:
        out.append("# " + title)
        if subtitle:
            out.append("*" + subtitle + "*")
        out.append("")
    chapter_prefix = "## " if top_title else "# "
    # Journal entries sit one level below the chapter (book) or directly below
    # the title (journal-only edition); mirror the ODT outline level.
    entry_hashes = ("#" * (entry_level + 1)) if top_title else ("#" * entry_level)
    entry_prefix = entry_hashes + " "

    def emit_section(sec: Section, need_sep: bool) -> None:
        is_entry_start = (
            sec.kind == "Journal"
            and sec.entry_first
            and sec.entry_index in entry_titles
        )
        if need_sep and not is_entry_start:
            out.append("~")
            out.append("")
        if is_entry_start:
            etitle, ewhen = entry_titles[sec.entry_index]
            out.append(entry_prefix + etitle)
            if ewhen:
                out.append("*" + ewhen + "*")
            out.append("")
        for para in _section_paragraphs(sec.path, keep_comments):
            out.append(_md_block(para))
            out.append("")

    if chapter_headings:
        for chapter, secs in chapters.items():
            ctitle = chapter_titles.get(chapter)
            label = "%s \u2013 %s" % (roman(chapter), ctitle) if ctitle else roman(chapter)
            out.append(chapter_prefix + label)
            out.append("")
            for j, sec in enumerate(secs):
                emit_section(sec, need_sep=j > 0)
    else:
        for j, sec in enumerate(sections):
            emit_section(sec, need_sep=j > 0)

    text = "\n".join(out).rstrip() + "\n"
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# --------------------------------------------------------------------------- #
# LibreOffice conversion
# --------------------------------------------------------------------------- #


def find_soffice(explicit: Optional[str]) -> str:
    candidates = [explicit] if explicit else SOFFICE_CANDIDATES
    for c in candidates:
        if not c:
            continue
        if os.path.isabs(c) and os.path.exists(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    raise SystemExit(
        "Could not find LibreOffice (soffice). Install LibreOffice or pass "
        "--soffice /path/to/soffice."
    )


def convert(soffice: str, odt_path: str, fmt: str, out_dir: str, profile: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "-env:UserInstallation=file://%s" % profile,
        "--convert-to",
        EXPORT_FILTERS[fmt],
        "--outdir",
        out_dir,
        odt_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = os.path.splitext(os.path.basename(odt_path))[0]
    produced = os.path.join(out_dir, base + "." + fmt)
    if res.returncode != 0 or not os.path.exists(produced):
        sys.stderr.write(res.stdout.decode("utf-8", "replace"))
        raise SystemExit("LibreOffice failed to convert %s to %s." % (odt_path, fmt))
    return produced


def _fix_epub_cover(epub_path: str) -> None:
    """Patch the EPUB OPF so the first image is declared as the cover image.

    LibreOffice exports the image but does not set ``properties="cover-image"``
    or the EPUB 2 ``<meta name="cover">`` entry.  This post-process step adds
    both so that every EPUB reader recognises the cover correctly.
    """
    import io as _io

    with zipfile.ZipFile(epub_path, "r") as zin:
        names = zin.namelist()
        opf_name = next((n for n in names if n.endswith(".opf")), None)
        if opf_name is None:
            return
        files = {n: zin.read(n) for n in names}

    opf = files[opf_name].decode("utf-8")

    # Find the first image item in the manifest.
    m = re.search(
        r'(<item\b([^>]*)\bmedia-type="image/[^"]*"([^>]*)/>)',
        opf,
    )
    if m is None:
        return   # no image at all – nothing to do

    full_tag = m.group(1)
    attrs = m.group(2) + m.group(3)
    id_m = re.search(r'\bid="([^"]+)"', attrs)
    item_id = id_m.group(1) if id_m else "cover-image"

    # Add properties="cover-image" if not already present.
    if "cover-image" not in full_tag:
        new_tag = full_tag.replace("/>", ' properties="cover-image"/>', 1)
        # Rename id to "cover-image" for maximum compatibility.
        new_tag = re.sub(r'\bid="[^"]+"', 'id="cover-image"', new_tag, count=1)
        opf = opf.replace(full_tag, new_tag, 1)
        # Update any spine/guide reference to the old id.
        if item_id != "cover-image":
            opf = opf.replace('idref="%s"' % item_id, 'idref="cover-image"')
        item_id = "cover-image"

    # Add EPUB 2 <meta name="cover"> inside <metadata> if missing.
    cover_meta = '<meta name="cover" content="%s"/>' % item_id
    if cover_meta not in opf and 'name="cover"' not in opf:
        opf = opf.replace("</metadata>", "  %s\n  </metadata>" % cover_meta, 1)

    files[opf_name] = opf.encode("utf-8")

    # Re-write the EPUB in place (mimetype entry must remain first+stored).
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zout.writestr(zi, files.pop("mimetype", b"application/epub+zip"))
        for name, data in files.items():
            zout.writestr(name, data)
    with open(epub_path, "wb") as fh:
        fh.write(buf.getvalue())





def slugify(name: str) -> str:
    table = {
        "\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue",
        "\u00c4": "Ae", "\u00d6": "Oe", "\u00dc": "Ue", "\u00df": "ss",
    }
    name = "".join(table.get(ch, ch) for ch in name)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")


TAGEBUCH_TITLE = "Wilhelms Tagebuch"


@dataclass
class Edition:
    """One output document to be produced (the book, a chapter, the diary …)."""

    base: str                                # output file name (without extension)
    title: str
    subtitle: Optional[str]
    sections: List[Section]
    chapters: "Dict[int, List[Section]]"
    chapter_headings: bool
    complete: bool
    with_title_page: bool
    with_toc: bool
    meta: Meta
    chapter_titles: "Dict[int, str]"
    entry_titles: "Dict[int, Tuple[str, str]]"
    entry_level: int
    cover: Optional[str] = None


def build_editions(
    args,
    sections: List[Section],
    chapters: "Dict[int, List[Section]]",
    config: BookConfig,
) -> List[Edition]:
    """Translate the command-line options into the list of editions to build."""
    editions: List[Edition] = []

    if args.tagebuch:
        journals = [s for s in sections if s.kind == "Journal"]
        if not journals:
            raise SystemExit("No Journal sections found for the Tagebuch edition.")
        jmeta = config.journal
        jtitle = args.title or jmeta.title or TAGEBUCH_TITLE
        jsub = args.subtitle if args.subtitle is not None else jmeta.subject
        if args.mode == "complete":
            editions.append(
                Edition(
                    base="Tagebuch",
                    title=jtitle,
                    subtitle=jsub,
                    sections=journals,
                    chapters=group_by_chapter(journals),
                    chapter_headings=False,
                    complete=True,
                    with_title_page=not args.no_title_page,
                    with_toc=False,
                    meta=jmeta,
                    chapter_titles={},
                    entry_titles=config.entry_titles,
                    entry_level=1,
                    cover=getattr(args, "cover", None),
                )
            )
        else:
            for chapter, secs in group_by_chapter(journals).items():
                editions.append(
                    Edition(
                        base="Tagebuch_Kapitel_%02d" % chapter,
                        title="%s \u2013 Kapitel %s" % (jtitle, roman(chapter)),
                        subtitle=None,
                        sections=secs,
                        chapters={chapter: secs},
                        chapter_headings=False,
                        complete=False,
                        with_title_page=False,
                        with_toc=False,
                        meta=jmeta,
                        chapter_titles={},
                        entry_titles=config.entry_titles,
                        entry_level=1,
                    )
                )
        return editions

    bmeta = config.book
    btitle = args.title or bmeta.title or "Edelwei\u00df"
    bsub = args.subtitle if args.subtitle is not None else bmeta.subject
    if args.mode == "complete":
        editions.append(
            Edition(
                base=slugify(btitle) or "Book",
                title=btitle,
                subtitle=bsub,
                sections=sections,
                chapters=chapters,
                chapter_headings=True,
                complete=True,
                with_title_page=not args.no_title_page,
                with_toc=args.index,
                meta=bmeta,
                chapter_titles=config.chapter_titles,
                entry_titles=config.entry_titles,
                entry_level=2,
                cover=getattr(args, "cover", None),
            )
        )
    else:
        for chapter, secs in chapters.items():
            editions.append(
                Edition(
                    base="Kapitel_%02d" % chapter,
                    title="%s \u2013 Kapitel %s" % (btitle, roman(chapter)),
                    subtitle=None,
                    sections=secs,
                    chapters={chapter: secs},
                    chapter_headings=True,
                    complete=False,
                    with_title_page=False,
                    with_toc=False,
                    meta=bmeta,
                    chapter_titles=config.chapter_titles,
                    entry_titles=config.entry_titles,
                    entry_level=2,
                )
            )
    return editions



def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="buchbinder",
        description="Merge the Edelweiß ODT sections into a book and export it.",
    )
    p.add_argument(
        "--mode",
        choices=["complete", "chapters"],
        default="complete",
        help="Build one complete book (default) or one file per chapter.",
    )
    p.add_argument(
        "--format",
        "-f",
        dest="formats",
        action="append",
        choices=["pdf", "epub", "html", "odt", "md"],
        help="Output format(s); repeat for several. Default: pdf.",
    )
    p.add_argument(
        "--index",
        action="store_true",
        help="Add a table of contents (only meaningful for --mode complete).",
    )
    p.add_argument(
        "--tagebuch",
        action="store_true",
        help="Build the journal-only edition ('Wilhelms Tagebuch') instead of the full book.",
    )
    p.add_argument(
        "--chapter",
        action="append",
        type=int,
        help="Restrict to the given chapter number(s); repeat for several.",
    )
    p.add_argument("--title", default=None, help="Book title override (otherwise taken from the ini).")
    p.add_argument("--subtitle", default=None, help="Subtitle override (otherwise the ini 'subject').")
    p.add_argument("--no-title-page", action="store_true", help="Omit the title page (complete mode).")
    p.add_argument("--keep-comments", action="store_true", help="Keep author annotations/comments.")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="Folder with the section .odt files.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Path to buchbinder.ini (titles/metadata).")
    p.add_argument("--cover", default=None, help="Path to a JPG or PNG cover image (embedded as the first page).")
    p.add_argument("--publish", default=DEFAULT_PUBLISH, help="Output root (data/publish).")
    p.add_argument("--soffice", default=None, help="Path to the LibreOffice 'soffice' binary.")
    p.add_argument("--keep-build", action="store_true", help="Keep the intermediate merged ODT build folder.")
    args = p.parse_args(argv)

    formats = args.formats or ["pdf"]

    config = load_config(args.config)

    all_sections = discover_sections(args.source)
    if not all_sections:
        raise SystemExit("No section files (NNNN_Type_Title.odt) found in %s" % args.source)

    # Number the journal entries from the *complete* book so the entry titles
    # stay correct even when only some chapters are built.
    assign_journal_entries(all_sections)

    if args.chapter:
        wanted = set(args.chapter)
        all_sections = [s for s in all_sections if s.chapter in wanted]
        if not all_sections:
            raise SystemExit("No sections found for chapter(s) %s" % sorted(wanted))

    chapters = group_by_chapter(all_sections)
    editions = build_editions(args, all_sections, chapters, config)

    needs_soffice = any(f not in ("odt", "md") for f in formats)
    soffice = find_soffice(args.soffice) if needs_soffice else None

    build_dir = os.path.join(args.publish, ".build")
    os.makedirs(build_dir, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="buchbinder_lo_")

    try:
        for ed in editions:
            # The merged ODT is needed for every format except Markdown.
            odt_path = None
            if any(f != "md" for f in formats):
                odt_path = os.path.join(build_dir, ed.base + ".odt")
                build_merged_odt(
                    ed.sections,
                    ed.chapters,
                    odt_path,
                    complete=ed.complete,
                    with_toc=ed.with_toc,
                    with_title_page=ed.with_title_page,
                    chapter_headings=ed.chapter_headings,
                    title=ed.title,
                    subtitle=ed.subtitle,
                    keep_comments=args.keep_comments,
                    meta=ed.meta,
                    chapter_titles=ed.chapter_titles,
                    entry_titles=ed.entry_titles,
                    entry_level=ed.entry_level,
                    cover=ed.cover,
                )

            for fmt in formats:
                out_dir = os.path.join(args.publish, fmt)
                os.makedirs(out_dir, exist_ok=True)
                if fmt == "md":
                    md = build_markdown(
                        ed.sections,
                        ed.chapters,
                        complete=ed.complete,
                        with_title_page=ed.with_title_page,
                        chapter_headings=ed.chapter_headings,
                        title=ed.title,
                        subtitle=ed.subtitle,
                        keep_comments=args.keep_comments,
                        chapter_titles=ed.chapter_titles,
                        entry_titles=ed.entry_titles,
                        entry_level=ed.entry_level,
                    )
                    dest = os.path.join(out_dir, ed.base + ".md")
                    with open(dest, "w", encoding="utf-8") as fh:
                        fh.write(md)
                    print("wrote", os.path.relpath(dest, PROJECT_ROOT))
                elif fmt == "odt":
                    dest = os.path.join(out_dir, ed.base + ".odt")
                    shutil.copyfile(odt_path, dest)
                    print("wrote", os.path.relpath(dest, PROJECT_ROOT))
                else:
                    produced = convert(soffice, odt_path, fmt, out_dir, profile)
                    if fmt == "epub" and ed.cover:
                        _fix_epub_cover(produced)
                    print("wrote", os.path.relpath(produced, PROJECT_ROOT))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if not args.keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
