# Image Scan POC — 實作規格書

> 本文件為交付給 coding agent 的實作規格。請**完整依照本文件實作**,不要自行更動目錄結構、檔名格式與 JSON 欄位名稱。
> 遇到規格未涵蓋的情況,採取「保留原始資料、記錄錯誤、繼續執行」的策略,不要中止整個流程。

---

## 0. 平台資訊

本專案**不直接呼叫 AFS**,而是透過公司內部的 **LLM Gateway** 代理,以便集中記錄 token 用量。

### 連線設定

| 項目 | 值 |
|---|---|
| Gateway Base URL | `http://localhost:8080` |
| API Key | `sk-proj-...`(見 `.env`,**不要寫進程式碼**) |
| 後端 | AFS ModelSpace(由 Gateway 內部轉發,程式端不需知道 AFS 位址) |

Gateway 是透明代理:路徑、`model`、`messages` 等 body 參數與直接呼叫 AFS 完全相同,只換 Base URL 與 Key。

```python
client = OpenAI(api_key=GATEWAY_API_KEY, base_url="http://localhost:8080")
# SDK 會自行組出 http://localhost:8080/chat/completions
```

> **與先前版本的差異:`base_url` 不再帶任何路徑後綴。**
> 直連 AFS 時 base_url 結尾是 `/api/models`,改走 Gateway 後結尾就是 port,後面什麼都不加。這是最容易寫錯的一行。

### 關於路徑:不要加 `/models`(已確認)

AFS 官方文件的路徑是 `.../api/models/chat/completions`,比 Gateway 指南的範例多一層 `/models`。**Gateway 端已確認採用不含 `/models` 的形式**,`base_url` 就是 `http://localhost:8080`,SDK 會組出 `http://localhost:8080/chat/completions`。

`/models` 那層由 Gateway 內部轉發時處理,程式端不需要也不可以自己補。若看到 404,第一個要檢查的就是 `base_url` 是不是被多加了路徑。

### 用量標籤(X-Task-Label)

Gateway 支援 `X-Task-Label` header 做細分統計,Dashboard 的 **By Label** 分頁會依此分類。本 POC 會反覆調整 prompt 與參數重跑,因此標籤必須同時能區分**哪一輪測試**與**哪一個階段**。

**標籤格式(不可更改):**

```
{prefix}-{round}-{stage}
```

| 片段 | 來源 | 範例 |
|---|---|---|
| `prefix` | `.env` 的 `GATEWAY_LABEL_PREFIX` | `imagescan` |
| `round` | 程式啟動時自動產生的日期時間,格式 `YYYYMMDD-HHMM` | `20260807-1432` |
| `stage` | 程式內固定 | `probe` / `locate` / `extract` / `wholepage` |

實際送出的樣子:

```
imagescan-20260807-1432-locate
imagescan-20260807-1432-extract
```

**`round` 的規則**

- 預設值:程式啟動當下的本地時間,`datetime.now().strftime("%Y%m%d-%H%M")`
- **精確到分鐘,不含秒**。秒級會讓同一批測試被切成好幾個標籤
- 整個 run 只在啟動時計算**一次**,存成變數後全程沿用。**不要在每個階段各自呼叫 `now()`** —— 跨越整點分鐘時定位與辨識會落到不同標籤,那是最難察覺的一種錯
- 可用 `--round` 覆寫成任意自訂字串(例如 `baseline`、`gemma-test`),字元集 `[a-z0-9-]`,上限 24 字元;不符則**拒絕執行**並印出明確錯誤,不要自行清洗

**同一批測試要共用一個 round**

自動時間戳的代價是:一輪測試若跑五份文件,會產生五個不同標籤,Dashboard 上就散開了。批次執行時請在 shell 固定 round:

```bash
ROUND=$(date +%Y%m%d-%H%M)
for f in samples/*.pdf; do
  python -m pipeline.run --input "$f" --round "$ROUND"
done
```

**此段請一字不漏寫進 README。** 這是自動時間戳唯一的操作陷阱,不寫清楚使用者一定會踩。

> **不要把模型名稱放進標籤。** Dashboard 已有獨立的 **By Model** 分頁做這件事。標籤只負責 Dashboard 切不出來的維度(輪次、階段),兩者不要重疊。

**標籤一律使用 ASCII。** 指南範例雖然用中文,但 HTTP header 值在多數 client(含 httpx/OpenAI SDK)預設以 latin-1 編碼,傳中文會拋 `UnicodeEncodeError` 或被轉成亂碼。上述字元集限制已涵蓋此問題,但 `call_vision()` 送出前仍需再 assert 一次。

### Gateway 特有的錯誤碼(**不可重試**)

| 狀態碼 | 意義 | 處理方式 |
|---|---|---|
| `401` | API Key 已被撤銷 | **立即中止整個執行**,印出明確訊息要求重新產生 Key |
| `403` | Project 已被停用 | **立即中止整個執行**,印出訊息要求至 Dashboard 重新啟用 |

這兩種錯誤重試沒有意義。若不特別處理,會變成每個欄位都重試 3 次、等待 14 秒,一份 24 欄位的表單要空轉 5 分鐘才結束。

### 可用模型(已確認,清單固定)

以下九個為平台全部可用模型。**`model` 參數一律傳「個體名稱」欄的字串。**

| 模型名稱 | 個體名稱(傳這個) | 視覺 |
|---|---|---|
| Gemma-4-31B-it | `gemma-4-31b-it` | 待確認 |
| Gemma-4-26B-A4B-it | `gemma-4-26b-a4b-it` | 待確認 |
| Gemma-3-TAIDE-12b-Chat-2602 | `gemma-3-taide-12b-chat-2602` | 待確認 |
| Gemma-4-12B-it | `gemma-4-12b-it` | 待確認 |
| Phi-4-Reasoning-Vision-15B-32K | `phi-4-reasoning-vision-15b-32k` | ✅ |
| gpt-oss-120b-32K | `gpt-oss-120b-32k` | ❌ 純文字 |
| Llama3.2-FFM-11B-V-32K | `llama32-ffm-11b-v-32k` | ✅ |
| Llama3.3-FFM-70B-32K | `llama-33-ffm-70b` | ❌ 純文字 |
| gpt-oss-20b-32K | `gpt-oss-20b-32k` | ❌ 純文字 |

