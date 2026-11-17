"""LLM Gateway 呼叫封裝：round/label 產生、JSON 解析容錯、重試、response_format 退回。"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import openai
from openai import OpenAI

ROUND_PATTERN = re.compile(r"[a-z0-9-]{1,24}")
LABEL_PATTERN = re.compile(r"[a-z0-9-]{1,64}")


def resolve_round(cli_round: str | None) -> tuple[str, str]:
    """回傳 (round, source)。source 為 'auto' 或 'specified'。
    整個 run 只呼叫一次，結果存進 config 全程沿用。"""
    if cli_round:
        r, src = cli_round.lower(), "specified"
    else:
        r, src = datetime.now().strftime("%Y%m%d-%H%M"), "auto"
    if not ROUND_PATTERN.fullmatch(r):
        raise SystemExit(f"--round 只允許 [a-z0-9-] 且不超過 24 字元，收到: {cli_round!r}")
    return r, src


def build_label(prefix: str, round_tag: str, stage: str) -> str:
    """組出 X-Task-Label。stage 為 probe/locate/extract/wholepage。"""
    label = f"{prefix}-{round_tag}-{stage}"
    assert LABEL_PATTERN.fullmatch(label), f"非法標籤: {label}"
    label.encode("ascii")  # 送出前再次確認純 ASCII，中文會在 httpx header 編碼階段炸掉
    return label


@dataclass
class RunState:
    """單次 run 全程共用的可變狀態。"""
    response_format_supported: bool | None = None  # None=尚未測試，True/False=已確認
    notes: list = field(default_factory=list)
    usage_missing_models: set = field(default_factory=set)  # 已就「未回傳 usage」記過 note 的模型


class GatewayAuthError(RuntimeError):
    """401 - API Key 已被撤銷。"""


class GatewayProjectDisabledError(RuntimeError):
    """403 - Project 已被停用。"""


class GatewayConnectionError(RuntimeError):
    """連線被拒或 DNS 解析失敗。"""


def parse_total_time_taken(value) -> float | None:
    """AFS 特有的 total_time_taken 實測回傳的是字串 `"0.49 sec"`，不是數字。
    轉成秒數 float 以便跨 attempt 累加；無法解析則回 None（原始值另外保留於 *_raw）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return None


def _is_response_format_error(exc: Exception) -> bool:
    body = str(getattr(exc, "message", "")) + str(getattr(exc, "body", ""))
    return "response_format" in body.lower()


