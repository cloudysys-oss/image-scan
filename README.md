# Image Scan POC（純 LLM 版）

以 LLM 解析 PDF / 圖片表單並自動填入欄位值的技術可行性 POC。

本專案是「中小企業私有化生成式 AI 導入平台暨企業知識推論服務驗證計畫」的研究成果之一，於數位發展部數位產業署 115 年算力平台（AFS ModelSpace）上實際執行，透過 [LLM Gateway](https://github.com/cloudysys-oss/llm-gateway) 呼叫平台上的視覺語言模型。

此版本為**純 LLM 架構**：

1. **定位**：整頁縮圖送 LLM，由模型輸出各欄位的 bounding box。
2. **裁切**：依 bbox 裁切並放大，讓每個欄位獨享一張圖的解析度預算。
3. **值辨識**：逐欄位送 LLM 讀取欄位值，輸出 value / confidence。
4. **整頁對照組**：整頁原圖單次送 LLM，用來比較兩種架構。

研究過程與結論見第 5 節（各模型視覺能力探測）與第 9 節（已知限制）。

> `sample-data/demo_purchase_order.png` 為虛構的示範表單，所有公司、人名、地址與編號均非真實。

## 1. 安裝

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # 編輯 .env,填入 GATEWAY_API_KEY
```

## 2. 執行範例

```bash
# 三階段管線（預設模式）
python -m pipeline.run --input sample-data/demo_purchase_order.png --round baseline --round-note "初次跑通"

# 同時跑 whole-page 對照組，report.html 會多出並列比較區
python -m pipeline.run --input sample-data/demo_purchase_order.png --mode both --round baseline --round-note "含 whole-page 對照組"

# 多頁 PDF，只跑第 1 到第 3 頁
python -m pipeline.run --input "path/to/form.pdf" --pages 1-3 --round baseline
```

### `--mode`：三種模式

| 值 | 行為 |
|---|---|
| `pipeline`（預設） | 三階段管線：定位 → 裁切 → 逐欄位值辨識。每個欄位獨享一張圖的解析度預算 |
| `whole-page` | P2 對照組：整頁**原圖**單次送 LLM，直接要求輸出所有欄位值，不做定位與裁切。結果寫入 `90_wholepage/result.json` |
| `both` | 兩種都跑，並在 `report.html` 產生「對照比較」區：同一欄位名稱**完全相等**才對齊成同一列，只出現在單側的欄位仍各自列出（另一側顯示「—」），不做模糊配對 |

`whole-page` 使用獨立的 `wholepage` 階段標籤，因此在**同一個 `--round`** 下跑 `both`，即可在 Dashboard 的 By Label 分頁直接讀出 `...-locate` + `...-extract` 對 `...-wholepage` 的 token 成本差距。

其餘參數（`--dpi`、`--locate-max-edge`、`--pages`、`--model`、`--crop-padding`、`--crop-min-edge`、`--out`、`--prompt-mode`）見 `project-purpose.md` 第 5 節。
注意 `--pages` 僅對 PDF 輸入生效；單張圖片輸入固定為一頁。

### `--prompt-mode`：兩種訊息結構

| 值 | 行為 |
|---|---|
| `user-only`（預設） | 現行行為：單一 user 訊息，圖片與文字混在同一則 content 陣列裡 |
| `system-user` | 拆成兩則訊息：system 放人設＋規則＋輸出格式，user 放觸發句（或 extract 階段的逐欄位變數）+ 圖片。三個階段（定位/辨識/whole-page）都支援 |

兩種模式對應不同組的 prompt 檔案（`prompts/*_v1.txt` vs `prompts/*_v1_system.txt` + `prompts/*_v1_user.txt`），內容逐字相同，只是拆法不同——不會改變任何規則或輸出格式。

`meta.json` 的 `prompts` 區塊在兩種模式下形狀不同：`user-only` 是 `{"file", "sha256"}`；`system-user` 是 `{"system_file", "system_sha256", "user_file", "user_sha256"}`。讀取 `meta.json` 的任何工具都需要處理這兩種形狀。

> 這個選項目前只是基礎設施——實際比較兩種模式在哪個模型上表現較好，是使用者自己要做的實驗，程式不做任何自動評分。

## 3. 輸出目錄結構

每次執行在 `--out`（預設 `runs/`）下建立一個目錄 `{round}_{HHMMSS}_{輸入檔名}/`，例如 `runs/baseline_171529_demo_purchase_order/`：

```
runs/{round}_{HHMMSS}_{name}/
├── 00_input/
│   ├── page_001.png              # 原始解析度（--dpi），供裁切與 whole-page 使用
│   ├── page_001_locate.png       # 長邊縮至 --locate-max-edge 的縮圖，供定位使用
│   ├── page_002.png              # 多頁 PDF 逐頁遞增
│   ├── page_002_locate.png
│   └── input_meta.json           # 頁數、尺寸、文字層資訊
├── 01_layout/                    # pipeline / both 模式
│   ├── page_001_response.txt     # 定位階段 LLM 原始回應，完整保留
│   ├── page_001_fields.json      # 解析與防呆後的欄位座標
│   └── page_001_preview.png      # 紅框標註圖（人工判斷定位是否正確的主要依據）
├── 02_crops/                     # pipeline / both 模式
│   ├── 001_001_申請人姓名.png       # {page:03d}_{idx:03d}_{slug}.png
│   ├── 001_001_申請人姓名_up.png    # 短邊 < --crop-min-edge 時的 Lanczos 放大版（送 API 用）
│   └── index.json                # 裁切清單與 skipped（invalid_bbox）記錄
├── 03_extract/                   # pipeline / both 模式
│   ├── raw/
│   │   └── p001_f001_response.txt # 每個欄位的 LLM 原始回應
│   └── result.json               # 各欄位的 value / confidence / notes / error
├── 90_wholepage/                 # 僅 whole-page / both 模式
│   ├── page_001_response.txt
│   └── result.json
├── report.html                   # 單一自包含報表（圖片以 base64 內嵌，可單獨寄送）
└── meta.json                     # 本次執行的完整記錄（見 project-purpose.md 第 7 節）
```

`runs/` 內容不進版控，唯一例外是 `runs/ROUNDS.md`（輪次總表，每次執行自動追加一列）。

`meta.json` 中 `notes` 與 `errors` 是兩件不同的事：`errors` 代表真的出錯了；`notes` 代表依設計發生了退回或替代行為（例如某模型不支援 `response_format`、或未回傳 `usage` 欄位）。一次成功的執行 `errors` 應為空陣列，`notes` 則可能有內容。

## 4. 人工判讀流程

1. 開啟 `runs/{round}_*/report.html`
2. 先看「定位檢查」區塊,對每頁的紅框標註勾選:定位正確 / 部分錯誤 / 大量錯誤
3. 逐列判讀「欄位判讀表」,為每個欄位選擇:未判定 / 正確 / 錯誤 / 部分正確,並可加註人工備註
4. 按「匯出 CSV」下載判讀結果
5. 回頭把本輪的整體結論填進 `runs/ROUNDS.md` 的「定位判定」與「值正確率」兩欄（程式不會自動填寫這兩欄）

## 5. Gemma 視覺支援探測結果（`scripts/check_vision.py`）

```
probing gemma-4-31b-it ...
probing gemma-4-26b-a4b-it ...
probing gemma-4-12b-it ...
probing gemma-3-taide-12b-chat-2602 ...
probing llama32-ffm-11b-v-32k ...
probing phi-4-reasoning-vision-15b-32k ...

model                            http       correct  raw_response
-----------------------------------------------------------------
gemma-4-31b-it                   200        True     {
  "text": "測試 A1"
}
gemma-4-26b-a4b-it               200        True     {
  "text": "測試 A1"
}
gemma-4-12b-it                   200        True     {
  "text": "測試 A1"
}
gemma-3-taide-12b-chat-2602      FAIL(5xx: Error code: 500 - {'error': {'message': "gemma-3-taide-12b-chat-2602: Error code: 500 - {'error': {'message': 'Expected there to be 1 prompt placeholders corresponding to 1 image items, but instead found 0 prompt placeholders! Make sure the implementation of `_call_hf_processor` and `_get_mm_fields_config` are consistent with each other.', 'type': 'InternalServerError', 'param': None, 'code': 500}}", 'type': 'InternalServerError', 'code': '500'}}) False    
llama32-ffm-11b-v-32k            FAIL(json_parse_failed) False    "測試 A1"
phi-4-reasoning-vision-15b-32k   200        True     {


    "測試 A1"

  :


    "測試 A1"

  }
```

排錯提示：AFS 官方文件的 vision 範例寫的是 `llama32-ffm-11b-v-32k-chat`（多 `-chat`）。平台清單以不含 `-chat` 為準；若遇到「模型不存在」的錯誤，可試加此後綴。

**2026-08-10 後續驗證：** 上表只證實 Gemma 系列「能吃圖片輸入、答得出圖裡的字」，沒測試定位階段真正需要的 bbox grounding 能力。實際拿 `gemma-4-31b-it` 跑 `stage1_locate` 對照真實表單，紅框確實貼合姓名/手機/e-mail/日期等 14 個欄位的實際位置；同一份文件用 `llama32-ffm-11b-v-32k` 跑則只會輸出 prompt 範例 bbox 加等差遞增的假座標（完全未讀圖）。已將 `gemma-4-31b-it` 移入 `pipeline/models.py` 的 `VISION_CONFIRMED`，並設為 `.env` 的 `AFS_MODEL` 預設值。

## 6. 用量查詢

- Gateway Dashboard：`http://<gateway-host>/_gateway/login`（見 [LLM Gateway](https://github.com/cloudysys-oss/llm-gateway)）
- 本專案的 X-Task-Label 格式：`{prefix}-{round}-{stage}`（例：`imagescan-20260807-1432-locate`）
- Dashboard 的「By Label」分頁依此格式分類；「By Model」分頁另外依 model 分類，兩者不重疊

## 7. 測試輪次操作慣例

- `--round` 預設為執行當下時間 `YYYYMMDD-HHMM`（精確到分鐘,整個 run 只算一次)
- 批次測試多份文件時,務必在 shell 固定同一個 round,否則會被 Dashboard 切成多組標籤:

```bash
ROUND=$(date +%Y%m%d-%H%M)
for f in samples/*.pdf; do
  python -m pipeline.run --input "$f" --round "$ROUND"
done
```

- 每次執行都帶 `--round-note` 說明這輪改了什麼
- 查閱時對照 `runs/ROUNDS.md`（改了什麼)與 Gateway Dashboard 的 By Label（花了多少)

## 8. 平台限制摘要

- 單張圖片最多 4 個 560×560 計價單位(6404 tokens 上限)
- 整頁圖片實際只被模型看到約 1120px 長邊,相當於 A4 在 ~95–135 DPI 的水準
- Rate limit / 併發上限未公開,本專案一律序列執行並實作 429 退避

## 9. 已知限制與待確認事項

- ~~確定支援視覺的模型偏小（11B / 15B),定位階段的 bounding box 精度可能不佳~~ → **2026-08-10 已解決**：`llama32-ffm-11b-v-32k` 的 bbox 經比對證實是照抄 prompt 範例後等差遞增的假座標，並非真正框選圖片內容。已改用 `gemma-4-31b-it`（31B，通過 grounding 實測，見第 5 節）作為預設模型。Task 8 提到的 JSON 截斷問題僅針對 llama32，供歷史對照。
- `response_format` 是否真的受支援因模型而異,實際結果記錄在對應 run 的 `meta.json.notes`
- Gemma 系列的視覺支援結果見第 5 節；`gemma-4-31b-it` 已確認可用並設為預設，其餘 Gemma 模型（26B-A4B / 12B / TAIDE-12B）的定位品質仍未實測
- Task 10 驗證發現預設模型在值提取階段會重複輸出 prompt 模板的 few-shot 示例值（"王小明"）而非實際讀取裁切圖，導致 5/7 欄位錯誤。此為模型品質問題，建議後續測試用視覺能力更強的模型。
- Task 11 whole-page 對照組測試顯示單一整頁識別模式在樣本頁面兩個測試模型都返回 0 個欄位（預設模型因 JSON 截斷失敗，phi-4 則返回空 `{}`），相較於三階段流程取得欄位的能力，是本 POC 的一個關鍵研究課題（見 project-purpose.md §11）。

## 授權

[MIT License](LICENSE)
