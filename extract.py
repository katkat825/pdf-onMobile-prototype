"""
extract.py  —  PDF → HTML + EPUB converter

See README.md for full project context, known limitations, and the rationale
for parking this work.  This docstring covers the code-level decisions.

─────────────────────────────────────────────────────────────────────────────
The problem
─────────────────────────────────────────────────────────────────────────────
The target PDFs are print-layout sourcebooks (RPG manuals, etc.) where most or
all pages are full-page rasterized images baked into the PDF.  There is no
selectable text, or where there is, the font encoding is broken so characters
come out wrong.  Standard approaches fail:

  • Native text extraction: broken font encodings cause missing characters
    (common culprit: ligature glyphs like fi/fl stored as unmapped Private Use
    Area codepoints, so "first" extracts as "rst").
  • Whole-page OCR: works for text accuracy but reads straight down the image,
    interleaving multi-column content in the wrong order.

─────────────────────────────────────────────────────────────────────────────
Approach
─────────────────────────────────────────────────────────────────────────────
1. For each page, attempt native text extraction and run it through quality
   checks (character count, garbled-text ratio, Private Use Area char ratio).
   If it passes, use native extraction — this preserves bold, italic, colour,
   and heading levels from font size metadata.

2. If native extraction fails the quality checks (most pages in this class of
   PDF), fall back to OCR.  The OCR engine returns word-level bounding boxes,
   not just a text string.  We use those bounding boxes to:
     a) Build a density histogram of word positions across the page width.
     b) Find the lowest-density gap in the centre region — that gap is the
        column boundary (see find_column_gap).
     c) Sort words into their correct column, then reconstruct lines and
        paragraphs from vertical proximity.

3. Regardless of text path, render every page to a JPEG and embed it above
   the reflowed text.  For pages that are pure illustrations, the render is
   the only content that matters.  For text pages it acts as a visual reference.

4. After paragraph reconstruction, scan each paragraph for runs of ALL-CAPS
   words.  These are promoted to <h3> headings.  This is a heuristic and
   over-fires on credits/label pages, but those aren't reading content.

─────────────────────────────────────────────────────────────────────────────
Known gaps (see README.md for detail)
─────────────────────────────────────────────────────────────────────────────
  • Tables: OCR reads them row-by-row, producing garbled output.  Fixing this
    requires visual grid detection and per-cell OCR — not attempted here.
  • Headers/footers: page numbers and running chapter titles are included in
    the body text.  The word bounding boxes needed to strip them exist in the
    OCR output; the cross-page comparison logic was not implemented.
  • File size: every page render adds ~150–300 KB.  No mode flag to disable
    renders was implemented.
  • Column edge cases: headers that span the full page width above two columns
    get split between columns; three-column and sidebar layouts are not handled.

─────────────────────────────────────────────────────────────────────────────
Output
─────────────────────────────────────────────────────────────────────────────
  output/book.html   — page renders + reflowed text
  output/book.epub   — same content packaged for ereader apps
  output/renders/    — JPEG renders of each page
  output/images/     — content images extracted directly from the PDF
                       (empty for this sample PDF, which bakes everything
                        into full-page scans)
"""

import re
from pathlib import Path
from collections import Counter

import fitz                     # PyMuPDF
from PIL import Image
import pytesseract
from pytesseract import Output as TessOutput
from ebooklib import epub


# ── Paths ────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent
PDF_PATH    = ROOT / "input"  / "sample.pdf"
OUT_DIR     = ROOT / "output"
IMG_DIR     = OUT_DIR / "images"
RENDER_DIR  = OUT_DIR / "renders"
HTML_PATH   = OUT_DIR / "book.html"
EPUB_PATH   = OUT_DIR / "book.epub"

# ── Tunables ─────────────────────────────────────────────────────────────────
RENDER_DPI     = 150    # DPI for page JPEG renders (visual reference)
OCR_DPI        = 300    # DPI for Tesseract (higher = better accuracy)
TEXT_MIN_CHARS = 30     # fewer chars extracted natively → force OCR
IMG_MIN_PX     = 50     # skip embedded images smaller than this (px)
IMG_THIN_RATIO = 0.05   # skip embedded images where min/max ratio < this
PUA_MAX_RATIO  = 0.005  # max PUA-char fraction before forcing OCR
TWO_COL_STRIPS = 50     # x-axis strips used for gap-based column detection
TESS_MIN_CONF  = 30     # minimum Tesseract word confidence to keep


