"""前處理：PDF/圖片轉為分頁 PNG，產生定位縮圖，記錄文字層資訊。"""
from __future__ import annotations

import json
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from pipeline.utils import ensure_dir


def parse_page_spec(pages: str, page_count: int) -> list[int]:
    """解析 --pages 參數（'all' / '1' / '1-3' / '1,3,5'），回傳 1-based 頁碼清單。"""
    if pages == "all":
        return list(range(1, page_count + 1))
    result: set[int] = set()
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return sorted(p for p in result if 1 <= p <= page_count)


def render_pdf_pages(source_path: str, dpi: int, page_numbers: list[int], out_dir: Path) -> list[dict]:
    doc = fitz.open(source_path)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages_info = []
    for page_num in page_numbers:
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=matrix)
        file_name = f"page_{page_num:03d}.png"
        pix.save(str(out_dir / file_name))
        text = page.get_text()
        pages_info.append({
            "page": page_num, "file": file_name, "width": pix.width, "height": pix.height,
            "has_text_layer": len(text.strip()) > 0, "text_layer_char_count": len(text),
        })
    doc.close()
    return pages_info


def copy_single_image(source_path: str, out_dir: Path) -> list[dict]:
    img = Image.open(source_path).convert("RGB")
    file_name = "page_001.png"
    img.save(out_dir / file_name)
    return [{
        "page": 1, "file": file_name, "width": img.width, "height": img.height,
        "has_text_layer": False, "text_layer_char_count": 0,
    }]


def make_locate_thumbnail(page_png_path: Path, max_edge: int, out_dir: Path, page_num: int) -> dict:
    """縮至長邊 max_edge（Lanczos），若已小於則直接複製，不放大。"""
    img = Image.open(page_png_path)
    long_edge = max(img.width, img.height)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        new_size = (round(img.width * scale), round(img.height * scale))
        thumb = img.resize(new_size, Image.LANCZOS)
    else:
        thumb = img.copy()
    file_name = f"page_{page_num:03d}_locate.png"
    thumb.save(out_dir / file_name)
    return {"locate_file": file_name, "locate_width": thumb.width, "locate_height": thumb.height}


def run_stage0(source_path: str, dpi: int, locate_max_edge: int, pages_spec: str, out_dir: Path) -> dict:
    ensure_dir(str(out_dir))
    source_type = "pdf" if source_path.lower().endswith(".pdf") else "image"

    if source_type == "pdf":
        doc = fitz.open(source_path)
        total_pages = doc.page_count
        doc.close()
        page_numbers = parse_page_spec(pages_spec, total_pages)
        pages_info = render_pdf_pages(source_path, dpi, page_numbers, out_dir)
    else:
        pages_info = copy_single_image(source_path, out_dir)

    for info in pages_info:
        thumb_info = make_locate_thumbnail(out_dir / info["file"], locate_max_edge, out_dir, info["page"])
        info.update(thumb_info)

    meta = {
        "source_path": str(Path(source_path).resolve()), "source_type": source_type,
        "dpi": dpi, "locate_max_edge": locate_max_edge, "page_count": len(pages_info),
        "pages": pages_info,
    }
    (out_dir / "input_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
