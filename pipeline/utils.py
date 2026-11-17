"""檔名處理、目錄建立、字型載入等共用工具。"""
from __future__ import annotations

import hashlib
import re
import warnings
from pathlib import Path

FORBIDDEN_CHARS = r'[/\\:*?"<>|]'

_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
]

_cjk_font_cache: dict[int, object] = {}


def sanitize_filename_component(text: str, max_len: int = 20, fallback: str = "field") -> str:
    """移除路徑不安全字元與空白，中文字元保留，長度上限 max_len。空字串則回退為 fallback。"""
    cleaned = re.sub(FORBIDDEN_CHARS, "", text)
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned[:max_len]
    return cleaned or fallback


def sanitize_run_dirname(stem: str) -> str:
    """輸入檔名去副檔名並淨化，供輸出目錄命名使用（沿用同一套淨化規則，長度上限放寬）。

    額外移除 `.`：雙副檔名來源檔（例如 `製造通知單.pdf.pdf`）的 `Path.stem` 仍會殘留
    `.pdf`，若不移除會在 run 目錄名中夾一段假副檔名。crop slug 用的
    `sanitize_filename_component()` 不做此處理（欄位名稱中的 `.` 可能有意義）。"""
    cleaned = sanitize_filename_component(stem, max_len=60, fallback="input")
    cleaned = cleaned.replace(".", "")
    return cleaned or "input"


def coerce_confidence(value) -> float:
    """把 LLM 回傳的 confidence 強制轉為 [0.0, 1.0] 的 float。

    模型可能回 null（-> None）、字串（"0.95"）或完全不是數字的文字；這些值若原樣寫進
    result.json，下游報表的 `>=` 比較與 `:.2f` 格式化都會炸。無法轉換時一律視為 0.0。"""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if c != c:  # NaN：float() 不會拋錯但比較行為異常
        return 0.0
    return min(1.0, max(0.0, c))


def sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_cjk_font(size: int) -> "ImageFont.FreeTypeFont":
    """依序嘗試常見中文字型路徑，全部失敗則退回 PIL 預設字型並印出警告。"""
    from PIL import ImageFont

    if size in _cjk_font_cache:
        return _cjk_font_cache[size]

    for font_path in _CJK_FONT_CANDIDATES:
        if Path(font_path).exists():
            font = ImageFont.truetype(font_path, size=size)
            _cjk_font_cache[size] = font
            return font

    warnings.warn(
        "找不到任何中文字型（已嘗試: " + ", ".join(_CJK_FONT_CANDIDATES) + "），"
        "退回 PIL 預設字型，中文標註可能無法正確顯示。"
    )
    font = ImageFont.load_default()
    _cjk_font_cache[size] = font
    return font


def ensure_dir(path: str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