# ═════════════════════════════════════════════════════════════════════════════
# Native text / font helpers
# ═════════════════════════════════════════════════════════════════════════════

def dominant_body_size(doc):
    """Most-common span font size across the document (= body text size)."""
    sizes = []
    for page in doc:
        for blk in page.get_text("dict")["blocks"]:
            if blk["type"] != 0:
                continue
            for ln in blk["lines"]:
                for sp in ln["spans"]:
                    if sp["text"].strip():
                        sizes.append(round(sp["size"], 1))
    return Counter(sizes).most_common(1)[0][0] if sizes else 11.0


def size_to_tag(size, body):
    r = size / body if body else 1.0
    if r >= 2.0:  return "h1"
    if r >= 1.6:  return "h2"
    if r >= 1.3:  return "h3"
    if r >= 1.1:  return "h4"
    return "p"


def render_span(sp):
    """One PyMuPDF span → HTML snippet preserving bold/italic/colour."""
    raw = sp["text"]
    if not raw:
        return ""
    txt   = raw.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    flags = sp.get("flags", 0)
    bold  = bool(flags & (1 << 4))
    ital  = bool(flags & (1 << 1))
    c     = sp.get("color", 0)
    r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
    style = f' style="color:rgb({r},{g},{b})"' if (r, g, b) != (0, 0, 0) else ""
    if bold and ital: return f"<strong><em{style}>{txt}</em></strong>"
    if bold:          return f"<strong{style}>{txt}</strong>"
    if ital:          return f"<em{style}>{txt}</em>"
    if style:         return f"<span{style}>{txt}</span>"
    return txt


def render_text_block(blk, body):
    """PyMuPDF text block → <hN> or <p> element."""
    spans = [sp for ln in blk["lines"] for sp in ln["spans"]]
    sizes = [sp["size"] for sp in spans if sp["text"].strip()]
    if not sizes:
        return ""
    tag = size_to_tag(Counter(sizes).most_common(1)[0][0], body)
    lines_html = []
    for ln in blk["lines"]:
        lh = "".join(render_span(sp) for sp in ln["spans"])
        if lh.strip():
            lines_html.append(lh)
    if not lines_html:
        return ""
    return f"<{tag}>{' '.join(lines_html)}</{tag}>\n"


# ═════════════════════════════════════════════════════════════════════════════
# Text quality checks
# ═════════════════════════════════════════════════════════════════════════════

def text_is_usable(text):
    """
    Return False if extracted text is garbage.
    Catches: replacement chars, PUA ligature failures (missing 'f', 'fi', etc.).
    """
    if not text or len(text) < TEXT_MIN_CHARS:
        return False
    total = len(text)
    bad = (text.count("\ufffd") + text.count("□") + text.count("▯")
           + sum(1 for c in text if not (c.isprintable() or c in "\n\t\r")))
    pua = sum(1 for c in text if "\ue000" <= c <= "\uf8ff")
    return bad / total < 0.15 and pua / total < PUA_MAX_RATIO


def blocks_are_usable(blocks):
    """Return False if too many spans are empty (font encoding failure)."""
    total = empty = 0
    for blk in blocks:
        if blk["type"] != 0:
            continue
        for ln in blk["lines"]:
            for sp in ln["spans"]:
                total += 1
                if not sp["text"].strip():
                    empty += 1
    return total > 0 and (empty / total) < 0.20


# ═════════════════════════════════════════════════════════════════════════════
# Layout helpers (for native-text pages)
# ═════════════════════════════════════════════════════════════════════════════

def find_column_gap(x_centres, span, n=TWO_COL_STRIPS):
    """
    Given a list of x-centre values within [0, span], look for a whitespace
    gap in the middle 60% of the range.  Returns (gap_found, split_x).

    Uses a density histogram: if the minimum-density strip in the centre region
    is < 25% of the average density on each side, that strip is a column gap.
    """
    if len(x_centres) < 8:
        return False, span / 2

    strip_w = span / n
    counts  = [0] * n
    for x in x_centres:
        counts[min(int(x / strip_w), n - 1)] += 1

    lo, hi = int(n * 0.2), int(n * 0.8)          # centre 60%
    centre  = counts[lo:hi]
    if not any(c > 0 for c in centre):
        return False, span / 2

    gap_local = centre.index(min(centre))
    gap_idx   = lo + gap_local
    split_x   = (gap_idx + 0.5) * strip_w

    left_cnt  = [c for c in counts[:gap_idx]     if c > 0]
    right_cnt = [c for c in counts[gap_idx + 1:] if c > 0]
    if not left_cnt or not right_cnt:
        return False, span / 2

    left_avg  = sum(left_cnt)  / len(left_cnt)
    right_avg = sum(right_cnt) / len(right_cnt)
    threshold = (left_avg + right_avg) / 4        # gap must be < 25% of avg

    if counts[gap_idx] <= threshold:
        return True, split_x
    return False, span / 2