**實作方式:把上表寫成 `pipeline/models.py` 裡的常數,程式一律從常數取值。**

```python
# pipeline/models.py
VISION_CONFIRMED = ["llama32-ffm-11b-v-32k", "phi-4-reasoning-vision-15b-32k"]
VISION_UNKNOWN   = ["gemma-4-31b-it", "gemma-4-26b-a4b-it",
                    "gemma-4-12b-it", "gemma-3-taide-12b-chat-2602"]
TEXT_ONLY        = ["gpt-oss-120b-32k", "gpt-oss-20b-32k", "llama-33-ffm-70b"]
```

> **個體名稱的命名規則不一致,必須逐字照抄,不可自行推導或正規化。**
> 對照兩列就知道為什麼:`Llama3.2-FFM-11B-V-32K` → `llama32-ffm-11b-v-32k`(`3.2` 併成 `32`、保留 `-32k`),但 `Llama3.3-FFM-70B-32K` → `llama-33-ffm-70b`(`3.3` 拆成 `-33`、且 **`-32k` 被拿掉了**)。任何「統一格式」的聰明做法都會產生不存在的模型名稱。

**不需要呼叫 `client.models.list()`,也不需要探測模型名稱是否存在。** 上表即為定案。

### 視覺能力的唯一未知數:Gemma 系列

Gemma 3 本身是多模態架構,但:
- **Gemma 4 系列超出本規格撰寫者的可靠知識範圍**,無法斷言其視覺能力
- `gemma-3-taide-12b-chat-2602` 是 TAIDE 的中文微調版,微調後是否保留視覺輸入需實測

這件事值得花十分鐘確認,因為**它可能直接改變 POC 的結論**:目前確定可用的兩個視覺模型只有 11B 與 15B,而 `gemma-4-31b-it` 大了一倍以上。若它支援圖片輸入,在欄位定位這種對空間推理要求高的任務上,表現很可能明顯較好。

`scripts/check_models.py` 因此縮減為單一用途:

> 以 Pillow 現場產生一張 600×200 的 PNG(白底,畫上「測試 A1」字樣),對 `VISION_UNKNOWN` 的四個模型各送一次 `image_url` 請求,問「圖片中的文字是什麼」。記錄各自回 200 還是 4xx、以及是否答對。同時對 `VISION_CONFIRMED` 的兩個各送一次作為對照組。
>
> 全部請求帶 `X-Task-Label: {prefix}-{round}-probe`。輸出以表格印出並貼進 README。

### 圖片規格(來自 AFS API 文件)

- 支援格式:**PNG、JPG/JPEG、WebP、GIF(靜態)**
- 傳入方式:圖片 URL 或 base64 data URL。**本專案一律使用 base64**,格式 `data:image/png;base64,{b64}`
- 訊息結構:`content` 為陣列,含 `{"type": "image_url", "image_url": {"url": ...}}` 與 `{"type": "text", "text": ...}`

### 圖片尺寸上限(**重要,直接影響本 POC 設計,見第 2 節**)

平台以 560×560 為一個計價單位(1601 tokens),**單張圖片最多 4 個單位**。超過的圖片會由平台自動等比例縮放至符合下列其中一種標準尺寸:

| 尺寸 | 單位數 | Tokens |
|---|---|---|
| 560×560 | 1 | 1601 |
| 560×1120 / 1120×560 | 2 | 3202 |
| 560×1680 / 1680×560 | 3 | 4803 |
| 560×2240 / 2240×560 / 1120×1120 | 4 | 6404 |

### 其他

| 項目 | 狀況 |
|---|---|
| `response_format` | 文件參數表列為支援 JSON 模式,但**無任何範例或細節**。實作需採「先嘗試、失敗則退回」策略,見第 8 節 |
| `enable_grammar` | AFS 自訂參數,`true` 可確保 **tool calling** 回傳符合 JSON Schema。僅搭配 tool calling 有效 |
| Rate limit / 併發上限 | **AFS 與 Gateway 兩份文件皆未記載**。故本專案一律序列執行,並實作 429 退避 |
| Token 用量統計 | Gateway 自動記錄,**Dashboard 為權威來源**。程式端仍記錄一份供對帳 |
| `max_tokens` | 範圍 1–32768 |
| `temperature` | 範圍 0.0–2.0。AFS vision 範例使用 `temperature=0.1, top_p=0.5` |
| Response 額外欄位 | 回應含 `total_time_taken`,請一併記錄 |

---

## 1. 目標與範圍

### 目標

驗證「以 LLM 解析 PDF/圖片表單並自動填入欄位值」的**技術可行性**。

正確率由**人工判讀**認定,程式端不需實作自動評分,但**必須產出便於人工判讀的產物**(見第 6.4 節)。

### 本次要回答的問題

1. LLM 能否正確定位表單欄位位置(框選精度是否足夠用於裁切)
2. 裁切後的小圖,LLM 能否正確辨識填入值
3. LLM 自報的信心值,是否與實際正確率相關

### 已知的主要風險(實作時請留意,並在報表中如實呈現)

1. **確定可用的視覺模型偏小(11B / 15B)。** 這類規模的模型輸出精確 bounding box 座標的能力普遍不佳,尤其在密集的中文表單上。定位階段結果不理想是**可預期的**,這正是本 POC 要驗證的事情之一。請如實記錄,不要為了讓結果好看而在程式中做額外修正。
   若 `check_vision.py` 證實 Gemma-4-31B 支援圖片輸入,應優先改用它重跑一輪 —— 參數量差距可能直接改變定位階段的結論。

