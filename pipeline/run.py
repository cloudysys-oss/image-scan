"""CLI 進入點：串接前處理→定位→裁切→值辨識→報表（及 P2 whole-page 對照組）。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from pipeline import stage0_input, stage1_locate, stage2_crop, stage3_extract, stage4_report, stage90_wholepage
from pipeline.config import InvalidModelError, get_client, load_config, resolve_model
from pipeline.llm import (
    GatewayAuthError, GatewayConnectionError, GatewayProjectDisabledError,
    RunState, build_label, resolve_round,
)
from pipeline.utils import ensure_dir, sanitize_run_dirname, sha256_file

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_stage_prompt(prompt_mode: str, base_name: str) -> tuple[str, str | None, dict]:
    """依 --prompt-mode 讀取指定階段（base_name 例如 'locate_v1'）的 prompt 檔案。

    回傳 (prompt_text, system_prompt, prompts_meta_entry)。
    user-only：system_prompt 為 None，prompts_meta_entry 沿用既有的 {"file", "sha256"} 兩欄。
    system-user：prompts_meta_entry 改為 {"system_file", "system_sha256", "user_file", "user_sha256"}
    四欄——兩種模式各自內部一致，不強求跨模式同構（檔案數量本來就不同）。"""
    if prompt_mode not in ("user-only", "system-user"):
        raise ValueError(f"unknown prompt_mode: {prompt_mode!r}")
    if prompt_mode == "user-only":
        path = PROMPTS_DIR / f"{base_name}.txt"
        text = path.read_text(encoding="utf-8")
        return text, None, {"file": f"prompts/{base_name}.txt", "sha256": sha256_file(str(path))}

    system_path = PROMPTS_DIR / f"{base_name}_system.txt"
    user_path = PROMPTS_DIR / f"{base_name}_user.txt"
    system_text = system_path.read_text(encoding="utf-8")
    user_text = user_path.read_text(encoding="utf-8")
    if not system_text.strip() or not user_text.strip():
        raise SystemExit(
            f"--prompt-mode system-user 需要的 prompt 檔案內容為空："
            f"{system_path if not system_text.strip() else user_path}"
        )
    return user_text, system_text, {
        "system_file": f"prompts/{base_name}_system.txt", "system_sha256": sha256_file(str(system_path)),
        "user_file": f"prompts/{base_name}_user.txt", "user_sha256": sha256_file(str(user_path)),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Image Scan POC pipeline")
    parser.add_argument("--input", required=True)
    parser.add_argument("--round", default=None)
    parser.add_argument("--round-note", default="")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--locate-max-edge", type=int, default=1120)
    parser.add_argument("--pages", default="all")
    parser.add_argument("--model", default=None)
    parser.add_argument("--crop-padding", type=float, default=0.01)
    parser.add_argument("--crop-min-edge", type=int, default=560)
    parser.add_argument("--out", default="runs")
    parser.add_argument("--mode", choices=["pipeline", "whole-page", "both"], default="pipeline")
    parser.add_argument("--prompt-mode", choices=["user-only", "system-user"], default="user-only")
    return parser.parse_args(argv)


def append_rounds_md(rounds_path: Path, row: dict) -> None:
    is_new = not rounds_path.exists()
    with open(rounds_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write("| round | run_id | 時間 | 輸入檔 | 模型 | locate prompt | extract prompt | 欄位數 | 失敗 | total tokens | 定位判定 | 值正確率 | note |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        f.write(
            f"| {row['round']} | {row['run_id']} | {row['time']} | {row['input_file']} | "
            f"{row['model']} | `{row['locate_sha']}` | `{row['extract_sha']}` | "
            f"{row['field_count']} | {row['failed']} | {row['total_tokens']} | | | {row['note']} |\n"
        )


def write_meta(run_dir: Path, meta: dict) -> None:
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _prompt_sha_for_rounds(prompts_entry: dict) -> str:
    """ROUNDS.md 的 hash 欄位需要對兩種 --prompt-mode 形狀都有意義的代表值：
    user-only 用 sha256；system-user 沒有單一 sha256，改用兩個檔案各自雜湊的前6碼組合，
    確保 system 或 user 檔案任一邊改變時，這一欄都會跟著變，不會落回容易誤讀的 "-"。"""
    if "sha256" in prompts_entry:
        sha = prompts_entry.get("sha256", "")
        return sha[:6] or "-"
    system_sha = prompts_entry.get("system_sha256", "")
    user_sha = prompts_entry.get("user_sha256", "")
    if not system_sha and not user_sha:
        return "-"
    return f"{system_sha[:6]}+{user_sha[:6]}"


STAGE_STAT_KEYS = ("duration_ms", "api_calls", "failures", "total_tokens", "total_time_taken_per_call")


def validate_pages_spec(pages_spec: str, input_path: Path) -> None:
    """在建立任何輸出目錄之前檢查 --pages。

    與 --round 相同，這屬於純 CLI 參數驗證失敗，不該產生半套的 run 目錄與 meta.json，
    因此直接 SystemExit 而非走 _finish_with_error。
    注意：圖片輸入（單頁）不會用到 --pages（copy_single_image 不查詢此參數），
    這是預期行為，不需修正，但此處仍會擋掉語法錯誤的值，避免留下誤導。"""
    if pages_spec == "all":
        return
    # 先做語法檢查（不先解析成頁碼集合：'1-99999999' 之類的值會撐爆記憶體）。
    # 頁碼為 1-based，故第一位數不接受 0。
    page_num = r"[1-9]\d{0,4}"
    if not re.fullmatch(rf"{page_num}(-{page_num})?(,{page_num}(-{page_num})?)*",
                        pages_spec.replace(" ", "")):
        raise SystemExit(
            f"--pages 格式錯誤，只接受 'all' / '1' / '1-3' / '1,3,5'"
            f"（頁碼為 1 起算的正整數，上限 5 位數），收到: {pages_spec!r}"
        )
    if input_path.suffix.lower() != ".pdf":
        return  # 單頁圖片：--pages 不生效（見 docstring），語法對就放行

    try:
        import fitz
        doc = fitz.open(str(input_path))
        total_pages = doc.page_count
        doc.close()
    except Exception:  # noqa: BLE001 - 開檔失敗留給 stage0 以正常錯誤路徑處理
        return
    if not stage0_input.parse_page_spec(pages_spec, total_pages):
        raise SystemExit(
            f"--pages {pages_spec!r} 未選到任何存在的頁碼："
            f"{input_path.name} 只有 {total_pages} 頁"
        )


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config()
    round_tag, round_source = resolve_round(args.round)
    validate_pages_spec(args.pages, Path(args.input).resolve())

    # run_dir/meta 在模型驗證之前就先建立：--model 不在常數表中時，規格要求仍需在
    # meta.json.errors 留下記錄（不同於 --round 非法時「不建立任何輸出目錄」的規則，
    # 見 project-purpose.md §10）。此時 model 尚未驗證，先用候選值填入 config.model。
    now = datetime.now().astimezone()
    input_path = Path(args.input).resolve()
    stem = sanitize_run_dirname(input_path.stem)
    run_dir_name = f"{round_tag}_{now.strftime('%H%M%S')}_{stem}"
    run_dir = ensure_dir(str(Path(args.out) / run_dir_name))

    meta = {
        "run_id": run_dir_name, "round": round_tag, "round_source": round_source,
        "round_note": args.round_note, "started_at": now.isoformat(), "finished_at": None,
        "input": {"path": str(input_path), "type": None, "page_count": None},
        "config": {"dpi": args.dpi, "locate_max_edge": args.locate_max_edge,
                   "crop_padding": args.crop_padding, "model": args.model or config.default_model,
                   "prompt_mode": args.prompt_mode},
        "gateway": {"base_url": config.gateway_base_url, "label_prefix": config.gateway_label_prefix,
                    "labels_used": []},
        "prompts": {}, "stages": {},
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        # notes 與 errors 分開：errors 代表「真的出錯了」，notes 代表「依設計發生了退回/替代行為」
        # （例如 response_format 不被支援）。規格 §8/§10 明訂後者記於 meta.json.notes。
        "notes": [],
        "errors": [],
    }

    run_state = None  # 供 _finish_with_error 判斷 client 建立前的極早期錯誤路徑

    def _finish_with_error(message: str) -> int:
        meta["errors"].append(message)
        if run_state is not None and run_state.notes:
            meta["notes"].extend(run_state.notes)
        meta["finished_at"] = datetime.now().astimezone().isoformat()
        write_meta(run_dir, meta)
        print(f"錯誤：{message}")
        return 1

    try:
        model = resolve_model(args.model, config)
    except InvalidModelError as exc:
        return _finish_with_error(str(exc))

    print(f"[--] round={round_tag} ({round_source})  "
          f"label={config.gateway_label_prefix}-{round_tag}-*  model={model}")

    try:
        client = get_client(config)
        run_state = RunState()

        print(f"[00] 前處理 ... dpi={args.dpi}")
        input_meta = stage0_input.run_stage0(
            str(input_path), args.dpi, args.locate_max_edge, args.pages, run_dir / "00_input"
        )
        meta["input"]["type"] = input_meta["source_type"]
        meta["input"]["page_count"] = input_meta["page_count"]
        first_page = input_meta["pages"][0]
        print(f"[00] 前處理 ... {input_meta['page_count']} 頁，DPI {args.dpi}，"
              f"定位縮圖 {first_page['locate_width']}x{first_page['locate_height']}")

        extract_results = None
        preview_paths = []
        wholepage_result = None

        def accumulate_tokens(stats: dict) -> None:
            meta["token_usage"]["prompt_tokens"] += stats["prompt_tokens"]
            meta["token_usage"]["completion_tokens"] += stats["completion_tokens"]
            meta["token_usage"]["total_tokens"] += stats["total_tokens"]

        if args.mode in ("pipeline", "both"):
            locate_prompt, locate_system_prompt, meta["prompts"]["locate"] = load_stage_prompt(
                args.prompt_mode, "locate_v1"
            )
            locate_label = build_label(config.gateway_label_prefix, round_tag, "locate")
            meta["gateway"]["labels_used"].append(locate_label)

            print(f"[01] 欄位定位 ... page 1/{input_meta['page_count']}")
            pages_fields, locate_stats = stage1_locate.run_stage1(
                input_meta, locate_prompt,
                {"client": client, "model": model, "task_label": locate_label, "run_state": run_state},
                run_dir / "01_layout", system_prompt=locate_system_prompt,
            )
            accumulate_tokens(locate_stats)
            meta["stages"]["locate"] = {"label": locate_label,
                                        **{k: locate_stats[k] for k in STAGE_STAT_KEYS}}
            total_fields = sum(len(p["fields"]) for p in pages_fields)
            invalid_fields = sum(1 for p in pages_fields for f in p["fields"] if f["invalid_bbox"])
            print(f"[01] 欄位定位 ... 共取得 {total_fields} 個欄位（{invalid_fields} 個座標無效）")
            preview_paths = [(p["page"], run_dir / "01_layout" / f"page_{p['page']:03d}_preview.png")
                              for p in pages_fields]

            crop_index = stage2_crop.run_stage2(
                pages_fields, input_meta, args.crop_padding, args.crop_min_edge,
                run_dir / "00_input", run_dir / "02_crops",
            )
            print(f"[02] 裁切 ... {len(crop_index['crops'])} 張")

            extract_prompt, extract_system_prompt, meta["prompts"]["extract"] = load_stage_prompt(
                args.prompt_mode, "extract_v1"
            )
            extract_label = build_label(config.gateway_label_prefix, round_tag, "extract")
            meta["gateway"]["labels_used"].append(extract_label)

            extract_doc, extract_stats = stage3_extract.run_stage3(
                crop_index, extract_prompt,
                {"client": client, "model": model, "task_label": extract_label, "run_state": run_state},
                run_dir / "02_crops", run_dir / "03_extract", system_prompt=extract_system_prompt,
            )
            accumulate_tokens(extract_stats)
            meta["stages"]["extract"] = {"label": extract_label,
                                         **{k: extract_stats[k] for k in STAGE_STAT_KEYS}}
            extract_results = extract_doc["results"]
            failed = sum(1 for r in extract_results if r["error"])
            print(f"[03] 值辨識 ... {len(extract_results)}/{len(extract_results)}（失敗 {failed}）")

        if args.mode in ("whole-page", "both"):
            wholepage_prompt, wholepage_system_prompt, meta["prompts"]["wholepage"] = load_stage_prompt(
                args.prompt_mode, "wholepage_v1"
            )
            wholepage_label = build_label(config.gateway_label_prefix, round_tag, "wholepage")
            meta["gateway"]["labels_used"].append(wholepage_label)
            wholepage_result, wholepage_stats = stage90_wholepage.run_stage90(
                input_meta, wholepage_prompt,
                {"client": client, "model": model, "task_label": wholepage_label, "run_state": run_state},
                run_dir / "00_input", run_dir / "90_wholepage", system_prompt=wholepage_system_prompt,
            )
            accumulate_tokens(wholepage_stats)
            meta["stages"]["wholepage"] = {"label": wholepage_label,
                                           **{k: wholepage_stats[k] for k in STAGE_STAT_KEYS}}
            print(f"[90] whole-page 對照組 ... {len(wholepage_result['fields'])} 個欄位")

        report_path = run_dir / "report.html"
        stage4_report.build_report(
            meta={"input_filename": input_path.name, "started_at": now.isoformat(), "model": model,
                  "prompt_mode": args.prompt_mode,
                  # stages 供報表計算「辨識失敗數」：定位整頁失敗時 extract_results 是空的，
                  # 只看它會把上游全掛顯示成 0 失敗（見 stage4_report.count_stage_failures）
                  "stages": meta["stages"]},
            extract_results=extract_results, preview_paths=preview_paths,
            crops_dir=run_dir / "02_crops", run_dir_name=run_dir_name,
            wholepage_result=wholepage_result, out_path=report_path,
        )
        print(f"[04] 報表 ... {report_path}")

        if run_state.notes:
            meta["notes"].extend(run_state.notes)

        meta["finished_at"] = datetime.now().astimezone().isoformat()
        write_meta(run_dir, meta)

        # 失敗數取「所有實際跑過的階段」加總：定位整頁失敗時不會有裁切與值辨識呼叫，
        # 若只看 extract_results 會寫出「0 欄位、0 失敗」，與「本來就沒有欄位」無法區分。
        total_failed = sum(
            int(meta["stages"].get(stage, {}).get("failures", 0) or 0)
            for stage in ("locate", "extract", "wholepage")
        )
        if extract_results is not None:
            field_count = len(extract_results)
        elif wholepage_result is not None:
            field_count = len(wholepage_result.get("fields") or [])  # 單獨 whole-page 模式
        else:
            field_count = 0

        append_rounds_md(Path(args.out) / "ROUNDS.md", {
            "round": round_tag, "run_id": run_dir_name, "time": now.strftime("%m-%d %H:%M"),
            "input_file": input_path.name, "model": model,
            "locate_sha": _prompt_sha_for_rounds(meta["prompts"].get("locate", {})),
            "extract_sha": _prompt_sha_for_rounds(meta["prompts"].get("extract", {})),
            "field_count": field_count,
            "failed": total_failed,
            "total_tokens": meta["token_usage"]["total_tokens"], "note": args.round_note,
        })
        return 0

    except GatewayAuthError:
        return _finish_with_error("Gateway API Key 已被撤銷，請至 Dashboard 重新產生")
    except GatewayProjectDisabledError:
        return _finish_with_error("Gateway Project 已被停用，請至 Dashboard 重新啟用")
    except GatewayConnectionError:
        return _finish_with_error(f"無法連線至 Gateway，目前使用的位址為 {config.gateway_base_url}")
    except Exception as exc:  # noqa: BLE001 - 依規格「保留原始資料、記錄錯誤、繼續執行」，頂層兜底避免未捕捉 traceback
        return _finish_with_error(f"未預期的錯誤: {exc}\n{traceback.format_exc()}")


if __name__ == "__main__":
    sys.exit(main())
