# Buchbinder – usage & behaviour

`tools/buchbinder.py` merges the *Edelweiß* section files into a book and
exports it. This document describes how to run it and exactly how it behaves.

For the manuscript/format conventions, see the [project README](../README.md).

---

## 1. Quick start

From the project root:

```bash
# Complete book as PDF (default)
python3 tools/buchbinder.py

# Complete book with a table of contents, as PDF and EPUB
python3 tools/buchbinder.py --index -f pdf -f epub

# One file per chapter, as PDF
python3 tools/buchbinder.py --mode chapters -f pdf

# Just chapters 1 and 2, all formats (incl. Markdown)
python3 tools/buchbinder.py --chapter 1 --chapter 2 -f pdf -f epub -f html -f odt -f md

# The journal-only edition (“Wilhelms Tagebuch”) as EPUB
python3 tools/buchbinder.py --tagebuch -f epub

# journal in all formats
python3 tools/buchbinder.py --tagebuch -f pdf -f epub -f html -f odt -f md

# book with table of contents in all formats
python3 tools/buchbinder.py --index --cover data/images/Cover.png -f pdf -f epub -f html -f odt -f md
```

No installation step is required — it is a single Python file using only the
standard library. LibreOffice must be installed for every format except `odt`.

---

## 2. Command-line options

| Option              | Values / default                | Meaning                                                                 |
|---------------------|---------------------------------|-------------------------------------------------------------------------|
| `--mode`            | `complete` (default), `chapters`| Build one whole book, or one document per chapter.                      |
| `-f`, `--format`    | `pdf` (default), `epub`, `html`, `odt`, `md` | Output format. Repeat the flag to produce several formats at once. |
| `--index`           | off                             | Add a clickable “Inhaltsverzeichnis” (only used in `complete` mode).     |
| `--tagebuch`        | off                             | Build the journal-only edition (*Wilhelms Tagebuch*) instead of the full book. |
| `--chapter N`       | all chapters                    | Restrict to chapter `N`. Repeat for several chapters.                   |
| `--title`           | from the ini                    | Override the book/journal title (otherwise taken from `buchbinder.ini`; also the file name). |
| `--subtitle`        | from the ini `subject`          | Override the subtitle shown on the title page.                          |
| `--no-title-page`   | off                             | Omit the title page in `complete` mode.                                 |
| `--keep-comments`   | off                             | Keep author annotations/comments (otherwise they are stripped).         |
| `--source`          | `data/edelweiss`                | Folder containing the `NNNN_Type_Title.odt` section files.              |
| `--config`          | `…/Buchbinder/buchbinder.ini`   | INI file with titles, chapter/entry names and document metadata.        |
| `--cover`           | none                            | Path to a JPG or PNG cover image (embedded as the first page; used as the EPUB cover). Only applied in `complete` mode. |
| `--publish`         | `data/publish`                  | Output root folder.                                                     |
| `--soffice`         | auto-detected                   | Path to the LibreOffice `soffice` binary.                               |
| `--keep-build`      | off                             | Keep the intermediate merged ODT in `data/publish/.build`.              |

---

## 3. Behaviour in detail

### Section discovery and ordering

- Only files matching `NNNN_Type_Title.odt` (four digits, `Story`/`Journal`,
  CamelCase title) in the source folder are used. Everything else is ignored.
- Files are ordered by their four-digit prefix; chapters are the first two
  digits.
- `--chapter` filters which chapters are included; the rest of the pipeline is
  unchanged.

### Merging

- Each chapter starts with a generated **chapter heading**: the chapter number
  as a **Roman numeral** (e.g. *I*, *II*, *XVII*) on the first line and the
  chapter title from the ini `[book_chapters]` section on a smaller second
  line. Chapters flow continuously — there is **no** forced page break between
  chapters. (Source heading styles carry a `master-page-name`, which ODF turns
  into a page break; Buchbinder strips that attribute while merging.) Only the
  front matter (title page and table of contents) is followed by a page break,
  so the body starts on a fresh page.
