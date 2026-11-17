"""彙總報表：產出單一自包含 report.html（圖片以 base64 內嵌）。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from pipeline.utils import coerce_confidence


def image_to_data_uri(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def confidence_color(confidence) -> str:
    # 防呆：舊的（或手改過的）result.json 可能仍存著 null / 字串型 confidence，
    # 在此再轉一次，讓報表寧可顯示 0.00 也不要整份產不出來。
    confidence = coerce_confidence(confidence)
    if confidence >= 0.9:
        return "#1a7f37"
    if confidence >= 0.7:
        return "#9a6700"
    return "#cf222e"


def _escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def count_stage_failures(extract_results: list[dict] | None, stages: dict | None) -> tuple[int, dict]:
    """回傳 (失敗總數, 各階段失敗數)。

    定位階段整頁失敗時 extract_results 會是空的（沒有欄位就沒有裁切、沒有值辨識呼叫），
    只看 extract_results 會把「上游全掛」顯示成乾淨的 0 失敗，與「這張表單本來就沒有欄位」
    無法區分。故一律以 meta.stages 的各階段 failures 加總為準。"""
    stages = stages or {}
    per_stage = {
        key: int(stages.get(key, {}).get("failures", 0) or 0)
        for key in ("locate", "extract", "wholepage")
        if key in stages
    }
    if per_stage:
        return sum(per_stage.values()), per_stage
    # 沒有 stages 資訊（例如單獨呼叫本模組）時退回舊行為
    fallback = sum(1 for r in (extract_results or []) if r.get("error"))
    return fallback, {"extract": fallback}


def build_header_html(meta: dict, extract_results: list[dict],
                      wholepage_result: dict | None = None) -> str:
    total = len(extract_results)
    failed, per_stage = count_stage_failures(extract_results, meta.get("stages"))
    breakdown = "、".join(f"{k}={v}" for k, v in per_stage.items())
    failed_html = f"{failed}" + (f' <span class="muted">({_escape(breakdown)})</span>' if breakdown else "")
    confidences = [coerce_confidence(r.get("confidence")) for r in extract_results if not r.get("error")]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    wholepage_row = ""
    if wholepage_result is not None:
        wholepage_row = (f"\n    <dt>whole-page 欄位數</dt>"
                         f"<dd>{len(wholepage_result.get('fields') or [])}</dd>")
    return f"""<section class="header">
  <h1>{_escape(meta['input_filename'])}</h1>
  <dl>
    <dt>執行時間</dt><dd>{meta['started_at']}</dd>
    <dt>模型</dt><dd>{_escape(meta['model'])}</dd>
    <dt>Prompt 模式</dt><dd>{_escape(meta.get('prompt_mode', 'user-only'))}</dd>
    <dt>總欄位數</dt><dd>{total}</dd>{wholepage_row}
    <dt>辨識失敗數</dt><dd>{failed_html}</dd>
    <dt>平均信心值</dt><dd>{avg_conf:.2f}</dd>
  </dl>
</section>"""


def build_locate_section_html(preview_paths: list[tuple[int, Path]]) -> str:
    blocks = []
    for page_num, preview_path in preview_paths:
        uri = image_to_data_uri(preview_path)
        blocks.append(f"""<div class="locate-page">
  <h3>定位結果 — 第 {page_num} 頁</h3>
  <img src="{uri}" style="width:100%">
  <div class="locate-judgement">
    <label><input type="radio" name="locate_judge_{page_num}" value="定位正確"> 定位正確</label>
    <label><input type="radio" name="locate_judge_{page_num}" value="部分錯誤"> 部分錯誤</label>
    <label><input type="radio" name="locate_judge_{page_num}" value="大量錯誤"> 大量錯誤</label>
  </div>