2. **整頁圖片會被平台強制降解析度。** 見下節。

### 不做的事(Non-goals)

- 不做自動化正確率評分
- 不做前端 UI(僅產出靜態 HTML 報表)
- 不做資料庫、不做使用者管理、不做部署
- 不做效能最佳化、不做非同步併發(除非遇到 rate limit 才加簡單節流)
- 不做手寫辨識的特別處理(但需在報表中如實呈現結果)

---

## 2. 解析度策略(**請先讀完再寫程式**)

因為平台將單張圖片上限定為 4 個 560×560 單位,一張 A4 直式頁面(寬高比約 0.707)送進去後,實際被模型看到的尺寸最多約 **1120px 長邊**,相當於 A4 在 **約 95–135 DPI** 的水準。用 300 DPI 掃描再整頁丟上去,多出來的畫素在傳輸途中就被平台丟掉了。

這導出三個設計決定:

**(1) 來源仍用高 DPI(預設 300),但不要把原尺寸整頁直接送 API。**
高 DPI 對「裁切」階段有實質幫助 —— 裁切出來的小圖是獨立一張圖,會被平台**放大**到 560 高度的單位,此時原始畫素愈多、字愈清楚。但對整頁而言高 DPI 完全浪費。

**(2) 定位階段送出前,由我們自己先降尺寸。**
把整頁縮到長邊 1120px(以 `Image.LANCZOS` 重採樣)再送出。理由:自己用 Lanczos 縮圖的品質通常優於平台端的自動縮放,且可控、可重現、可被人工檢視。縮圖後另存一份,不要覆蓋原圖。

**(3) 這也正是三階段流程存在的理由。**
若整頁單次辨識,所有欄位都只能在 ~1120px 的頁面上讀;而三階段流程下,每個欄位都獨享一張圖的解析度預算。請把這點寫進 README,它是本架構的核心論據,也是第 11 節對照組要驗證的假設。

> **座標不受影響**:因為第 6.1 節規定使用正規化 0–1000 座標,定位在縮圖上進行、裁切在原圖上進行,兩者可直接換算,不需額外處理縮放比例。這是選用正規化座標而非像素座標的原因。

---

## 3. 技術規格

| 項目 | 指定值 |
|---|---|
| 語言 | Python 3.11+ |
| API SDK | `openai`(指向 LLM Gateway 的 OpenAI-compatible endpoint) |
| PDF 轉圖 | `PyMuPDF` (`pymupdf`) — **不要用 pdf2image**,避免 poppler 系統相依 |
| 影像處理 | `Pillow` |
| 設定管理 | `python-dotenv` |
| HTML 產生 | 直接以 Python f-string 組字串即可,不需模板引擎 |

`requirements.txt`:

```
openai>=1.0
pymupdf>=1.24
pillow>=10.0
python-dotenv>=1.0
```

### 環境變數(`.env`)

```
# LLM Gateway(代理至 AFS ModelSpace)
# 結尾不加任何路徑,不要補 /models
GATEWAY_BASE_URL=http://localhost:8080
GATEWAY_API_KEY=
GATEWAY_LABEL_PREFIX=imagescan

# 模型名稱,須為 pipeline/models.py 常數表中的「個體名稱」,逐字照抄
AFS_MODEL=llama32-ffm-11b-v-32k
```

`.env.example` 保留上述內容但 `GATEWAY_API_KEY` 留空。

`.gitignore` 需包含:

```
.env
runs/*
!runs/ROUNDS.md
```

`runs/` 內容不進版控,但 `ROUNDS.md` 是實驗記錄,必須納入。

> **API Key 只放在 `.env`,不得寫入任何 `.py`、`README.md` 或本規格文件。** 提交前請確認 `git grep "sk-proj-"` 無結果。

---

## 4. 專案結構

```
image-scan-poc/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── scripts/
│   └── check_vision.py      # 探測 Gemma 系列是否支援圖片輸入(第一支要寫的程式)
├── prompts/
│   ├── locate_v1.txt        # 欄位定位 prompt
│   └── extract_v1.txt       # 值辨識 prompt
├── pipeline/
│   ├── __init__.py
│   ├── models.py            # 可用模型常數表(逐字照抄,不可推導)
│   ├── run.py               # CLI 進入點
│   ├── config.py            # 環境變數載入
│   ├── llm.py               # API 呼叫封裝、重試、JSON 解析
│   ├── stage0_input.py      # 前處理
│   ├── stage1_locate.py     # 欄位定位
│   ├── stage2_crop.py       # 裁切
│   ├── stage3_extract.py    # 值辨識
│   ├── stage4_report.py     # 彙總報表
│   └── utils.py             # 檔名處理、目錄建立等
└── runs/                    # 執行結果輸出(加入 .gitignore,但 ROUNDS.md 例外,需納入版控)
    └── ROUNDS.md            # 輪次總表,每次執行自動追加一列
```

---

## 5. CLI 介面

```bash
python -m pipeline.run --input <PDF或圖片路徑> [options]
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--input` | (必填) | 輸入檔路徑,支援 `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp` |
| `--round` | 當下時間 `YYYYMMDD-HHMM` | 測試輪次識別,進入 `X-Task-Label`。批次執行時請明確指定以共用同一 round。字元集 `[a-z0-9-]`,上限 24 字元 |
| `--round-note` | `""` | 本輪改了什麼的自由文字,記入 `meta.json` 與 `ROUNDS.md`。**不會**進入 header |
| `--dpi` | `300` | PDF 轉圖解析度。高 DPI 是為了裁切品質,不是為了定位(見第 2 節) |
| `--locate-max-edge` | `1120` | 送定位 API 前,整頁縮圖的長邊像素上限 |
| `--pages` | `all` | 頁碼範圍,如 `1`, `1-3`, `1,3,5` |
| `--model` | 取自 `AFS_MODEL` | 覆寫使用的模型。啟動時需驗證其存在於 `models.py` 常數表,不存在則拒絕執行並列出可用清單 |
| `--crop-padding` | `0.01` | 裁切時 bbox 四周外擴比例(相對於頁面尺寸) |
| `--crop-min-edge` | `560` | 裁切圖短邊若小於此值,以 Lanczos 等比放大至此值後再送 API |
| `--out` | `runs/` | 輸出根目錄 |