def parse_json_response(raw_text: str) -> dict | None:
    """依序嘗試 4 種方式解析 LLM 回應為 JSON，全部失敗回傳 None。
    只有解析結果是 dict 才算成功，其餘型別（list/str/number/bool）視同解析失敗，
    以維持函式簽名承諾的 dict | None 回傳型別。"""
    try:
        result = json.loads(raw_text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(raw_text[start:end + 1])
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def call_vision(
    client: OpenAI,
    image_path: str,
    prompt: str,
    model: str,
    task_label: str,
    run_state: RunState,
    system_prompt: str | None = None,
    max_tokens: int = 4096,
    max_retries: int = 3,
) -> tuple[dict | None, str, dict]:
    """
    回傳 (解析後的 dict 或 None, 原始回應文字, 中繼資訊)。
    中繼資訊含 attempts, latency_ms, usage, total_time_taken, task_label, error。
    task_label 不給預設值，強制每個呼叫端明確指定；一律經由 build_label() 產生。

    system_prompt 為 None（預設）時，行為與既有版本逐位元組相同：單一 user 訊息，
    content 陣列含圖片與文字。system_prompt 有值時，在前面加一則 system 訊息，
    user 訊息維持圖片+文字的結構不變——這是本函式唯一新增的分支，其餘重試/
    退避/JSON 容錯/token 累加邏輯完全不受影響。
    """
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    user_message = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ],
    }
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}, user_message]
    else:
        messages = [user_message]

    last_error = None
    raw_text = ""
    attempts = 0
    delays = [2, 4, 8]

    # 跨 attempt 累加：重試時每一次都是真實付費請求，只回傳最後一次的數字會低報用量與耗時。
    acc_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    acc_latency_ms = 0
    acc_total_time_taken = None  # 秒數（float），只在至少有一次回應帶此欄位時才不是 None
    acc_total_time_taken_raw = []  # 各 attempt 回傳的原始值，如 ["0.49 sec"]
    vary_seed = False  # 僅在 json 解析失敗的重試才變動 seed，見下方註解

    def _accumulate_total_time_taken(value) -> None:
        nonlocal acc_total_time_taken
        if value is None:
            return
        acc_total_time_taken_raw.append(value)
        seconds = parse_total_time_taken(value)
        if seconds is not None:
            acc_total_time_taken = seconds if acc_total_time_taken is None else acc_total_time_taken + seconds

    def _build_meta(error) -> dict:
        return {
            "attempts": attempts,
            "latency_ms": acc_latency_ms,
            "usage": dict(acc_usage),
            # 跨 attempt 累加的秒數（四捨五入至 3 位，避免浮點加總的 36.239999999999995）；
            # 原始字串另存 *_raw 以保留平台回傳的原文
            "total_time_taken": None if acc_total_time_taken is None else round(acc_total_time_taken, 3),
            "total_time_taken_raw": list(acc_total_time_taken_raw),
            "task_label": task_label,
            "error": error,
        }

    for attempt_index in range(max_retries):
        attempts += 1
        # temperature=0.0 + 固定 seed=42 會讓「JSON 解析失敗」的重試逐字重現同一份無法解析的
        # 輸出，等於白燒 token。故此類重試改動 seed；429/timeout/5xx 屬於傳輸層的暫時性失敗，
        # 與模型的決定性無關，維持固定 seed=42 以保留重現性。
        seed = 42 + attempt_index if vary_seed else 42
        kwargs = dict(
            model=model, messages=messages, temperature=0.0, top_p=0.5,
            max_tokens=max_tokens, seed=seed, extra_headers={"X-Task-Label": task_label},
        )
        if run_state.response_format_supported is not False:
            kwargs["response_format"] = {"type": "json_object"}

        start = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as exc:
            time.sleep(0.5)
            raise GatewayAuthError(str(exc)) from exc
        except openai.PermissionDeniedError as exc:
            time.sleep(0.5)
            raise GatewayProjectDisabledError(str(exc)) from exc
        except openai.APITimeoutError as exc:
            acc_latency_ms += int((time.monotonic() - start) * 1000)
            last_error = f"timeout: {exc}"
            time.sleep(0.5)
            if attempt_index < max_retries - 1:
                time.sleep(delays[min(attempt_index, len(delays) - 1)])
            continue
        except openai.APIConnectionError as exc:
            time.sleep(0.5)
            raise GatewayConnectionError(str(exc)) from exc
        except openai.RateLimitError as exc:
            acc_latency_ms += int((time.monotonic() - start) * 1000)
            last_error = f"429: {exc}"
            time.sleep(0.5)
            if attempt_index < max_retries - 1:
                time.sleep(delays[min(attempt_index, len(delays) - 1)])
            continue
        except openai.InternalServerError as exc:
            acc_latency_ms += int((time.monotonic() - start) * 1000)
            last_error = f"5xx: {exc}"
            time.sleep(0.5)
            if attempt_index < max_retries - 1:
                time.sleep(delays[min(attempt_index, len(delays) - 1)])
            continue
        except openai.APIStatusError as exc:
            acc_latency_ms += int((time.monotonic() - start) * 1000)
            time.sleep(0.5)
            if "response_format" in kwargs and _is_response_format_error(exc):
                run_state.response_format_supported = False
                run_state.notes.append(
                    f"model={model} 拒絕 response_format 參數，本次執行後續呼叫已永久移除該參數"
                )
                continue  # 立即以移除該參數的方式重試，不是「每次都重試一遍」
            last_error = f"{exc.status_code}: {exc}"
            break  # 其他未列舉的 4xx：視為不可重試的一般錯誤，不中止整個 run

        acc_latency_ms += int((time.monotonic() - start) * 1000)
        time.sleep(0.5)
        raw_text = resp.choices[0].message.content or ""
        finish_reason = resp.choices[0].finish_reason
        usage = getattr(resp, "usage", None)
        dump = resp.model_dump() if hasattr(resp, "model_dump") else {}
        if usage is None:
            if model not in run_state.usage_missing_models:
                run_state.usage_missing_models.add(model)
                run_state.notes.append(f"model={model} 未回傳 usage 欄位，token 統計以 0 計")
        else:
            acc_usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            acc_usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
            acc_usage["total_tokens"] += getattr(usage, "total_tokens", 0) or 0
        _accumulate_total_time_taken(dump.get("total_time_taken"))

        if finish_reason == "length":
            return None, raw_text, _build_meta("truncated")

        parsed = parse_json_response(raw_text)
        if parsed is not None:
            return parsed, raw_text, _build_meta(None)

        last_error = "json_parse_failed"
        if attempt_index < max_retries - 1:
            vary_seed = True  # 下一次重試換 seed，避免決定性地重現同一份壞輸出
            time.sleep(delays[min(attempt_index, len(delays) - 1)])
            continue
        return None, raw_text, _build_meta(last_error)

    return None, raw_text, _build_meta(last_error or "unknown_error")
