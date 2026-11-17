"""探測 VISION_UNKNOWN（Gemma 系列）是否支援圖片輸入，VISION_CONFIRMED 作對照組。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.config import get_client, load_config
from pipeline.llm import RunState, build_label, call_vision, resolve_round
from pipeline.models import VISION_CONFIRMED, VISION_UNKNOWN
from pipeline.utils import get_cjk_font

PROBE_TEXT = "測試 A1"
PROMPT = "圖片中的文字是什麼？只回答文字內容，不要加任何說明。"


def make_probe_image(path: Path) -> Path:
    img = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(img)
    font = get_cjk_font(48)
    draw.text((40, 70), PROBE_TEXT, fill="black", font=font)
    img.save(path)
    return path


def probe_model(client, image_path: str, model: str, task_label: str, run_state: RunState) -> dict:
    parsed, raw_text, meta = call_vision(
        client=client, image_path=image_path, prompt=PROMPT, model=model,
        task_label=task_label, run_state=run_state, max_tokens=64,
    )
    answered_text = raw_text.strip()
    correct = PROBE_TEXT.replace(" ", "") in answered_text.replace(" ", "")
    return {
        "model": model,
        "http_ok": meta["error"] is None,
        "error": meta["error"],
        "raw_response": answered_text,
        "correct": correct if meta["error"] is None else False,
    }


def main() -> None:
    config = load_config()
    client = get_client(config)
    round_tag, _source = resolve_round(None)
    label = build_label(config.gateway_label_prefix, round_tag, "probe")
    run_state = RunState()

    tmp_dir = Path("runs") / "_probe"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    image_path = make_probe_image(tmp_dir / "probe.png")

    results = []
    for model in VISION_UNKNOWN + VISION_CONFIRMED:
        print(f"probing {model} ...")
        results.append(probe_model(client, str(image_path), model, label, run_state))

    header = f"{'model':32} {'http':10} {'correct':8} raw_response"
    print()
    print(header)
    print("-" * len(header))
    for r in results:
        status = "200" if r["http_ok"] else f"FAIL({r['error']})"
        print(f"{r['model']:32} {status:10} {str(r['correct']):8} {r['raw_response'][:40]}")

    if run_state.notes:
        print("\nnotes:")
        for note in run_state.notes:
            print(f"  - {note}")


if __name__ == "__main__":
    main()