執行過程需在 stdout 輸出簡明進度,例如:

```
[--] round=20260807-1432 (auto)  label=imagescan-20260807-1432-*  model=llama32-ffm-11b-v-32k
[00] 前處理 ... 3 頁,DPI 300,定位縮圖 792x1120
[01] 欄位定位 ... page 1/3
[01] 欄位定位 ... 共取得 24 個欄位(2 個座標無效)
[02] 裁切 ... 22 張
[03] 值辨識 ... 22/22(失敗 1)
[04] 報表 ... runs/20260807-1432_143210_form_a/report.html
```

**第一行必須印出本次使用的 round,並標明是 `(auto)` 還是 `(specified)`。** 批次跑到一半才發現每份文件各自一個 round,重跑的成本比多印一行高得多。

---

## 6. 各階段規格

輸出根目錄:`runs/{round}_{HHMMSS}_{input檔名去副檔名並淨化}/`
例:`runs/20260807-1432_143210_form_a/`。round 置於最前,同一輪的執行結果在檔案總管中會自然排在一起。
以下路徑皆相對於此目錄。

### 6.0 前處理(stage0_input)

**行為**

1. 若輸入為 PDF,以 PyMuPDF 依指定 DPI 逐頁轉為 PNG;若輸入為圖片,直接複製為單頁。
2. **產生定位用縮圖**:將每頁等比縮至長邊 `--locate-max-edge`(預設 1120),以 `Image.LANCZOS` 重採樣,另存為 `page_00N_locate.png`。**不要覆蓋原圖。** 若原圖長邊已小於該值則不放大,直接複製。
3. **額外檢查**:若為 PDF,以 `page.get_text()` 檢查該頁是否含文字層,將結果記錄下來(此資訊供後續評估參考,**但流程仍一律走視覺辨識**,不要因為有文字層就跳過 LLM)。

**產出**

```
00_input/
├── page_001.png            # 原始解析度,供裁切使用
├── page_001_locate.png     # 縮圖,供定位 API 使用
├── page_002.png
├── page_002_locate.png
└── input_meta.json
```

`input_meta.json`:

```json
{
  "source_path": "/abs/path/form_a.pdf",
  "source_type": "pdf",
  "dpi": 300,
  "locate_max_edge": 1120,
  "page_count": 3,
  "pages": [
    {
      "page": 1,
      "file": "page_001.png",
      "width": 2480,
      "height": 3508,
      "locate_file": "page_001_locate.png",
      "locate_width": 792,
      "locate_height": 1120,
      "has_text_layer": true,
      "text_layer_char_count": 812
    }
  ]
}
```

---

### 6.1 欄位定位(stage1_locate)

**行為**

逐頁將 **`page_00N_locate.png`(縮圖,非原圖)** 送給 LLM,要求回傳欄位清單與座標。
`max_tokens` 設為 `4096`(欄位多時 JSON 會很長,設太小會被截斷成無效 JSON)。

**座標系統(重要,不可更改)**

- 使用**正規化整數座標**,範圍 `0`–`1000`
- 原點在**左上角**,x 向右遞增,y 向下遞增
- 格式:`bbox: [x1, y1, x2, y2]`,`x1 < x2`、`y1 < y2`
- 轉回像素時,**一律以原圖尺寸換算**(不是縮圖尺寸):
  `px = round(x / 1000 * page_width)`,`py = round(y / 1000 * page_height)`

> 因座標已正規化,在縮圖上定位、在原圖上裁切是安全的,不需要任何縮放補償計算。

**必要的防呆處理**(LLM 座標常出錯,以下務必實作):

1. 將所有座標 clamp 至 `[0, 1000]`
2. 若 `x1 >= x2` 或 `y1 >= y2`,自動交換;交換後仍無效則標記該欄位 `"invalid_bbox": true` 並跳過裁切
3. 若 bbox 面積小於頁面的 0.01%,標記 `"invalid_bbox": true`
4. **不要因為單一欄位座標錯誤就中止整頁處理**

**Prompt**(存為 `prompts/locate_v1.txt`)

```
你是一個表單版面分析工具。請分析這張表單圖片,找出所有「需要填寫的欄位」。

規則:
1. 只回報實際的填寫欄位(有標籤且有對應填寫區域者)。純標題、說明文字、頁碼、表格框線不算欄位。
2. bbox 請框出「填寫值所在的區域」,盡量不要包含欄位標籤文字本身。
3. 座標使用正規化整數,範圍 0 到 1000,原點在圖片左上角。格式為 [x1, y1, x2, y2],x1<x2 且 y1<y2。
4. field_name 請使用該欄位在表單上的標籤原文。若同一標籤出現多次,在後面加上序號,例如「金額_2」。
5. 欄位順序請由上到下、由左到右排列。

僅輸出 JSON,不要輸出任何說明文字或 markdown 標記:

{
  "fields": [
    {
      "field_name": "申請人姓名",
      "label_text": "申請人姓名",
      "bbox": [320, 145, 720, 180]
    }
  ]
}
```

**產出**

```
01_layout/
├── page_001_response.txt    # LLM 原始回應,完整保留未經處理
├── page_001_fields.json     # 解析與防呆後的結果
├── page_001_preview.png     # 標註圖(見下)
└── ...
```