- **Journal entries** get a small two-line heading taken from the ini
  `[journal_entries]` section: the entry name, then the place/date on a smaller
  right-aligned line (styled after *Wilhelms Tagebuch.odm*). A journal that was
  written in several parts (same base title with a trailing digit, e.g.
  `Judastee` + `Judastee2` + `Judastee3`) is merged into a **single** entry and
  only its first part receives the heading.
- Sections inside a chapter are separated by a centred **`~`** paragraph. There
  is no separator between chapters, and none directly before a journal-entry
  heading.
- All paragraph styles, character styles, fonts, the page layout and master
  pages from the source files are merged in, so the serif **Story** text, the
  italic **Journal** text, **poems** and **foreign-language** passages keep
  their original formatting.
- To avoid clashes, each section’s *automatic* style names are renamed with a
  per-section prefix (`bx0_`, `bx1_`, …) before merging. Named template styles
  (e.g. *Textkörper [Story]*) are shared and merged by name.
- Author **annotations/comments** are removed by default. Use `--keep-comments`
  to retain them.

### Title page

- In `complete` mode a simple centred title page (book title, optional subtitle)
  is added unless `--no-title-page` is given.
- `chapters` mode never adds a title page.

### Cover image (`--cover`)

- Pass a JPG or PNG file with `--cover path/to/cover.jpg`. The image is embedded
  as the very first page of the document (before the title page) and is also
  used as the EPUB cover image (the OPF is patched so every reader recognises
  it, both EPUB 2 `<meta name="cover">` and EPUB 3 `properties="cover-image"`).
- The image is scaled to fill the **content area** of the page (page size minus
  margins). With the default A4 layout (21 × 29.7 cm, 2 cm margins) that area
  is **17 × 25.7 cm**, i.e. an aspect ratio of roughly **1 : 1.51**.
- Recommended pixel dimensions (so the cover stays crisp in PDF as well as on
  high-DPI e-reader screens):

  | Use                    | Pixel size            | Approx. DPI |
  |------------------------|-----------------------|-------------|
  | Minimum (e-reader)     | **1240 × 1880 px**     | ~185        |
  | Recommended (print)    | **2480 × 3757 px**     | ~370 (A4 @ 300 DPI) |

  JPG (~80 % quality) is usually enough; use PNG only if you need lossless
  graphics. Any other aspect ratio is accepted but will be stretched to fit
  the content area, so prefer 1 : 1.51.
- The cover is only applied to `complete`-mode editions (the full book and the
  full *Wilhelms Tagebuch*); per-chapter exports ignore `--cover`.

### Table of contents (`--index`)

- Only meaningful in `complete` mode.
- The contents are generated as an **“Inhaltsverzeichnis”** heading followed by
  one entry per chapter (labelled with the Roman numeral and chapter title).
  Each entry links to a bookmark on the corresponding chapter heading, so it is
  clickable in PDF, EPUB and HTML.
- The entries deliberately do **not** print page numbers. (LibreOffice only
  fills in page numbers for a live `Table of Contents` field when fields are
  updated interactively, which headless conversion does not do. A pre-rendered,
  link-based contents list is reliable in every format instead.)
- In EPUB, readers additionally get the native EPUB navigation
  (`toc.ncx` / `toc.xhtml`) that LibreOffice builds from the chapter headings,
  regardless of `--index`.

### Conversion

- The merged ODT is first written to `data/publish/.build/` and then converted
  with `soffice --headless --convert-to …`.
- A throwaway LibreOffice user profile is created per run, so conversions work
  even if LibreOffice is otherwise open.
- The build folder is deleted at the end unless `--keep-build` is set. For the
  `odt` format the merged document is copied to `data/publish/odt/`.
