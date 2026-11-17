"""欄位定位：對每頁定位縮圖呼叫 LLM，取得欄位清單與座標，並產生標註預覽圖。"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.llm import call_vision
from pipeline.utils import ensure_dir, get_cjk_font

MIN_AREA_NORM = 1000 * 1000 * 0.0001  # 頁面面積（正規化 1000x1000）的 0.01%


def clamp(value: int, lo: int = 0, hi: int = 1000) -> int:
    return max(lo, min(hi, value))


def normalize_bbox(raw_bbox: list) -> tuple[list[int], bool]:
    """clamp 至 [0,1000]，x1>=x2 或 y1>=y2 時交換，交換後仍無效或面積過小則標記 invalid。"""
    x1, y1, x2, y2 = (clamp(int(round(v))) for v in raw_bbox)
    if x1 >= x2:
        x1, x2 = min(x1, x2), max(x1, x2)
    if y1 >= y2:
        y1, y2 = min(y1, y2), max(y1, y2)
    invalid = x1 >= x2 or y1 >= y2
    if not invalid and (x2 - x1) * (y2 - y1) < MIN_AREA_NORM:
        invalid = True
    return [x1, y1, x2, y2], invalid


def bbox_norm_to_px(bbox_norm: list[int], page_width: int, page_height: int) -> list[int]:
    x1, y1, x2, y2 = bbox_norm
    return [
        round(x1 / 1000 * page_width), round(y1 / 1000 * page_height),
        round(x2 / 1000 * page_width), round(y2 / 1000 * page_height),
    ]


def _coerce_field_name(raw, idx: int) -> str:
    """field_name 必須是非空字串：下游 stage2 用它產生檔名、stage3 代入 prompt。
    模型有時會回整數或 null，原樣傳下去會在檔名淨化或 str.replace() 階段炸。"""
    if not isinstance(raw, dict):
        return f"field_{idx}"
    value = raw.get("field_name")
    if value is None:
        return f"field_{idx}"
    text = value if isinstance(value, str) else str(value)
    return text or f"field_{idx}"


def _coerce_label_text(raw) -> str:
    """label_text 必須是字串（可為空）。null 要變成 ""，不是字面上的 "None"。"""
    if not isinstance(raw, dict):
        return ""
    value = raw.get("label_text")
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def build_fields(raw_fields: list, page_num: int, page_width: int, page_height: int) -> list[dict]:
    fields = []
    for idx, raw in enumerate(raw_fields, start=1):
        try:
            bbox_norm, invalid = normalize_bbox(raw.get("bbox", [0, 0, 0, 0]))
            bbox_px = bbox_norm_to_px(bbox_norm, page_width, page_height)
            fields.append({
                "field_id": f"p{page_num:03d}_f{idx:03d}",
                "field_name": _coerce_field_name(raw, idx),
                "label_text": _coerce_label_text(raw),
                "bbox_norm": bbox_norm, "bbox_px": bbox_px, "invalid_bbox": invalid,
            })
        except (TypeError, ValueError, AttributeError, KeyError):
            # 單一欄位資料格式異常（非 dict、bbox 長度不對、座標非數字等）不應中止整頁處理。
            fields.append({
                "field_id": f"p{page_num:03d}_f{idx:03d}",
                "field_name": _coerce_field_name(raw, idx),
                "label_text": _coerce_label_text(raw),
                "bbox_norm": [0, 0, 0, 0], "bbox_px": [0, 0, 0, 0], "invalid_bbox": True,
            })
    return fields


def _draw_dashed_rect(draw, box, color, dash=8):
    x1, y1, x2, y2 = box
    for x in range(x1, x2, dash * 2):
        draw.line([(x, y1), (min(x + dash, x2), y1)], fill=color, width=3)
        draw.line([(x, y2), (min(x + dash, x2), y2)], fill=color, width=3)
    for y in range(y1, y2, dash * 2):
        draw.line([(x1, y), (x1, min(y + dash, y2))], fill=color, width=3)
        draw.line([(x2, y), (x2, min(y + dash, y2))], fill=color, width=3)


def draw_preview(page_png_path: Path, fields: list[dict], out_path: Path) -> None:
    img = Image.open(page_png_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = get_cjk_font(24)
    for idx, field in enumerate(fields, start=1):
        x1, y1, x2, y2 = field["bbox_px"]
        color = "gray" if field["invalid_bbox"] else "red"
        if field["invalid_bbox"]:
            _draw_dashed_rect(draw, (x1, y1, x2, y2), color)
        else:
            draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
        label = f"{idx}. {field['field_name']}"
        text_pos = (x1, max(0, y1 - 26))
        text_bbox = draw.textbbox(text_pos, label, font=font)
        draw.rectangle(text_bbox, fill="white")
        draw.text(text_pos, label, fill=color, font=font)
    img.save(out_path)


def run_stage1(input_meta: dict, prompt_text: str, call_ctx: dict, out_dir: Path,
                system_prompt: str | None = None) -> tuple[list[dict], dict]:
    """
    call_ctx 需含: client, model, task_label, run_state。
    回傳 (每頁 fields.json 內容的 list, stats)。
    """
    ensure_dir(str(out_dir))
    all_pages_fields = []
    stats = {"duration_ms": 0, "api_calls": 0, "failures": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "total_time_taken_per_call": []}

    for page_info in input_meta["pages"]:
        page_num = page_info["page"]
        locate_path = out_dir.parent / "00_input" / page_info["locate_file"]

        parsed, raw_text, meta = call_vision(
            client=call_ctx["client"], image_path=str(locate_path), prompt=prompt_text,
            model=call_ctx["model"], task_label=call_ctx["task_label"],
            run_state=call_ctx["run_state"], system_prompt=system_prompt, max_tokens=4096,
        )
        # api_calls 反映真實 HTTP 請求數：一次 call_vision 內部可能重試多次，每次都是付費請求
        stats["api_calls"] += meta["attempts"]
        stats["duration_ms"] += meta["latency_ms"]
        stats["total_time_taken_per_call"].append(meta["total_time_taken"])
        stats["prompt_tokens"] += meta["usage"]["prompt_tokens"]
        stats["completion_tokens"] += meta["usage"]["completion_tokens"]
        stats["total_tokens"] += meta["usage"]["total_tokens"]
        if meta["error"]:
            stats["failures"] += 1

        (out_dir / f"page_{page_num:03d}_response.txt").write_text(raw_text, encoding="utf-8")

        raw_fields = (parsed or {}).get("fields", []) if parsed else []
        if not isinstance(raw_fields, list):
            raw_fields = []
        fields = build_fields(raw_fields, page_num, page_info["width"], page_info["height"])

        page_result = {
            "page": page_num, "image_width": page_info["width"], "image_height": page_info["height"],
            "fields": fields, "error": meta["error"],
        }
        (out_dir / f"page_{page_num:03d}_fields.json").write_text(
            json.dumps(page_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        original_png = out_dir.parent / "00_input" / page_info["file"]
        draw_preview(original_png, fields, out_dir / f"page_{page_num:03d}_preview.png")

        all_pages_fields.append(page_result)

    return all_pages_fields, stats
