"""
pdf_extractor.py — PDF page rendering and routing
Converts PDF pages to base64 JPEG images for the Claude API.
"""
import base64
import json
from pathlib import Path
from io import BytesIO

try:
    import pypdfium2 as pdfium
    PDFIUM_OK = True
except ImportError:
    PDFIUM_OK = False
    print("WARNING: pypdfium2 not installed. PDF processing will be unavailable.")

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    from lib.snbi_items import classify_doc_type, PAGE_CLEARANCE
except ImportError:
    from snbi_items import classify_doc_type, PAGE_CLEARANCE


def page_to_base64_jpeg(pdf_path, page_index, dpi=150, max_px=3000):
    """
    Render one PDF page to a base64-encoded JPEG string.
    page_index is 0-based.
    """
    if not PDFIUM_OK:
        raise RuntimeError("pypdfium2 is required for PDF processing.")

    doc  = pdfium.PdfDocument(pdf_path)
    page = doc[page_index]

    scale = dpi / 72.0
    bitmap = page.render(scale=scale, rotation=0)
    pil_img = bitmap.to_pil()

    # Downscale if needed
    w, h = pil_img.size
    if max(w, h) > max_px:
        ratio = max_px / max(w, h)
        pil_img = pil_img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    # Convert to RGB (PDF renders may be RGBA)
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")

    buf = BytesIO()
    pil_img.save(buf, format="JPEG", quality=85)
    buf.seek(0)

    doc.close()
    return base64.b64encode(buf.read()).decode("utf-8")


def get_page_count(pdf_path):
    if not PDFIUM_OK:
        return 0
    doc = pdfium.PdfDocument(pdf_path)
    n = len(doc)
    doc.close()
    return n


def load_metadata(bridge_dir):
    """
    Load metadata.json from bridge directory.
    Returns dict mapping drawing_number → {doc_type, year} or empty dict.
    """
    meta_path = Path(bridge_dir) / "metadata.json"
    if meta_path.exists():
        with open(meta_path, "r") as f:
            return json.load(f)
    return {}


def build_page_plan(pdf_path, metadata):
    """
    Build a list of (page_index, page_type, doc_type_str, drawing_number)
    for a Plans PDF, using metadata.json for routing.

    The PDF bookmarks in the planset match drawing numbers in metadata.json.
    We use the page order to assign page types when we can't match exactly.

    Returns list of dicts:
        {page_index, page_type, doc_type_str, drawing_number, year}
    """
    if not PDFIUM_OK:
        return []

    page_count = get_page_count(pdf_path)
    if page_count == 0:
        return []

    # Try to get bookmark/outline from PDF to map page numbers to drawing numbers
    doc = pdfium.PdfDocument(pdf_path)
    bookmark_map = {}  # page_index → title
    try:
        _extract_bookmarks(doc.get_toc(), bookmark_map)
    except Exception:
        pass
    doc.close()

    plan = []
    metadata_values = list(metadata.values())  # ordered list of {doc_type, year}

    for page_idx in range(page_count):
        # Try bookmark match first
        bm_title = bookmark_map.get(page_idx, "")
        doc_type_str = ""
        drawing_number = ""
        year = ""

        # Match bookmark title to a drawing number in metadata
        if bm_title:
            for dwg_num, meta in metadata.items():
                if dwg_num in bm_title or bm_title.startswith(dwg_num):
                    doc_type_str   = meta.get("doc_type", "")
                    drawing_number = dwg_num
                    year           = meta.get("year", "")
                    break

        # Fall back to sequential metadata assignment
        if not doc_type_str and page_idx < len(metadata_values):
            m = metadata_values[page_idx]
            doc_type_str = m.get("doc_type", "")
            year         = m.get("year", "")

        page_type = classify_doc_type(doc_type_str)

        plan.append({
            "page_index":     page_idx,
            "page_type":      page_type,
            "doc_type_str":   doc_type_str or f"Page {page_idx + 1}",
            "drawing_number": drawing_number,
            "year":           year,
        })

    return plan


def _extract_bookmarks(toc, result, parent_page=None):
    """Recursively extract bookmarks into a {page_index: title} dict."""
    for item in toc:
        try:
            page = item.page_index
            if page is not None:
                result[page] = item.title
        except Exception:
            pass
        if item.n_kids > 0:
            _extract_bookmarks(item, result, parent_page)


def select_priority_pages(page_plan, needed_page_types, max_pages=8):
    """
    From a full page plan, select the highest-priority pages to send to the API.
    Limits to max_pages to control cost.
    Prioritises: PLAN, SECTION, VICINITY first, then NOTES, RAIL, BENT.
    """
    priority_order = {
        "PLAN": 0, "VICINITY": 1, "SECTION": 2,
        "NOTES": 3, "RAIL": 4, "BENT": 5, "CLEARANCE": 6,
    }
    filtered = [p for p in page_plan if p["page_type"] in needed_page_types]
    filtered.sort(key=lambda p: (priority_order.get(p["page_type"], 9), p["page_index"]))

    # Deduplicate: one page per type, then add more if budget allows
    seen_types = set()
    selected = []
    for p in filtered:
        if p["page_type"] not in seen_types:
            selected.append(p)
            seen_types.add(p["page_type"])
    # Fill remaining budget with next best pages
    for p in filtered:
        if len(selected) >= max_pages:
            break
        if p not in selected:
            selected.append(p)

    selected.sort(key=lambda p: p["page_index"])
    return selected[:max_pages]


def get_vc_pdf_paths(bridge_dir, bridge_id):
    """Find all vertical clearance PDFs in a bridge directory."""
    d = Path(bridge_dir)
    if not d.exists():
        return []
    return sorted([str(p) for p in d.glob(f"{bridge_id}_BC_*.pdf")])
