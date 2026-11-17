"""環境變數載入與執行期設定。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from pipeline.models import ALL_MODELS


@dataclass(frozen=True)
class Config:
    gateway_base_url: str
    gateway_api_key: str
    gateway_label_prefix: str
    default_model: str


class InvalidModelError(RuntimeError):
    """--model 或 AFS_MODEL 不在 pipeline/models.py 的常數表中。"""


def load_config(env_path: str | None = None) -> Config:
    """讀取 .env。缺少任何必要變數則直接中止（尚未建立任何 run 輸出，SystemExit 合理）。"""
    load_dotenv(dotenv_path=env_path, override=False)

    base_url = os.environ.get("GATEWAY_BASE_URL", "").strip()
    api_key = os.environ.get("GATEWAY_API_KEY", "").strip()
    label_prefix = os.environ.get("GATEWAY_LABEL_PREFIX", "").strip()
    default_model = os.environ.get("AFS_MODEL", "").strip()

    missing = [
        name for name, value in [
            ("GATEWAY_BASE_URL", base_url), ("GATEWAY_API_KEY", api_key),
            ("GATEWAY_LABEL_PREFIX", label_prefix), ("AFS_MODEL", default_model),
        ] if not value
    ]
    if missing:
        raise SystemExit(f".env 缺少必要設定: {', '.join(missing)}")

    return Config(
        gateway_base_url=base_url, gateway_api_key=api_key,
        gateway_label_prefix=label_prefix, default_model=default_model,
    )


def resolve_model(cli_model: str | None, config: Config) -> str:
    """驗證 --model（或 .env 的 AFS_MODEL）是否存在於常數表，不存在則拋出 InvalidModelError。"""
    model = cli_model or config.default_model
    if model not in ALL_MODELS:
        available = "\n".join(f"  - {m}" for m in ALL_MODELS)
        raise InvalidModelError(f"模型 {model!r} 不在可用清單中，可用模型:\n{available}")
    return model


def get_client(config: Config) -> "OpenAI":
    # 以字串註記回傳型別，import 仍延後至函式內（避免模組載入即拉進 openai）
    from openai import OpenAI
    return OpenAI(api_key=config.gateway_api_key, base_url=config.gateway_base_url)