**`page_00N_fields.json`**

```json
{
  "page": 1,
  "image_width": 1654,
  "image_height": 2339,
  "fields": [
    {
      "field_id": "p001_f001",
      "field_name": "申請人姓名",
      "label_text": "申請人姓名",
      "bbox_norm": [320, 145, 720, 180],
      "bbox_px": [529, 339, 1191, 421],
      "invalid_bbox": false
    }
  ],
  "error": null
}
```

**標註圖 `page_00N_preview.png`(本階段最重要的產物)**

在原頁面圖上繪製:

- 每個有效 bbox 的紅色外框,線寬 3px
- 框的左上角外側標註 `{序號}. {field_name}`,字級需清晰可讀(建議 20px 以上),文字加白色底色避免與內容重疊看不清
- `invalid_bbox` 為 true 者改用灰色虛線框(若座標仍可繪製)

> 此圖用於人工判斷「定位是否正確」。若最終值辨識錯誤,需靠這張圖分辨是框歪了還是讀錯了。請務必確保標註清晰。
>
> 中文字型:PIL 預設字型不支援中文。請以 `PIL.ImageFont.truetype()` 依序嘗試常見路徑(`/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc`、`/System/Library/Fonts/PingFang.ttc`、`C:\Windows\Fonts\msjh.ttc`),全部失敗時退回預設字型並在 console 印出警告。

---

### 6.2 裁切(stage2_crop)

**行為**

依 `bbox_px` 裁切原頁面圖,四周外擴 `--crop-padding`(相對頁面寬高的比例,預設 1%),外擴後 clamp 至圖片邊界。

> 外擴的原因:LLM 的框普遍偏緊,容易切掉字的邊緣,導致辨識失敗但無從得知原因。

**放大處理(平台特性所需)**

裁切完成後,若圖片短邊小於 `--crop-min-edge`(預設 560),以 `Image.LANCZOS` 等比放大至短邊等於該值,另存為 `_up.png`;送 API 時使用放大版,報表縮圖仍顯示原裁切版。

> 原因:平台會把每張圖對齊到 560×560 的倍數單位並自動縮放。一條 694×110 的欄位圖在平台端會被放大處理,與其交給平台端縮放,不如我們自己用 Lanczos 放大,品質較可控。放大不會增加 token 費用 —— 計價依對齊後的單位數,一張細長圖無論如何都落在同一個單位級距。

**檔名格式**

`{page:03d}_{idx:03d}_{slug}.png`

`slug` 產生規則:取 `field_name`,移除 `/ \ : * ? " < > |` 與空白字元,長度上限 20 字元。**中文字元保留**。若 slug 為空則以 `field` 代替。

**產出**

```
02_crops/
├── 001_001_申請人姓名.png
├── 001_002_身分證字號.png
└── index.json
```

`index.json`:

```json
{
  "crops": [
    {
      "field_id": "p001_f001",
      "field_name": "申請人姓名",
      "label_text": "申請人姓名",
      "page": 1,
      "file": "001_001_申請人姓名.png",
      "bbox_px_padded": [770, 488, 1810, 652],
      "width": 1040,
      "height": 164,
      "upscaled_file": "001_001_申請人姓名_up.png",
      "upscaled_size": [3551, 560]
    }
  ],
  "skipped": [
    { "field_id": "p001_f009", "reason": "invalid_bbox" }
  ]
}
```

---

### 6.3 值辨識(stage3_extract)

**行為**

逐張裁切圖(有 `_up.png` 則用放大版)送 LLM,取得填入值與信心值。
**序列執行,不要併發** —— AFS 文件未記載 rate limit 與併發上限,併發可能觸發未知的限流行為。
`max_tokens` 設為 `512`。

**Prompt**(存為 `prompts/extract_v1.txt`,`{field_name}` 與 `{label_text}` 為代入變數)

```
這是一張從表單中裁切出來的欄位圖片。

欄位名稱:{field_name}
欄位標籤:{label_text}

請辨識這個欄位「被填入的值」。

規則:
1. 只回報填入的內容。若圖片中包含欄位標籤文字,不要把標籤當成填入值。
2. 若該欄位是空白的(沒有填任何內容),value 請回傳空字串,並將 is_empty 設為 true。**絕對不要猜測或杜撰內容。**
3. 若圖片模糊、被印章遮擋或無法判讀,value 請盡力填入你能看到的部分,並降低 confidence。
4. confidence 為 0.0 到 1.0 的數字,反映你對這個辨識結果的把握程度。請誠實反映,不要一律給高分。
5. notes 用於記錄異常狀況(如「被印章遮擋」「疑似手寫」「圖片模糊」),無異常則留空字串。

僅輸出 JSON,不要輸出任何說明文字或 markdown 標記:

{
  "value": "王小明",
  "is_empty": false,
  "confidence": 0.95,
  "notes": ""
}
```

**產出**

```
03_extract/
├── raw/
│   ├── p001_f001_response.txt   # 每個欄位的 LLM 原始回應
│   └── ...
└── result.json
```

`result.json`:

```json
{
  "results": [
    {
      "field_id": "p001_f001",
      "field_name": "申請人姓名",
      "page": 1,
      "crop_file": "001_001_申請人姓名.png",
      "value": "王小明",
      "is_empty": false,
      "confidence": 0.95,
      "notes": "",
      "error": null,
      "attempts": 1,
      "latency_ms": 1840
    }
  ]
}
```

---

### 6.4 彙總報表(stage4_report)

產出 `report.html`,**單一自包含檔案**(圖片以 base64 內嵌,不依賴外部檔案,可單獨寄送)。

**版面規格**

1. **標題區**:輸入檔名、執行時間、模型名稱、總欄位數、辨識失敗數、平均信心值

