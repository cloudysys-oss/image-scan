"""平台可用模型常數表。個體名稱逐字照抄，不可自行推導或正規化。"""

# gemma-4-31b-it：2026-08-10 實測定位階段（stage1_locate）bbox 確實貼合欄位內容，
# 已由 VISION_UNKNOWN 移入，並設為 .env 的 AFS_MODEL 預設值。
# 對照組 llama32-ffm-11b-v-32k 的 bbox 只是照抄 prompt 範例後等差遞增，未曾真正框選圖片內容。
VISION_CONFIRMED = ["llama32-ffm-11b-v-32k", "phi-4-reasoning-vision-15b-32k", "gemma-4-31b-it"]
VISION_UNKNOWN = [
    "gemma-4-26b-a4b-it",
    "gemma-4-12b-it",
    "gemma-3-taide-12b-chat-2602",
]
TEXT_ONLY = ["gpt-oss-120b-32k", "gpt-oss-20b-32k", "llama-33-ffm-70b"]

ALL_MODELS = VISION_CONFIRMED + VISION_UNKNOWN + TEXT_ONLY
