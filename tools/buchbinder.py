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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
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

    @property
    def display_title(self) -> str:
        # Turn "ListeDerWinter" into "Liste Der Winter".
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", self.title)
        spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", spaced)
        return spaced


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
fo:text-align="center" style:justify-single-word="false"/></style:style>\
<style:style style:name="BB_PageBreak" style:family="paragraph" \
style:parent-style-name="Standard"><style:paragraph-properties \
fo:break-after="page"/></style:style>\
<style:style style:name="BB_Tilde" style:family="paragraph" \
style:parent-style-name="%(sj)s"><style:paragraph-properties \
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
""" % {"sj": STYLE_STORY_JOURNAL}


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _chapter_heading(chapter: int) -> str:
    return (
        '<text:h text:style-name="BB_Chapter" text:outline-level="1">'
        '<text:bookmark text:name="chapter_%d"/>Kapitel %d</text:h>'
        % (chapter, chapter)
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


def _toc(chapters: "Dict[int, List[Section]]") -> str:
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
        out.append(
            '<text:p text:style-name="Contents_20_1">'
            '<text:a xlink:type="simple" xlink:href="#chapter_%d" '
            'text:style-name="Internet_20_link">Kapitel %d</text:a></text:p>'
            % (chapter, chapter)
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
  <dc:title>%(title)s</dc:title>
  <meta:generator>Buchbinder</meta:generator>
 </office:meta>
</office:document-meta>
"""


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
) -> None:
    parsed = [parse_section(s.path, i, keep_comments) for i, s in enumerate(sections)]
    index_of = {id(s): i for i, s in enumerate(sections)}

    content_ns = parsed[0].content_ns
    styles_ns = parsed[0].styles_ns

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

    body_parts: List[str] = [seq_decls]
    if complete and with_title_page:
        body_parts.append(_title_page(title, subtitle))
        body_parts.append(_page_break())
    if complete and with_toc and chapter_headings:
        body_parts.append(_toc(chapters))
        body_parts.append(_page_break())

    if chapter_headings:
        # Chapter headings flow continuously; no page break before chapters.
        for chapter, secs in chapters.items():
            body_parts.append(_chapter_heading(chapter))
            for j, sec in enumerate(secs):
                body_parts.append(parsed[index_of[id(sec)]].body)
                if j < len(secs) - 1:
                    body_parts.append(_tilde())
    else:
        # Continuous sequence (e.g. the diary); only tilde separators.
        for j, sec in enumerate(sections):
            body_parts.append(parsed[index_of[id(sec)]].body)
            if j < len(sections) - 1:
                body_parts.append(_tilde())

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

    _write_odt(out_path, content_xml, styles_xml, title)


def _write_odt(out_path: str, content_xml: str, styles_xml: str, title: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype must be the first entry and stored uncompressed.
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, "application/vnd.oasis.opendocument.text")
        z.writestr("META-INF/manifest.xml", MANIFEST)
        z.writestr("meta.xml", META % {"title": _esc(title)})
        z.writestr("content.xml", content_xml)
        z.writestr("styles.xml", styles_xml)


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
) -> str:
    out: List[str] = []
    top_title = complete and with_title_page
    if top_title:
        out.append("# " + title)
        if subtitle:
            out.append("*" + subtitle + "*")
        out.append("")
    chapter_prefix = "## " if top_title else "# "

    def emit_section(sec: Section) -> None:
        for para in _section_paragraphs(sec.path, keep_comments):
            out.append(_md_block(para))
            out.append("")

    if chapter_headings:
        for chapter, secs in chapters.items():
            out.append(chapter_prefix + "Kapitel %d" % chapter)
            out.append("")
            for j, sec in enumerate(secs):
                emit_section(sec)
                if j < len(secs) - 1:
                    out.append("~")
                    out.append("")
    else:
        for j, sec in enumerate(sections):
            emit_section(sec)
            if j < len(sections) - 1:
                out.append("~")
                out.append("")

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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


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


def build_editions(args, sections: List[Section], chapters: "Dict[int, List[Section]]") -> List[Edition]:
    """Translate the command-line options into the list of editions to build."""
    editions: List[Edition] = []

    if args.tagebuch:
        journals = [s for s in sections if s.kind == "Journal"]
        if not journals:
            raise SystemExit("No Journal sections found for the Tagebuch edition.")
        if args.mode == "complete":
            editions.append(
                Edition(
                    base="Tagebuch",
                    title=TAGEBUCH_TITLE,
                    subtitle=args.subtitle,
                    sections=journals,
                    chapters=group_by_chapter(journals),
                    chapter_headings=False,
                    complete=True,
                    with_title_page=not args.no_title_page,
                    with_toc=False,
                )
            )
        else:
            for chapter, secs in group_by_chapter(journals).items():
                editions.append(
                    Edition(
                        base="Tagebuch_Kapitel_%02d" % chapter,
                        title="%s \u2013 Kapitel %d" % (TAGEBUCH_TITLE, chapter),
                        subtitle=None,
                        sections=secs,
                        chapters={chapter: secs},
                        chapter_headings=False,
                        complete=False,
                        with_title_page=False,
                        with_toc=False,
                    )
                )
        return editions

    if args.mode == "complete":
        editions.append(
            Edition(
                base=slugify(args.title) or "Book",
                title=args.title,
                subtitle=args.subtitle,
                sections=sections,
                chapters=chapters,
                chapter_headings=True,
                complete=True,
                with_title_page=not args.no_title_page,
                with_toc=args.index,
            )
        )
    else:
        for chapter, secs in chapters.items():
            editions.append(
                Edition(
                    base="Kapitel_%02d" % chapter,
                    title="%s \u2013 Kapitel %d" % (args.title, chapter),
                    subtitle=None,
                    sections=secs,
                    chapters={chapter: secs},
                    chapter_headings=True,
                    complete=False,
                    with_title_page=False,
                    with_toc=False,
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
    p.add_argument("--title", default="Edelwei\u00df", help="Book title (title page / metadata).")
    p.add_argument("--subtitle", default=None, help="Optional subtitle for the title page.")
    p.add_argument("--no-title-page", action="store_true", help="Omit the title page (complete mode).")
    p.add_argument("--keep-comments", action="store_true", help="Keep author annotations/comments.")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="Folder with the section .odt files.")
    p.add_argument("--publish", default=DEFAULT_PUBLISH, help="Output root (data/publish).")
    p.add_argument("--soffice", default=None, help="Path to the LibreOffice 'soffice' binary.")
    p.add_argument("--keep-build", action="store_true", help="Keep the intermediate merged ODT build folder.")
    args = p.parse_args(argv)

    formats = args.formats or ["pdf"]

    all_sections = discover_sections(args.source)
    if not all_sections:
        raise SystemExit("No section files (NNNN_Type_Title.odt) found in %s" % args.source)

    if args.chapter:
        wanted = set(args.chapter)
        all_sections = [s for s in all_sections if s.chapter in wanted]
        if not all_sections:
            raise SystemExit("No sections found for chapter(s) %s" % sorted(wanted))

    chapters = group_by_chapter(all_sections)
    editions = build_editions(args, all_sections, chapters)

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
                    print("wrote", os.path.relpath(produced, PROJECT_ROOT))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if not args.keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