2. **定位檢查區**:依序顯示每頁的 `page_00N_preview.png`,寬度佔滿版面,上方標題「定位結果 — 第 N 頁」。
   每頁下方放一組人工勾選:`定位正確 / 部分錯誤 / 大量錯誤`(radio button)

3. **欄位判讀表**:一列一欄位,欄位如下

   | 欄 | 內容 |
   |---|---|
   | # | 序號 |
   | 頁 | 頁碼 |
   | 欄位名稱 | field_name |
   | 裁切圖 | 內嵌縮圖,高度約 60px,**點擊可放大顯示原尺寸** |
   | 辨識值 | value(若 is_empty 則顯示灰色「(空白)」) |
   | 信心值 | confidence,依數值上色:≥0.9 綠、0.7–0.9 黃、<0.7 紅 |
   | 備註 | notes / error |
   | **人工判定** | 下拉選單:`未判定`(預設) / `正確` / `錯誤` / `部分正確` |
   | **人工備註** | 可輸入文字框 |

4. **底部**:「匯出 CSV」按鈕。以 JavaScript 讀取表格目前狀態(含人工判定與備註)產生 CSV 並下載,檔名 `judgement_{執行目錄名}.csv`。CSV 需加上 UTF-8 BOM,否則 Excel 開啟中文會亂碼。

**排序**:預設依頁碼、欄位順序。提供「依信心值排序」的切換按鈕(純前端 JS)。

---

## 7. 執行記錄

輸出目錄根層產出 `meta.json`:

```json
{
  "run_id": "20260807-1432_143210_form_a",
  "round": "20260807-1432",
  "round_source": "auto",
  "round_note": "定位 prompt 改為要求排除標籤文字",
  "started_at": "2026-08-07T14:32:10+08:00",
  "finished_at": "2026-08-07T14:38:52+08:00",
  "input": { "path": "...", "type": "pdf", "page_count": 3 },
  "config": { "dpi": 300, "locate_max_edge": 1120, "crop_padding": 0.01, "model": "..." },
  "gateway": {
    "base_url": "http://localhost:8080",
    "label_prefix": "imagescan",
    "labels_used": ["imagescan-20260807-1432-locate", "imagescan-20260807-1432-extract"]
  },
  "prompts": {
    "locate": { "file": "prompts/locate_v1.txt", "sha256": "..." },
    "extract": { "file": "prompts/extract_v1.txt", "sha256": "..." }
  },
  "stages": {
    "locate":  { "label": "imagescan-20260807-1432-locate",  "duration_ms": 24100,  "api_calls": 3,  "failures": 0, "total_tokens": 21500 },
    "extract": { "label": "imagescan-20260807-1432-extract", "duration_ms": 380400, "api_calls": 26, "failures": 2, "total_tokens": 48200 }
  },
  "token_usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
  "errors": []
}
```

> `prompts` 區塊記錄 prompt 檔案的 sha256。POC 會反覆調整 prompt,若不記錄版本,幾輪之後無法對應哪個 prompt 產生哪份結果。
>
> `token_usage` 從 API 回應的 `usage` 欄位累加;若未回傳則填 0 並在 `errors` 註明。
>
> **各階段 token 分開統計。** 這組數字要能回答「定位貴還是辨識貴」,以及第 11 節對照組的成本比較。同樣的分類在 Gateway Dashboard 的 By Label 分頁也看得到,兩邊應該對得起來。

---

### 輪次總表(`runs/ROUNDS.md`)

**每次執行結束後,自動在 `runs/ROUNDS.md` 追加一列**(檔案不存在則先建立含表頭)。這是把 Dashboard 上的數字對回「當時改了什麼」的唯一途徑,不能省略。

| round | run_id | 時間 | 輸入檔 | 模型 | locate prompt | extract prompt | 欄位數 | 失敗 | total tokens | 定位判定 | 值正確率 | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20260807-1432 | 20260807-1432_143210_form_a | 08-07 14:32 | form_a.pdf | llama32-ffm-11b-v-32k | `a3f1c2` | `9b04de` | 24 | 2 | 69700 | | | 定位 prompt 改為要求排除標籤文字 |

- prompt 欄位填 `meta.json` 中 sha256 的**前 6 碼**,一眼就能看出兩輪之間 prompt 有沒有換
- 以 append 方式寫入,**絕對不要重寫或排序既有內容**
- 同一 round 跑多份文件會有多列,這是預期行為
- **「定位判定」與「值正確率」兩欄由程式留白,人工判讀完後自行填入**(例如 `部分錯誤` 與 `18/24`)。程式不得寫入這兩欄,也不得因為它們是空的而報錯

> 這兩欄是整張表的重點。其餘欄位都是程式能自動記錄的過程資料,唯獨「這一輪到底準不準」只有人知道。若不留位置,判讀結果會散落在各個 run 的 CSV 裡,幾輪之後就無法橫向比較 —— 而橫向比較正是分 round 測試的目的。

> 這張表加上 Gateway Dashboard 的 By Label,就構成完整的實驗記錄:表格說明「這一輪改了什麼」,Dashboard 說明「這一輪花了多少」,兩邊用 round 字串對接。缺任何一邊,兩週後都會看不懂自己的數據。
>
> round 改用時間戳後,這張表的重要性更高了 —— `20260807-1432` 本身完全不帶語意,唯一能說明那一輪在做什麼的就是 `note` 欄。**請養成每次都帶 `--round-note` 的習慣。**

---

## 8. LLM 呼叫共用規格(pipeline/llm.py)

實作一個函式,所有階段共用:

```python
def resolve_round(cli_round: str | None) -> tuple[str, str]:
    """回傳 (round, source)。source 為 'auto' 或 'specified'。
    整個 run 只呼叫一次,結果存進 config 全程沿用。"""
    if cli_round:
        r, src = cli_round.lower(), "specified"
    else:
        r, src = datetime.now().strftime("%Y%m%d-%H%M"), "auto"
    if not re.fullmatch(r"[a-z0-9-]{1,24}", r):
        raise SystemExit(f"--round 只允許 [a-z0-9-] 且不超過 24 字元,收到: {cli_round!r}")
    return r, src


def build_label(prefix: str, round_tag: str, stage: str) -> str:
    """組出 X-Task-Label。stage 為 probe/locate/extract/wholepage。"""
    label = f"{prefix}-{round_tag}-{stage}"
    assert re.fullmatch(r"[a-z0-9-]{1,64}", label), f"非法標籤: {label}"
    return label


def call_vision(
    image_path: str,
    prompt: str,
    model: str,
    task_label: str,          # 必填,由 build_label() 產生
    max_tokens: int = 4096,
    max_retries: int = 3,
) -> tuple[dict | None, str, dict]:
    """
    回傳 (解析後的 dict 或 None, 原始回應文字, 中繼資訊)
    中繼資訊含 attempts, latency_ms, usage, total_time_taken, task_label, error
    """
```

`task_label` 不給預設值,強制每個呼叫端明確指定,避免用量統計混在一起。標籤一律經由 `build_label()` 產生,**不要在各階段用 f-string 各自拼字串** —— 拼錯一個字元就會在 Dashboard 上多出一個孤兒標籤,而且不會報錯。

### 請求格式

```python
from openai import OpenAI
import base64

client = OpenAI(api_key=GATEWAY_API_KEY, base_url=GATEWAY_BASE_URL)

with open(image_path, "rb") as f:
    b64 = base64.b64encode(f.read()).decode("utf-8")

messages = [{
    "role": "user",
    "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": prompt},
    ],
}]

resp = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.0,
    top_p=0.5,
    max_tokens=max_tokens,
    seed=42,
    extra_headers={"X-Task-Label": task_label},
)
```

**注意事項**

- `extra_headers` 是**每次請求**帶入,不要設成 client 層級的 `default_headers` —— 各階段標籤不同,設在 client 上就分不開了。
- `task_label` 進入 header 前由 `build_label()` 的 assert 把關。中文或其他非 ASCII 字元會在 httpx 的 header 編碼階段炸掉。
- `X-Task-Label` 只被 Gateway 記錄、不會轉發給 AFS,故不影響推論結果。
- `image_url` 物件在前、`text` 在後 —— 依 AFS 文件範例的順序。若辨識結果異常,可作為調整變因之一。
- `seed` 設為固定值以利重現。若平台回報不支援則移除,並在 README 註明。
- MIME type 需與實際檔案相符。本專案統一使用 PNG,故一律 `data:image/png;base64,`。
- **不使用 `stream`**。本專案無串流需求,非串流回應可直接取得 `usage`。

### `response_format` 的處理(必須實作退回機制)

AFS 文件參數表列出 `response_format` 支援 JSON 模式,但**未提供任何範例或格式細節,能否運作未知**。因此:

1. 首次呼叫時嘗試帶入 `response_format={"type": "json_object"}`
2. 若收到 4xx 錯誤且訊息與 `response_format` 相關,**記錄此事實至 `meta.json.notes`,並在該次執行的後續所有呼叫中永久移除此參數**(不要每次都重試一遍)
3. 無論是否支援,prompt 中「僅輸出 JSON」的指示都保留,且 JSON 解析容錯邏輯照樣實作

> `enable_grammar=true` 是 AFS 的自訂參數,可保證 JSON Schema 相符,但**僅在搭配 tool calling 時有效**,與本專案直接要求模型輸出 JSON 的做法不相容。若解析失敗率偏高(> 20%),可考慮改用 tool calling + `enable_grammar` 重做定位階段 —— 但此為 P2,先不要動。

### JSON 解析容錯(依序嘗試,不要只做第一項)

1. 直接 `json.loads()`
2. 移除 ` ```json ` / ` ``` ` 圍籬後再解析
3. 以正則抓出第一個 `{` 到最後一個 `}` 的區間再解析
4. 全部失敗 → 回傳 `None`,並將原始回應完整寫入對應的 `_response.txt`

> 補充:`phi-4-reasoning-vision` 這類推理型模型可能在 JSON 前輸出思考過程,或使用 `<think>` 之類的標籤。步驟 3 的區間擷取可涵蓋多數情況;若實測發現特定標籤,再於 README 記錄並增補處理。

### 重試與失敗

**先判斷是否為不可重試的錯誤:**

| 狀況 | 行為 |
|---|---|
| `401` | Key 已撤銷。**立即中止整個 run**,寫入 `meta.json.errors`,stdout 印出「Gateway API Key 已被撤銷,請至 Dashboard 重新產生」後 `sys.exit(1)` |
| `403` | Project 已停用。**立即中止整個 run**,印出「Gateway Project 已被停用,請至 Dashboard 重新啟用」後 `sys.exit(1)` |
| 連線被拒 / DNS 失敗 | Gateway 未啟動或位址錯誤。**立即中止**,印出目前使用的 `GATEWAY_BASE_URL` |

以上三種一律不重試、不繼續跑後續欄位。

**可重試的錯誤:**

- HTTP 429 / 5xx / 連線逾時 / JSON 解析失敗時重試,最多 3 次,間隔 2s / 4s / 8s
- 檢查 `finish_reason`:若為 `length`,表示輸出被 `max_tokens` 截斷。此時**不要重試同樣參數**,直接標記 `error: "truncated"` 並記錄,提高 `max_tokens` 是人為決定
- 重試耗盡後**不要拋出例外中止流程**。回傳 `None`,由呼叫端在該筆結果的 `error` 欄位記錄原因後繼續下一筆
- 每次呼叫後固定 `sleep(0.5)`。AFS 未公開 rate limit,保守處理

### 需記錄的回應欄位