</div>""")
    return '<section class="locate"><h2>定位檢查</h2>' + "".join(blocks) + "</section>"


def build_table_rows_html(extract_results: list[dict], crops_dir: Path) -> str:
    rows = []
    for i, r in enumerate(extract_results, start=1):
        crop_uri = image_to_data_uri(crops_dir / r["crop_file"])
        value_html = '<span class="empty">(空白)</span>' if r.get("is_empty") else _escape(r.get("value", ""))
        conf = coerce_confidence(r.get("confidence"))
        color = confidence_color(conf)
        note_or_error = r.get("error") or r.get("notes") or ""
        rows.append(f"""<tr data-confidence="{conf}">
  <td>{i}</td>
  <td>{r['page']}</td>
  <td>{_escape(r['field_name'])}</td>
  <td><img src="{crop_uri}" class="crop-thumb" onclick="enlarge(this.src)"></td>
  <td>{value_html}</td>
  <td style="color:{color}">{conf:.2f}</td>
  <td>{_escape(note_or_error)}</td>
  <td>
    <select class="judge">
      <option value="未判定" selected>未判定</option>
      <option value="正確">正確</option>
      <option value="錯誤">錯誤</option>
      <option value="部分正確">部分正確</option>
    </select>
  </td>
  <td><input type="text" class="judge-note"></td>
</tr>""")
    return "".join(rows)


def build_wholepage_section_html(wholepage_result: dict | None) -> str:
    if not wholepage_result or not wholepage_result.get("fields"):
        return ""
    rows = "".join(
        f"""<tr>
  <td>{f.get('page', '')}</td><td>{_escape(f.get('field_name', ''))}</td>
  <td>{'(空白)' if f.get('is_empty') else _escape(f.get('value', ''))}</td>
  <td>{coerce_confidence(f.get('confidence')):.2f}</td>
</tr>"""
        for f in wholepage_result["fields"]
    )
    return f"""<section class="wholepage">
  <h2>對照組：整頁單次辨識（whole-page）明細</h2>
  <table>
    <thead><tr><th>頁</th><th>欄位名稱</th><th>辨識值</th><th>信心值</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>"""


def _cell_html(entries: list[dict]) -> str:
    """把同一欄位名稱在同一側的所有結果組成一格。多筆以 ｜ 分隔，不做任何配對推測。"""
    if not entries:
        return '<span class="muted">—</span>'
    parts = []
    for e in entries:
        value = '<span class="empty">(空白)</span>' if e.get("is_empty") else _escape(e.get("value", ""))
        conf = coerce_confidence(e.get("confidence"))
        parts.append(f'{value} <span class="conf" style="color:{confidence_color(conf)}">({conf:.2f})</span>')
    return " ｜ ".join(parts)


def build_comparison_section_html(extract_results: list[dict] | None,
                                  wholepage_result: dict | None) -> str:
    """三階段管線 vs whole-page 的同欄位名稱對齊比較表（--mode both）。

    依設計文件：「同一欄位名稱對齊比較；欄位名稱若兩邊不同則各自列出，不做自動配對演算法」，
    因此配對條件是 field_name 的**完全字串相等**，沒有模糊比對。只出現在單側的欄位仍會
    獨立成列，另一側留空。"""
    if not extract_results or not wholepage_result or not wholepage_result.get("fields"):
        return ""

    pipeline_map: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in extract_results:
        name = str(r.get("field_name", ""))
        if name not in pipeline_map:
            pipeline_map[name] = []
            order.append(name)
        pipeline_map[name].append(r)

    wholepage_map: dict[str, list[dict]] = {}
    for f in wholepage_result["fields"]:
        name = str(f.get("field_name", ""))
        if name not in wholepage_map:
            wholepage_map[name] = []
            if name not in pipeline_map:
                order.append(name)
        wholepage_map[name].append(f)

    rows = []
    for name in order:
        left = pipeline_map.get(name, [])
        right = wholepage_map.get(name, [])
        only = "" if (left and right) else ' class="one-sided"'
        rows.append(f"""<tr{only}>
  <td>{_escape(name)}</td>
  <td>{_cell_html(left)}</td>
  <td>{_cell_html(right)}</td>