def block_column_count(text_blocks, page_w):
    """Detect column count from PyMuPDF block x-centres using gap detection."""
    centres = [(b["bbox"][0] + b["bbox"][2]) / 2 for b in text_blocks]
    found, _ = find_column_gap(centres, page_w)
    return 2 if found else 1


def block_reading_order(blocks, cols, page_w):
    """Sort blocks by reading order; handles 2-column."""
    if cols == 1:
        return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    mid   = page_w / 2
    left  = sorted([b for b in blocks if (b["bbox"][0]+b["bbox"][2])/2 <  mid],
                   key=lambda b: b["bbox"][1])
    right = sorted([b for b in blocks if (b["bbox"][0]+b["bbox"][2])/2 >= mid],
                   key=lambda b: b["bbox"][1])
    return left + right


# ═════════════════════════════════════════════════════════════════════════════
# Page rendering
# ═════════════════════════════════════════════════════════════════════════════

def render_page(page, pnum):
    """
    Render the page at RENDER_DPI as a JPEG.
    Returns (relative_html_path, PIL.Image at OCR_DPI).
    The PIL image is at OCR_DPI for Tesseract — we render once at the higher DPI
    and downscale for the saved JPEG to save space.
    """
    RENDER_DIR.mkdir(parents=True, exist_ok=True)

    # Render at OCR_DPI for Tesseract quality
    mat_ocr = fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72)
    pix     = page.get_pixmap(matrix=mat_ocr, alpha=False)
    img_ocr = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Downscale to RENDER_DPI for the saved file
    scale   = RENDER_DPI / OCR_DPI
    save_w  = int(pix.width  * scale)
    save_h  = int(pix.height * scale)
    img_save = img_ocr.resize((save_w, save_h), Image.LANCZOS)

    fname = f"page_{pnum:04d}.jpg"
    img_save.save(RENDER_DIR / fname, "JPEG", quality=72, optimize=True)

    return f"renders/{fname}", img_ocr


# ═════════════════════════════════════════════════════════════════════════════
# Column-aware OCR using word-level bounding boxes
# ═════════════════════════════════════════════════════════════════════════════

def _split_caps_headings(text):
    """
    Scan a paragraph for embedded ALL-CAPS heading runs and split them out.
    Handles headings at the start, middle, or end of a block.

    Returns a list of (tag, text) pairs.

    A 'heading run' is 1–6 consecutive words where every word is all-uppercase
    (after stripping hyphens/asterisks) and at least 2 chars long, AND either:
      - the run starts the block, or
      - the preceding context is body text.
    """
    words = text.split()
    if not words:
        return [("p", text)]

    def is_caps_token(w):
        core = w.replace("-", "").replace("*", "").replace("'", "")
        return len(core) >= 2 and core.isupper()

    segments   = []   # list of (tag, [word list])
    body_buf   = []   # current body words

    i = 0
    while i < len(words):
        # Try to match a heading run at position i
        j = i
        while j < len(words) and is_caps_token(words[j]):
            j += 1
        run_len = j - i

        if 1 <= run_len <= 6:
            # Confirm it's a heading, not just an acronym mid-sentence:
            # At least one word in the run must be >2 chars (not just "I", "A")
            # AND the run must not be preceded by a body word on the same line
            # (we relax this: any caps run of 1-6 that is followed by body text
            #  or ends the block is treated as a heading).
            is_heading = any(len(words[k].replace("-","").replace("*","")) > 2
                             for k in range(i, j))
            if is_heading:
                if body_buf:
                    segments.append(("p", " ".join(body_buf)))
                    body_buf = []
                segments.append(("h3", " ".join(words[i:j])))
                i = j
                continue

        body_buf.append(words[i])
        i += 1

    if body_buf:
        segments.append(("p", " ".join(body_buf)))

    return segments if segments else [("p", text)]


