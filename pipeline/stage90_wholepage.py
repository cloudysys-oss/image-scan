"""P2 對照組：整頁單次辨識，不做定位與裁切，直接輸出所有欄位值。

送出的是 stage0 產生的**全解析度原頁圖**（`page_00N.png`），不是定位用縮圖
（`page_00N_locate.png`）。設計文件明訂 whole-page 模式「整頁原圖(非縮圖)送 LLM」：
本階段的用途是與三階段管線做公平的對照，若先自行降解析度就等於預先削弱對照組。
平台端仍會把整頁對齊到 4 個 560×560 單位，該降解析度由平台決定，不由我們代勞。
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.llm import call_vision
from pipeline.utils import coerce_confidence


def build_wholepage_fields(raw_fields: list, page_num: int, error: str | None) -> list[dict]:
    """將單頁原始 fields 轉為輸出結構。單一欄位資料格式異常（非 dict 等）不應中止整頁處理，
    僅跳過該筆並保留其餘欄位。"""
    fields = []
    for field in raw_fields:
        try:
            fields.append({
                "page": page_num, "field_name": field.get("field_name", ""),
                "value": field.get("value", ""), "is_empty": field.get("is_empty", False),
                "confidence": coerce_confidence(field.get("confidence", 0.0)), "error": error,
            })
        except (TypeError, AttributeError):
            # 單一欄位資料格式異常（例如 raw_fields 內某項不是 dict）不應中止整頁處理。
            continue
    return fields


def run_stage90(input_meta: dict, prompt_text: str, call_ctx: dict, input_dir: Path, out_dir: Path,
                 system_prompt: str | None = None) -> tuple[dict, dict]:
    """call_ctx 需含: client, model, task_label, run_state。回傳 (result.json 內容, stats)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    all_fields = []
    stats = {"duration_ms": 0, "api_calls": 0, "failures": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "total_time_taken_per_call": []}

    for page_info in input_meta["pages"]:
        page_num = page_info["page"]
        # 全解析度原頁圖，非 _locate.png 縮圖（見模組 docstring）
        page_image_path = input_dir / page_info["file"]

        parsed, raw_text, meta = call_vision(
            client=call_ctx["client"], image_path=str(page_image_path), prompt=prompt_text,
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
        all_fields.extend(build_wholepage_fields(raw_fields, page_num, meta["error"]))

    result_doc = {"fields": all_fields}
    (out_dir / "result.json").write_text(json.dumps(result_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_doc, stats
