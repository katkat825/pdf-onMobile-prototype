# PDF → Mobile Ereader Prototype

## What this is

A proof-of-concept pipeline that takes a PDF — specifically the kind of "bad"
PDF you get from a print-layout sourcebook, RPG manual, or scanned document —
and converts it into an HTML file and an EPUB file that can be read comfortably
on a mobile device without horizontal scrolling or tiny text.

The input PDF (`input/sample.pdf`) is a Traveller RPG sourcebook. It was chosen
deliberately because it represents the hardest class of PDF to convert:

- Pages are full-page rasterized images, not selectable text
- Multi-column layouts (credits page, content pages)
- Complex typography with decorative headers, coloured text, and illustrations
- No usable font encoding in the text layer (where one exists at all)

This is the kind of PDF that commercial ebook stores reject. They require
publishers to submit properly formatted source files. When you buy a DnD book
or RPG sourcebook in a Kindle or Nook store, you are getting an EPUB that was
typeset in parallel from the same source — not a converted PDF. There is no
automated tool that does this conversion well at the time of writing, and
making it look polished is a product unto itself.

**This prototype was built to explore what is possible and to establish a
foundation for a more complete conversion tool if that project is ever pursued.**
It is intentionally parked here incomplete.

---

## What the current code does

The main script is `extract.py`. Run it with:

```
python extract.py
```

It produces:
- `output/book.html` — best for reading in a mobile browser; includes both
  the original page as an image and the reflowed text below it
- `output/book.epub` — for ereader apps; same structure
- `output/renders/` — JPEG renders of each page (the "original view" images)
- `output/images/` — any content images extracted directly from the PDF
  (relevant for PDFs that embed images as objects rather than baking them
  into a full-page scan)

### How it decides what to do with each page

**Step 1 — Try native text extraction.**
Some PDFs have a real text layer with font metadata. If the extracted text
passes quality checks (enough characters, not mostly garbled or containing
unmapped glyphs), the script uses it directly. This preserves bold, italic,
colour, and can detect heading levels from font size ratios.

**Step 2 — Fall back to OCR.**
If native extraction fails the quality checks — which it does for the sample
PDF on every page — the script renders the page to an image and runs an optical
character recognition engine on it. Most "bad" PDFs end up here.

The quality checks are worth understanding because they catch a non-obvious
failure mode: some PDFs have a text layer but the font encoding is broken.
Characters like "f", "fi", "fl" are stored as unmapped Private Use Area
glyphs, so the extracted text reads "o" instead of "of" and "rst" instead of
"first". The script detects this and routes those pages to OCR as well.

**Step 3 — Detect columns.**
Rather than naively splitting the page at the midpoint, the script builds a
density histogram of where words appear horizontally across the page. If there
is a clear low-density gap in the centre region, that gap is the column
boundary. This works whether the layout information comes from PDF block
geometry (for native-text pages) or from word bounding boxes in the OCR output
(for scanned pages). It correctly handles asymmetric columns where one column
has significantly more content than the other.

**Step 4 — Reconstruct reading order.**
Words are grouped into lines by vertical proximity, then lines are grouped into
paragraphs by gap size. For multi-column pages, the left column is fully read
before the right column.

**Step 5 — Detect headings.**
Because OCR gives us only plain text with no font metadata, section headings
are identified heuristically: a run of 1–6 consecutive ALL-CAPS words that is
followed by body text (or ends the block) is promoted to an `<h3>` element.
This works well for content pages. It over-fires on credits pages (AUTHOR,
EDITOR, ILLUSTRATIONS all become headings) but credits pages are not reading
content so this is acceptable.

**Step 6 — Save the page render.**
Every page is rendered to a JPEG regardless of which text path was taken.
The HTML and EPUB include this image above the reflowed text. For purely
scanned pages, the render is the only way to preserve illustrations, maps,
portraits, and decorative elements.

### The baseline

`extract-goodHtml.py` is kept as a reference. It was the starting point: it
OCRs every page without any column awareness or quality checks, and produces
HTML only (no EPUB). Do not modify it.

---

## What still needs work

These are the known gaps, roughly in order of how much they would matter to
a reader.

### 1. Tables

Tables in scanned pages are read by OCR row by row, which produces garbled
output. A proper fix requires detecting the grid structure of a table visually
(finding the lines that form the cells), segmenting the image into individual
cells, OCR-ing each cell, and emitting a proper HTML `<table>`. This is a
meaningful computer vision problem and was not attempted here.

For PDFs with a real text layer, table detection from block alignment is
somewhat more tractable but still non-trivial.

### 2. Page headers and footers

Running headers (chapter titles repeated at the top of each page) and footers
(page numbers) are included in the OCR output as if they were body content.
They should be detected and stripped. The approach: words appearing in the top
~5% or bottom ~5% of the page by vertical position that repeat across multiple
pages are almost certainly headers/footers. The groundwork is there — we
already have word-level bounding boxes from the OCR step — but the
cross-page comparison logic was not implemented.

### 3. Full-page illustrations vs. text pages

Right now every page gets a JPEG render regardless of whether the page is
mostly artwork or mostly text. For a DnD sourcebook where some pages are a
full portrait of a villain and some pages are stat blocks, it would be better
to:
- Show illustration pages as image-only (no attempt to OCR)
- Show text pages as text-only (no render, smaller file)
- Show mixed pages as both

A heuristic for this already exists implicitly: if OCR returns very little
text relative to the page area, the page is probably mostly artwork.

### 4. File size

Page renders make the EPUB large. A 200-page PDF at 150 DPI JPEG would be
roughly 30–60 MB. Options that were discussed but not implemented:
- A command-line flag to choose between text-only, render-only, or both modes
- Renders only for pages where OCR confidence is low (likely illustrations)
- Thumbnails with a link to the full render rather than embedding full-size

### 5. Column detection edge cases

The gap-based column detector works well for clean two-column layouts. It
does not handle:
- Three or more columns
- Pages where a header spans the full width above two columns (the header
  words get split between columns)
- Sidebar layouts (a narrow column beside a wide main column)
- Tables that happen to look like two columns

For the sample PDF specifically, the title text at the top of the credits page
("TRAVELLER: THE SPINWARD EXTENTS") is split between the two columns because
some of its words fall to the left of the gap and some to the right. The page
render makes this survivable — you can see what it was supposed to look like.

### 6. Mixed-language and non-Latin scripts

The OCR engine is configured for English. PDFs containing other languages,
character sets, or scripts would need the OCR language configuration adjusted.

---

## Why this was parked

Making this output genuinely polished — good enough to compete with a
publisher-sourced EPUB — is larger than a feature in an ereader app. It is a
product. It would benefit from:

- Dedicated layout analysis rather than repurposing a general-purpose OCR engine
- Semantic structure recovery, potentially using a language model to understand
  what a block of text *is* (heading, caption, footnote, sidebar) rather than
  guessing from typography alone
- Proper table extraction
- Round-trip testing against a wide variety of PDF styles and publishers

If this project is ever revisited, the architecture here — quality-gated text
extraction, word-position-based column detection, OCR fallback, page renders
as a visual safety net — is a reasonable foundation to build on. The
technology available for each of these steps will likely look very different
by the time anyone reads this.

---

## Dependencies

All dependencies are in the `venv/` virtual environment. The project requires:
- A PDF parsing and rendering library (PyMuPDF)
- An optical character recognition engine and its Python wrapper (Tesseract + pytesseract)
- An image processing library (Pillow)
- An EPUB generation library (ebooklib)

Tesseract must be installed at the system level separately from the Python
packages.