- **Markdown** (`-f md`) is produced directly in Python — LibreOffice has no
  usable Markdown export filter — so it needs no `soffice`. Paragraphs become
  plain text, poem line breaks are kept as hard breaks (two trailing spaces),
  the title page becomes a top-level `# heading`, chapter headings become
  `## I – Title` (Roman numeral + chapter title), journal-entry headings become
  a heading one level deeper with the place/date in italics, and section
  separators are written as `~`.

### Document metadata

- Title, subtitle/subject, author, description, keywords, year and website are
  read from `buchbinder.ini` (`--config`) and written into the document
  metadata (`meta.xml` / EPUB OPF).
- The `[journal]` edition inherits any value it does not set from `[book]`, so
  *Wilhelms Tagebuch* keeps the same author, keywords, etc. while overriding the
  title and subject.

### Journal-only edition (`--tagebuch`)

- Builds *Wilhelms Tagebuch*: only the **Journal** sections, in reading order.
- There are no chapter headings and no table of contents; instead each diary
  entry gets its two-line entry heading (name + place/date) as the top heading
  level, with `~` separators between continuation parts.
- In `complete` mode it is written as `Tagebuch.<ext>` (with a title page unless
  `--no-title-page`). In `chapters` mode you get `Tagebuch_Kapitel_NN.<ext>`.
- The title and subtitle come from the ini `[journal]` section (default
  **“Wilhelms Tagebuch”** / **“Für immer unverloren”**).

### Output names

| Mode / edition          | File names                                            |
|-------------------------|-------------------------------------------------------|
| `complete`              | `<title-slug>.<ext>` (e.g. `Edelweiss.pdf`)           |
| `chapters`              | `Kapitel_01.<ext>`, `Kapitel_02.<ext>`, …             |
| `--tagebuch` complete   | `Tagebuch.<ext>`                                      |
| `--tagebuch` chapters   | `Tagebuch_Kapitel_01.<ext>`, …                        |

The title slug transliterates German characters (`ß→ss`, `ä→ae`, …) and replaces
any remaining non-filename characters with `_`.

---

## 4. Examples

```bash
# Full book, PDF + EPUB + HTML, with a table of contents
python3 tools/buchbinder.py --index -f pdf -f epub -f html

# Full book with a cover image (used on the first page and as the EPUB cover)
python3 tools/buchbinder.py --index --cover data/edelweiss/cover.jpg \
    -f pdf -f epub

# Per-chapter EPUBs for an e-reader
python3 tools/buchbinder.py --mode chapters -f epub

# The journal-only edition in every format
python3 tools/buchbinder.py --tagebuch -f pdf -f epub -f html -f md

# A proof of chapters 11–13 only, keeping comments and the merged ODT
python3 tools/buchbinder.py --chapter 11 --chapter 12 --chapter 13 \
    --keep-comments --keep-build -f pdf -f odt

# Custom title and explicit LibreOffice path
python3 tools/buchbinder.py --title "Edelweiß" --subtitle "Ein Roman" \
    --soffice /Applications/LibreOffice.app/Contents/MacOS/soffice
```

---

## 5. Troubleshooting

- **“Could not find LibreOffice (soffice).”** Install LibreOffice or pass
  `--soffice /path/to/soffice`. Only the `odt` format works without it.
- **“No section files … found.”** Check `--source`; the files must match
  `NNNN_Type_Title.odt`.
- **A conversion fails.** Re-run with `--keep-build` and open the merged
  document in `data/publish/.build/` in LibreOffice to inspect it. Make sure no
  other headless `soffice` process is stuck (`pkill -f soffice`).
- **Index page numbers.** By design the contents list has no page numbers; open
  `data/publish/odt/…odt` in LibreOffice and use *Tools ▸ Update ▸ Update All*
  if you want a classic page-numbered TOC field instead.
- **The EPUB opens in a text/XML editor.** The file is valid; this is a macOS
  file-association setting. Right-click the `.epub` ▸ *Open With* ▸ **Books**,
  or *Get Info* ▸ *Open with* ▸ choose Books ▸ *Change All…* to set the default.
