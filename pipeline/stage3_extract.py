"""值辨識：逐張裁切圖（有放大版則用放大版）序列送 LLM，取得填入值與信心值。"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.llm import call_vision
from pipeline.utils import coerce_confidence


def build_extract_prompt(template: str, field_name: str, label_text: str) -> str:
    """以字串取代而非 str.format()：prompt 範本內含 JSON 範例的字面大括號
    ({"value": ...}),若用 .format() 會誤判成佔位符而炸 KeyError。"""
    return template.replace("{field_name}", field_name).replace("{label_text}", label_text)


def run_stage3(crop_index: dict, prompt_template: str, call_ctx: dict, crops_dir: Path, out_dir: Path,
               system_prompt: str | None = None) -> tuple[dict, dict]:
    """call_ctx 需含: client, model, task_label, run_state。回傳 (result.json 內容, stats)。序列執行，不併發。"""
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    results = []
    stats = {"duration_ms": 0, "api_calls": 0, "failures": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "total_time_taken_per_call": []}

    for crop in crop_index["crops"]:
        image_file = crop["upscaled_file"] or crop["file"]
        image_path = crops_dir / image_file
        prompt = build_extract_prompt(prompt_template, crop["field_name"], crop["label_text"])

        parsed, raw_text, meta = call_vision(
            client=call_ctx["client"], image_path=str(image_path), prompt=prompt,
            model=call_ctx["model"], task_label=call_ctx["task_label"],
            run_state=call_ctx["run_state"], system_prompt=system_prompt, max_tokens=512,
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

        (raw_dir / f"{crop['field_id']}_response.txt").write_text(raw_text, encoding="utf-8")

        parsed = parsed or {}
        results.append({
            "field_id": crop["field_id"], "field_name": crop["field_name"], "page": crop["page"],
            "crop_file": crop["file"], "value": parsed.get("value", ""),
            "is_empty": parsed.get("is_empty", False),
            "confidence": coerce_confidence(parsed.get("confidence", 0.0)),
            "notes": parsed.get("notes", ""), "error": meta["error"],
            "attempts": meta["attempts"], "latency_ms": meta["latency_ms"],
            "total_time_taken": meta["total_time_taken"],
        })

    result_doc = {"results": results}
    (out_dir / "result.json").write_text(json.dumps(result_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_doc, stats
