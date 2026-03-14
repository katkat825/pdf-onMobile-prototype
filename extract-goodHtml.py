import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import os

INPUT_PDF = "input/sample.pdf"
OUT_HTML = "output/html/book.html"
OUT_IMG_DIR = "output/images"

os.makedirs("output/html", exist_ok=True)
os.makedirs("output/images", exist_ok=True)


def save_pixmap(pix, filename_base):
    """Save pixmap as PNG and return relative HTML path."""
    filename = f"{filename_base}.png"
    full_path = os.path.join(OUT_IMG_DIR, filename)
    pix.save(full_path)
    return f"../images/{filename}"


def ocr_image(path):
    """OCR a PNG image using Tesseract."""
    text = pytesseract.image_to_string(Image.open(path))
    return text.strip()


def extract_inline_images(page, page_number):
    """Extract only inline (non-fullpage) images from block structure."""
    results = []
    page_dict = page.get_text("dict")

    # Page dimensions for filtering
    page_w = page.rect.width
    page_h = page.rect.height

    for block in page_dict.get("blocks", []):
        if block["type"] != 1:
            continue  # not an image block

        bbox = block["bbox"]
        x0, y0, x1, y1 = bbox
        bw = x1 - x0
        bh = y1 - y0

        # Filter: ignore huge page-filling art
        if bw > page_w * 0.7 and bh > page_h * 0.7:
            continue

        # Extract image pixmap
        pix = fitz.Pixmap(block["image"])
        relpath = save_pixmap(
            pix,
            f"p{page_number}_img_{len(results)+1}"
        )

        results.append({
            "bbox": bbox,
            "path": relpath,
            "width": bw,
            "height": bh,
        })

    return results


def main():
    doc = fitz.open(INPUT_PDF)

    html_parts = ["""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body { font-family:sans-serif; padding:20px; max-width:650px; margin:auto; }
.page { margin-bottom:40px; }
img { width:100%; height:auto; margin:15px 0; display:block; }
p { white-space:pre-wrap; line-height:1.5; }
</style>
</head>
<body>
""" ]

    for page_num, page in enumerate(doc, start=1):
        print(f"\n--- PAGE {page_num} ---")

        # 1. OCR the full page as a fallback
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        temp_img_path = os.path.join(OUT_IMG_DIR, f"page_{page_num}_full.png")
        pix.save(temp_img_path)

        text = ocr_image(temp_img_path)
        print("Extracted text length:", len(text))

        # 2. Extract inline images
        inline_images = extract_inline_images(page, page_num)
        print("Inline images found:", len(inline_images))

        # 3. Build HTML for this page
        html_parts.append('<div class="page">')

        # Insert text first
        html_parts.append(f"<p>{text}</p>")

        # Now insert inline images under the text
        for img in inline_images:
            html_parts.append(f'<img src="{img["path"]}">')

        html_parts.append("</div>")  # end page

    html_parts.append("</body></html>")

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write("".join(html_parts))

    print("\nDONE → HTML written to:", OUT_HTML)


if __name__ == "__main__":
    main()