`usage.prompt_tokens` / `usage.completion_tokens` / `usage.total_tokens`,以及 AFS 特有的 `total_time_taken`(以 `getattr` 或 `model_dump()` 取用,OpenAI SDK 的型別不含此欄位)。

> Gateway Dashboard 已自動記錄所有用量且永久保留,是成本分析的權威來源。程式端這份記錄的用途是**對帳**:若 `meta.json` 的統計與 Dashboard 差距明顯,表示有請求沒被正確計入或標籤貼錯,需要查明。

---

## 9. README.md 需包含的內容

1. 安裝步驟(建立 venv、`pip install -r requirements.txt`、複製 `.env.example` 為 `.env`)
2. 執行範例指令
3. 輸出目錄結構說明
4. **人工判讀流程說明**:開啟 `report.html` → 先看定位檢查區判斷框選是否正確 → 再逐列判讀欄位 → 匯出 CSV → **回頭把結論填進 `runs/ROUNDS.md` 的「定位判定」與「值正確率」兩欄**
5. **`scripts/check_vision.py` 的實際輸出**:Gemma 四個模型哪些接受圖片輸入、答對與否
   - 附帶一則排錯提示:AFS 官方文件的 vision 範例寫的是 `llama32-ffm-11b-v-32k-chat`(多 `-chat`)。平台清單以不含 `-chat` 為準;若遇到模型不存在的錯誤,可試加此後綴
6. **用量查詢方式**:Gateway Dashboard 位址、本專案使用的 Project 名稱、標籤格式 `{prefix}-{round}-{stage}` 的說明
7. **測試輪次的操作慣例**:round 預設為當下時間自動產生;批次測試多份文件時務必用 shell 變數固定同一個 round(附上該段迴圈範例);每次執行都帶 `--round-note` 說明改了什麼;查閱時對照 `runs/ROUNDS.md` 與 Dashboard 的 By Label
8. 平台限制摘要:單圖最多 4×(560×560) 單位、整頁實際只被看到約 1120px 長邊、rate limit 未公開
9. 已知限制與待確認事項

---

## 10. 驗收檢查清單

實作完成後,coding agent 需自行確認以下項目全部通過:

- [ ] `scripts/check_vision.py` 已執行,Gemma 四個模型的視覺支援結果已寫入 README
- [ ] `--model` 傳入不在常數表中的名稱時,程式拒絕執行並列出可用清單
- [ ] 不帶 `--round` 連跑兩次,兩次的 round 應為當下時間且格式為 `YYYYMMDD-HHMM`,stdout 標明 `(auto)`
- [ ] 帶 `--round baseline` 跑,stdout 標明 `(specified)`,標籤為 `imagescan-baseline-*`
- [ ] 用 README 的 shell 迴圈跑兩份文件,Dashboard 的 **By Label** 上兩份文件的用量應**合併在同一組標籤**,不是兩組
- [ ] 單次執行內,`meta.json` 的 `locate` 與 `extract` 兩個 label 的 round 片段必須完全相同(驗證 round 只計算一次)
- [ ] `--round "R 02!"` 之類的非法值應被拒絕並印出明確錯誤,不得自動清洗後照跑
- [ ] `runs/ROUNDS.md` 在多次執行後正確累積多列,既有列未被覆寫
- [ ] 將 `.env` 的 `GATEWAY_API_KEY` 改成無效值,程式應在**第一次請求**就中止並印出明確訊息,不得對每個欄位各重試 3 次
- [ ] 將 `GATEWAY_BASE_URL` 改成錯誤 port,程式應立即中止並印出目前使用的位址
- [ ] 以一份多頁 PDF 執行,流程完整跑完不中斷
- [ ] 以一張 JPG 執行,流程完整跑完不中斷
- [ ] `runs/` 下產生完整目錄結構,各階段檔案齊全
- [ ] `01_layout/page_001_preview.png` 中的紅框與中文欄位名稱清晰可讀
- [ ] 裁切圖檔名含中文且未出現亂碼或非法字元
- [ ] 刻意將 `.env` 的 model 改成不存在的值,程式應優雅失敗並在 `meta.json.errors` 留下記錄,不出現未捕捉的 traceback
- [ ] 刻意讓 LLM 回傳非 JSON 內容(可暫時改 prompt 測試),程式應記錄原始回應並繼續執行
- [ ] `report.html` 可單獨開啟,圖片正常顯示,匯出的 CSV 以 Excel 開啟中文不亂碼
- [ ] 專案中未 commit `.env` 與 `runs/`;`git grep "sk-proj-"` 無結果
- [ ] `00_input/` 同時存在原圖與 `_locate.png` 縮圖,且縮圖長邊為 1120
- [ ] `meta.json` 中的 `token_usage` 有實際數字(非全 0),`response_format` 是否可用已記錄於 `notes`

---

## 11. 選用項目(P2,行有餘力再做)

新增 `--mode` 參數,支援 `pipeline`(預設,即上述三階段)與 `whole-page` 兩種模式。

`whole-page` 模式:整頁圖片單次送 LLM,直接要求輸出所有 `{欄位名: 值, 信心值}` 的 JSON,不做定位與裁切。標籤 stage 使用 `wholepage`(完整標籤如 `imagescan-20260807-1432-wholepage`)。結果輸出至 `90_wholepage/result.json`,並在 report.html 增加一區並列顯示兩種模式的結果。

> 目的:作為對照組。若整頁單次處理的正確率與三階段流程相近,則三階段的複雜度與 API 成本並不划算。這對後續架構決策相當關鍵,建議務必安排時間完成。
>
> 因為兩種模式使用不同的 stage 標籤,**在同一個 `--round` 下**跑完兩種模式,即可在 Dashboard 的 By Label 分頁直接讀出 `...-locate` + `...-extract` 對上 `...-wholepage` 的 token 成本差距,不需另外計算。比較成本時務必用同一 round,否則等於拿不同 prompt 的結果互比。