def ocr_with_columns(img):
    """
    Use Tesseract's word-level output to detect columns, sort words into
    reading order, group into lines and paragraphs, return HTML fragments.

    Works on any page layout including multi-column scanned pages.
    """
    data = pytesseract.image_to_data(img, output_type=TessOutput.DICT)

    # Collect words with acceptable confidence
    words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = 0
        if txt and conf >= TESS_MIN_CONF:
            words.append({
                "text":      txt,
                "x":         data["left"][i],
                "y":         data["top"][i],
                "w":         data["width"][i],
                "h":         data["height"][i],
                "block_num": data["block_num"][i],
                "par_num":   data["par_num"][i],
                "line_num":  data["line_num"][i],
            })

    if not words:
        return []

    # ── Detect columns using gap detection on word x-centres ──
    img_w      = img.width
    x_centres  = [w["x"] + w["w"] / 2 for w in words]
    is_two_col, split_x = find_column_gap(x_centres, img_w)

    if is_two_col:
        left_words  = [w for w in words if (w["x"] + w["w"] / 2) <  split_x]
        right_words = [w for w in words if (w["x"] + w["w"] / 2) >= split_x]
        cols = [left_words, right_words]
        print(" (2-col)", end="")
    else:
        cols = [words]

    html_parts = []

    for col_words in cols:
        if not col_words:
            continue

        # Sort by Tesseract's structural numbers, then x within a line
        col_words.sort(key=lambda w: (w["block_num"], w["par_num"],
                                      w["line_num"], w["x"]))

        # ── Group words into visual lines ──
        lines     = []
        cur_line  = [col_words[0]]
        for word in col_words[1:]:
            prev = cur_line[-1]
            # Same line if the word's top is within 70% of the previous word's height
            if abs(word["y"] - prev["y"]) < prev["h"] * 0.7:
                cur_line.append(word)
            else:
                lines.append(cur_line)
                cur_line = [word]
        if cur_line:
            lines.append(cur_line)

        # ── Group lines into paragraphs (gap > 1.5× line height = new para) ──
        paragraphs   = []
        cur_para     = []
        last_bottom  = None
        for line_words in lines:
            line_top    = min(w["y"]            for w in line_words)
            line_bottom = max(w["y"] + w["h"]  for w in line_words)
            avg_h       = sum(w["h"] for w in line_words) / len(line_words)
            gap         = (line_top - last_bottom) if last_bottom is not None else 0

            if last_bottom is None or gap < avg_h * 1.5:
                cur_para.append(line_words)
            else:
                if cur_para:
                    paragraphs.append(cur_para)
                cur_para = [line_words]
            last_bottom = line_bottom
        if cur_para:
            paragraphs.append(cur_para)

        # ── Convert paragraphs to HTML ──
        for para_lines in paragraphs:
            parts = []
            for line_words in para_lines:
                line_text = " ".join(
                    w["text"] for w in sorted(line_words, key=lambda w: w["x"])
                )
                parts.append(line_text)
            text = " ".join(parts).strip()
            if not text:
                continue

            # Split any embedded ALL-CAPS headings from body text.
            # Pattern: heading words (ALL CAPS, ≥3 chars each) followed by a
            # lowercase sentence.  E.g. "PRE-ASLAN ERA The first non-native…"
            segments = _split_caps_headings(text)
            for seg_tag, seg_text in segments:
                seg_text = (seg_text.replace("&", "&amp;")
                                    .replace("<", "&lt;")
                                    .replace(">", "&gt;"))
                html_parts.append(f"<{seg_tag}>{seg_text}</{seg_tag}>\n")

    return html_parts


# ═════════════════════════════════════════════════════════════════════════════
# Embedded image extraction (for non-scanned pages with real image blocks)
# ═════════════════════════════════════════════════════════════════════════════

