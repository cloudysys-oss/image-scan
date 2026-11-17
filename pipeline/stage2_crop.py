"""裁切：依 bbox_px 裁切原圖，外擴 padding，短邊過小則放大。"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from pipeline.utils import ensure_dir, sanitize_filename_component


def pad_and_clamp_bbox(bbox_px: list[int], padding_ratio: float, page_width: int, page_height: int) -> list[int]:
    x1, y1, x2, y2 = bbox_px
    pad_x = round(page_width * padding_ratio)
    pad_y = round(page_height * padding_ratio)
    return [max(0, x1 - pad_x), max(0, y1 - pad_y), min(page_width, x2 + pad_x), min(page_height, y2 + pad_y)]


def run_stage2(
    pages_fields: list[dict], input_meta: dict, padding_ratio: float, min_edge: int,
    input_dir: Path, out_dir: Path,
) -> dict:
    ensure_dir(str(out_dir))
    crops = []
    skipped = []
    page_dims = {p["page"]: (p["width"], p["height"]) for p in input_meta["pages"]}

    for page_result in pages_fields:
        page_num = page_result["page"]
        page_width, page_height = page_dims[page_num]
        img = Image.open(input_dir / f"page_{page_num:03d}.png").convert("RGB")

        for idx, field in enumerate(page_result["fields"], start=1):
            if field["invalid_bbox"]:
                skipped.append({"field_id": field["field_id"], "reason": "invalid_bbox"})
                continue

            bbox_padded = pad_and_clamp_bbox(field["bbox_px"], padding_ratio, page_width, page_height)
            crop = img.crop(tuple(bbox_padded))

            slug = sanitize_filename_component(field["field_name"])
            file_name = f"{page_num:03d}_{idx:03d}_{slug}.png"
            crop.save(out_dir / file_name)

            crop_entry = {
                "field_id": field["field_id"], "field_name": field["field_name"],
                "label_text": field["label_text"], "page": page_num, "file": file_name,
                "bbox_px_padded": bbox_padded, "width": crop.width, "height": crop.height,
                "upscaled_file": None, "upscaled_size": None,
            }

            short_edge = min(crop.width, crop.height)
            if short_edge > 0 and short_edge < min_edge:
                scale = min_edge / short_edge
                new_size = (round(crop.width * scale), round(crop.height * scale))
                upscaled = crop.resize(new_size, Image.LANCZOS)
                # 只動副檔名邊界：欄位名稱 slug 內若含 ".png" 字樣，盲目 str.replace 會改錯位置
                up_name = Path(file_name).with_stem(Path(file_name).stem + "_up").name
                upscaled.save(out_dir / up_name)
                crop_entry["upscaled_file"] = up_name
                crop_entry["upscaled_size"] = list(new_size)

            crops.append(crop_entry)

    index = {"crops": crops, "skipped": skipped}
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index