</tr>""")

    both_count = sum(1 for n in order if pipeline_map.get(n) and wholepage_map.get(n))
    return f"""<section class="comparison">
  <h2>對照比較：三階段管線 vs whole-page（依欄位名稱完全相等對齊）</h2>
  <p class="muted">共 {len(order)} 個相異欄位名稱，其中 {both_count} 個兩邊都有；
  只出現在單側者以「—」表示另一側沒有對應欄位。名稱不同即各自列出，不做模糊配對。</p>
  <table id="comparison-table">
    <thead><tr><th>欄位名稱</th><th>三階段管線結果</th><th>whole-page結果</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>"""


_STYLE = """
body { font-family: -apple-system, "Microsoft JhengHei", sans-serif; margin: 24px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }
.crop-thumb { height: 60px; cursor: zoom-in; }
.empty { color: #888; }
.muted { color: #888; font-weight: normal; }
.conf { font-size: 90%; }
.one-sided td { background: #fffdf0; }
.locate-page { margin-bottom: 24px; }
#overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.8);
           align-items:center; justify-content:center; z-index:999; }
#overlay img { max-width:90%; max-height:90%; }
"""

_SCRIPT_TEMPLATE = """<script>
function enlarge(src) {{
  document.getElementById('overlay-img').src = src;
  document.getElementById('overlay').style.display = 'flex';
}}

var sortBtn = document.getElementById('sort-toggle');
if (sortBtn) {{
  // 載入時先記下原始順序（頁碼、欄位序），才有東西可以「切回去」
  var sortTbody = document.querySelector('#field-table tbody');
  var originalOrder = Array.from(sortTbody.querySelectorAll('tr'));
  var sortedByConfidence = false;
  sortBtn.addEventListener('click', function() {{
    var rows;
    if (sortedByConfidence) {{
      rows = originalOrder.slice();
      sortBtn.textContent = '依信心值排序';
    }} else {{
      rows = originalOrder.slice().sort(function(a, b) {{
        return parseFloat(a.dataset.confidence) - parseFloat(b.dataset.confidence);
      }});
      sortBtn.textContent = '恢復原始順序';
    }}
    sortedByConfidence = !sortedByConfidence;
    rows.forEach(function(row) {{ sortTbody.appendChild(row); }});
  }});
}}

document.getElementById('export-csv').addEventListener('click', function() {{
  var table = document.getElementById('field-table');
  var rows = table ? Array.from(table.querySelectorAll('tbody tr')) : [];
  var header = ['#', '頁', '欄位名稱', '辨識值', '信心值', '備註', '人工判定', '人工備註'];
  var lines = [header.join(',')];
  rows.forEach(function(row) {{
    var cells = row.querySelectorAll('td');
    var judge = row.querySelector('.judge').value;
    var note = row.querySelector('.judge-note').value;
    var fields = [
      cells[0].textContent, cells[1].textContent, cells[2].textContent,
      cells[4].textContent.trim(), cells[5].textContent.trim(),
      cells[6].textContent, judge, note
    ].map(function(v) {{ return '"' + String(v).replace(/"/g, '""') + '"'; }});
    lines.push(fields.join(','));
  }});
  var csv = '\\ufeff' + lines.join('\\r\\n');
  var blob = new Blob([csv], {{ type: 'text/csv;charset=utf-8;' }});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = {filename_json};
  a.click();
}});
</script>"""


def build_report(
    meta: dict, extract_results: list[dict] | None, preview_paths: list[tuple[int, Path]],
    crops_dir: Path, run_dir_name: str, wholepage_result: dict | None, out_path: Path,
) -> None:
    sections = [build_header_html(meta, extract_results or [], wholepage_result)]

    if preview_paths:
        sections.append(build_locate_section_html(preview_paths))

    if extract_results:
        sections.append(f"""<section class="fields">
  <h2>欄位判讀表</h2>
  <button id="sort-toggle">依信心值排序</button>
  <table id="field-table">
    <thead>
      <tr>
        <th>#</th><th>頁</th><th>欄位名稱</th><th>裁切圖</th><th>辨識值</th>
        <th>信心值</th><th>備註</th><th>人工判定</th><th>人工備註</th>
      </tr>
    </thead>
    <tbody>{build_table_rows_html(extract_results, crops_dir)}</tbody>
  </table>
</section>""")

    # --mode both：先放對齊比較表（人工判讀的主要產物），再放 whole-page 明細（保留頁碼資訊）
    comparison_html = build_comparison_section_html(extract_results, wholepage_result)
    if comparison_html:
        sections.append(comparison_html)

    wholepage_html = build_wholepage_section_html(wholepage_result)
    if wholepage_html:
        sections.append(wholepage_html)

    body = "\n".join(sections)
    filename_json = json.dumps(f"judgement_{run_dir_name}.csv")
    script = _SCRIPT_TEMPLATE.format(filename_json=filename_json)
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>Image Scan POC 報表 — {_escape(meta['input_filename'])}</title>
<style>{_STYLE}</style>
</head>
<body>
{body}
<button id="export-csv">匯出 CSV</button>
<div id="overlay" onclick="this.style.display='none'"><img id="overlay-img"></div>
{script}
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")