def extract_image_blocks(image_blocks, pnum):
    """
    Save content images from PyMuPDF image blocks.
    Filters out: tiny images, very thin images (rules/borders).
    Returns sorted list of (y_pos, html_img_tag).
    """
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for idx, blk in enumerate(image_blocks):
        img_bytes = blk.get("image")
        if not img_bytes:
            continue
        w   = blk.get("width",  0)
        h_p = blk.get("height", 0)
        if w < IMG_MIN_PX or h_p < IMG_MIN_PX:
            continue
        # Skip borders / dividers (extremely thin elements)
        if min(w, h_p) / max(w, h_p) < IMG_THIN_RATIO:
            continue
        ext   = blk.get("ext", "png")
        fname = f"p{pnum:04d}_{idx:03d}.{ext}"
        (IMG_DIR / fname).write_bytes(img_bytes)
        y_pos = blk["bbox"][1]
        results.append((y_pos, f'<img src="images/{fname}" alt="" class="content-image">\n'))
    return sorted(results, key=lambda x: x[0])


def merge_text_and_images(text_parts, images_by_y, page_height):
    """
    Interleave text paragraphs and images by approximate vertical position.
    Uses the image's y-coordinate fraction of the page to pick an insert point.
    """
    if not images_by_y:
        return list(text_parts)
    if not text_parts:
        return [tag for _, tag in images_by_y]

    n = len(text_parts)
    # Map each image to the index of the paragraph it should precede
    insertions = {}
    for y_pos, tag in images_by_y:
        frac = y_pos / page_height if page_height > 0 else 0
        idx  = max(0, min(n - 1, int(frac * n)))
        insertions.setdefault(idx, []).append(tag)

    result = []
    for i, para in enumerate(text_parts):
        for img_tag in insertions.get(i, []):
            result.append(img_tag)
        result.append(para)
    # Any images mapped past the last paragraph
    for img_tag in insertions.get(n, []):
        result.append(img_tag)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Per-page conversion
# ═════════════════════════════════════════════════════════════════════════════

def page_to_html(page, pnum, body):
    """Convert one page to an HTML fragment, returning the string."""
    print(f"  page {pnum}: ", end="", flush=True)

    # ── 1. Render page (visual reference + OCR source) ──
    render_rel, img_ocr = render_page(page, pnum)
    render_tag = (
        f'<figure class="page-render">\n'
        f'  <img src="{render_rel}" alt="Original page {pnum}" loading="lazy">\n'
        f'</figure>\n'
    )

    # ── 2. Get block geometry (reliable even with bad fonts) ──
    d            = page.get_text("dict", flags=fitz.TEXT_MEDIABOX_CLIP)
    all_blocks   = d["blocks"]
    text_blocks  = [b for b in all_blocks if b["type"] == 0]
    image_blocks = [b for b in all_blocks if b["type"] == 1]

    # ── 3. Decide: native text or OCR? ──
    plain      = page.get_text("text").strip()
    use_native = (text_is_usable(plain)
                  and blocks_are_usable(text_blocks)
                  and len(text_blocks) > 0)

    # ── 4. Get text HTML parts ──
    text_html = []

    if use_native:
        print("native", end="")
        pw   = page.rect.width
        cols = block_column_count(text_blocks, pw)
        if cols == 2:
            print(" (2-col)", end="")
        ordered = block_reading_order(all_blocks, cols, pw)
        for blk in ordered:
            if blk["type"] == 0:
                h = render_text_block(blk, body)
                if h:
                    text_html.append(h)
    else:
        print("OCR", end="")
        text_html = ocr_with_columns(img_ocr)

    # ── 5. Extract any embedded content images ──
    content_images = extract_image_blocks(image_blocks, pnum)

    # ── 6. Merge text + embedded images by position ──
    ph     = page.rect.height
    merged = merge_text_and_images(text_html, content_images, ph)

    print()  # newline after status

    parts = [f'<div class="page" id="page-{pnum}">\n', render_tag,
             '<div class="page-text">\n']
    parts.extend(merged)
    parts.append('</div>\n</div>\n')
    return "".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# CSS
# ═════════════════════════════════════════════════════════════════════════════

CSS = """\
/* ── Reset / base ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 1rem;
    line-height: 1.7;
    color: #111;
    background: #fafafa;
    max-width: 760px;
    margin: 0 auto;
    padding: 1rem 1.25rem;
}

/* ── Headings ── */
h1, h2, h3, h4 {
    font-family: Arial, Helvetica, sans-serif;
    line-height: 1.2;
    margin: 1.5em 0 0.4em;
}
h1 { font-size: 2em;   }
h2 { font-size: 1.6em; }
h3 { font-size: 1.3em; }
h4 { font-size: 1.1em; }

/* ── Paragraphs ── */
p { margin: 0.6em 0; }

/* ── Page sections ── */
.page {
    border-bottom: 2px solid #e0e0e0;
    padding-bottom: 2em;
    margin-bottom: 2.5em;
}

/* ── Page render (original visual) ── */
.page-render {
    margin: 0 0 1.25em;
    border: 1px solid #ccc;
    border-radius: 3px;
    overflow: hidden;
    background: #fff;
}
.page-render img {
    width: 100%;
    height: auto;
    display: block;
}

/* ── Inline content images extracted from PDF ── */
.content-image {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}

/* ── Mobile ── */
@media (max-width: 600px) {
    body { padding: 0.4rem; font-size: 0.95rem; }
}
"""

CSS_EPUB = """\
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 1em;
    line-height: 1.65;
    color: #111;
}
h1,h2,h3,h4 {
    font-family: Arial, Helvetica, sans-serif;
    line-height: 1.2;
    margin: 1.3em 0 0.4em;
}
h1{font-size:1.8em} h2{font-size:1.5em} h3{font-size:1.2em} h4{font-size:1.05em}
p{margin:0.5em 0}
.page-render img, .content-image { max-width:100%; height:auto; display:block; margin:0.8em auto; }
"""


# ═════════════════════════════════════════════════════════════════════════════
# Top-level driver
# ═════════════════════════════════════════════════════════════════════════════

def main():
    for d in (OUT_DIR, IMG_DIR, RENDER_DIR):
        d.mkdir(parents=True, exist_ok=True)

    doc  = fitz.open(str(PDF_PATH))
    n    = len(doc)
    body = dominant_body_size(doc)
    print(f"Opened: {PDF_PATH.name}  ({n} pages, body {body}pt)")

    page_frags = []
    for i, page in enumerate(doc, 1):
        page_frags.append(page_to_html(page, i, body))
    doc.close()

    # ── Write HTML ────────────────────────────────────────────────────────────
    full_html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '  <title>PDF Export</title>\n'
        f'  <style>\n{CSS}\n  </style>\n'
        '</head>\n<body>\n'
        + "\n".join(page_frags)
        + '\n</body>\n</html>'
    )
    HTML_PATH.write_text(full_html, encoding="utf-8")
    print(f"HTML  -> {HTML_PATH}")

    # ── Write EPUB ────────────────────────────────────────────────────────────
    book = epub.EpubBook()
    book.set_title(PDF_PATH.stem)
    book.set_language("en")

    css_item = epub.EpubItem(
        uid="css", file_name="style.css",
        media_type="text/css", content=CSS_EPUB.encode()
    )
    book.add_item(css_item)

    # Add page renders to EPUB
    render_items = {}
    for p in sorted(RENDER_DIR.iterdir()):
        if p.suffix.lower() == ".jpg":
            item = epub.EpubItem(
                uid=f"render_{p.stem}",
                file_name=f"renders/{p.name}",
                media_type="image/jpeg",
                content=p.read_bytes(),
            )
            book.add_item(item)
            render_items[p.name] = item

    # Add extracted content images to EPUB
    ext_mime = {".png":"image/png", ".jpg":"image/jpeg",
                ".jpeg":"image/jpeg", ".gif":"image/gif", ".webp":"image/webp"}
    if IMG_DIR.exists():
        for p in sorted(IMG_DIR.iterdir()):
            mt = ext_mime.get(p.suffix.lower())
            if mt:
                book.add_item(epub.EpubItem(
                    uid=f"img_{p.stem}",
                    file_name=f"images/{p.name}",
                    media_type=mt,
                    content=p.read_bytes(),
                ))

    chapters = []
    for i, frag in enumerate(page_frags, 1):
        ch = epub.EpubHtml(
            title=f"Page {i}",
            file_name=f"page_{i:04d}.xhtml",
            lang="en",
        )
        ch.set_content((
            "<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<head><title>Page {i}</title>"
            "<link href='style.css' rel='stylesheet' type='text/css'/></head>"
            f"<body>{frag}</body></html>"
        ).encode("utf-8"))
        ch.add_item(css_item)
        book.add_item(ch)
        chapters.append(ch)

    book.spine = ["nav"] + chapters
    book.toc   = tuple(
        epub.Link(f"page_{i:04d}.xhtml", f"Page {i}", f"p{i}")
        for i in range(1, len(chapters) + 1)
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(EPUB_PATH), book)
    print(f"EPUB  -> {EPUB_PATH}")


if __name__ == "__main__":
    main()
