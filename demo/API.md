# 竹聘安決賽示範系統 API（初版，S00）

本文件分成兩部分：

- **A. 現有 API**：S00 基線的實際行為。依 2026-11-15 的程式碼與測試整理。
- **B. 目標契約**：工作包第 3 節規定的 API，**尚未實作**，由 S01–S10 完成。

改動 API 的 PR 必須同步更新本文件，附上請求／回應範例與錯誤碼。

除特別註明外，錯誤回應一律是 `{"detail": "<中文錯誤訊息>"}`；未處理的例外會回 500 `{"detail": "系統錯誤：<例外類別>"}`，不帶堆疊或資料內容。

---

## A. 現有 API（S00 基線）

> ⚠️ 基線**沒有任何登入或權限檢查**，所有端點都能匿名呼叫，所有使用者共用同一份 `state.json`。只能用合成資料。

### `GET /api/health`（S00 已更新）

服務狀態。不含金鑰、Token 或案件資料。

```json
{
  "ok": true,
  "service": "zhupinan-demo",
  "version": "2026.11-finals-s00",
  "demo_mode": true,
  "rules": {
    "engine": "validation/scripts/rules.py",
    "law_versions": {
      "就業服務法": "民國 114 年 01 月 20 日",
      "雇主聘僱外國人許可及管理辦法": "民國 114 年 12 月 30 日",
      "受聘僱外國人健康檢查管理辦法": "民國 114 年 12 月 31 日",
      "行政程序法": "民國 110 年 01 月 20 日",
      "訴願法": "民國 101 年 06 月 27 日",
      "私立就業服務機構收費項目及金額標準": "民國 106 年 04 月 06 日"
    }
  },
  "calendar_years": ["2026", "2027"],
  "articles": 359,
  "line_configured": false
}
```

`law_versions` 讀自 `validation/laws/*.txt` 第二行的修正日期；`line_configured` 只回布林值。`DEMO_DATA` 不是 `true` 時，伺服器根本不會啟動。

### 文件與待辦

| 端點 | 請求 | 回應／行為 | 錯誤 |
|---|---|---|---|
| `POST /api/extract` | multipart `file`（JPG/PNG/WEBP/PDF/TXT/MD） | `{file_id, filename, is_image, model, doc_type, workers:[{name, entry_date, permit_start, permit_end, event_date, evidence:{欄位:原文片段}, flags:{欄位:提示}}]}`。**原件會存到 `demo/server/uploads/`**；文字在本機遮蔽後送 Haiku，**圖片未遮蔽就送 Sonnet**。 | 413 超過 15 MB；415 格式不支援；502 模型失敗 |
| `POST /api/obligations` | `{doc_type, workers:[{name, entry_date?, permit_start?, permit_end?, event_date?}], save=true}` | `{obligations:[…], added, calendar_years}`；`save=true` 時寫入共用的 `state.json`，同一義務不會重複寫入。 | 422 欄位格式錯誤。**`doc_type` 不認得時回 200 加空清單**，不會提示需人工確認（S03 修正） |
| `GET /api/todos` | — | `{workers, items}`，回傳**所有人**的待辦 | — |
| `DELETE /api/todos` | — | 重設 `state.json`；**不會刪上傳檔** | — |
| `POST /api/todos/{tid}/assign` | `{assignee: "仲介" \| null}` | `{ok, assigned}` | 404 |
| `POST /api/todos/{tid}/proof` | multipart `file`、`by`（預設「雇主」） | 判讀結果 `{verdict, verdict_text, completed_date, …}`；判為準時就**自動視為完成** | 404、413、415、502 |
| `DELETE /api/todos/{tid}/proof` | — | `{ok}` | 404 |
| `GET /api/todos/{tid}/proof/file` | — | 回傳證明原件 | 404 |

`doc_type` 的有效值（`rules.compute`）：`入國通報受理證明書`、`聘僱許可函`、`期滿續聘許可函`、`再檢查診斷證明書`、`住宿地點變更`、`家事移工預定入國`、`移工失聯`、`聘僱關係終止`。

`POST /api/obligations` 實測範例（合成資料，`save:false`，不寫入）：

```json
// 請求
{"doc_type": "入國通報受理證明書", "workers": [{"name": "DEMO WORKER A", "entry_date": "2026-02-13"}], "save": false}
// 回應（共 5 項，此處只列第 1 項）
{
  "obligations": [{
    "key": "health_entry", "worker": "DEMO WORKER A", "title": "安排入國三日內健康檢查",
    "start": null, "due": "2026-02-25",
    "basis": "受聘僱外國人健康檢查管理辦法第3條第3款",
    "note": "入國後三個工作日內（依人事行政總處辦公日曆）",
    "days_left": -263,
    "office": "衛生福利部指定醫院（受聘僱外國人健康檢查指定醫院）", "online": null
  }],
  "added": 5, "calendar_years": ["2026", "2027"]
}
```

這個回應**還沒有**工作包要求的 `calc_trace`（起算日、原到期日、順延原因）和 `law_version` → S03。

### 問答與承辦人

| 端點 | 請求 | 回應 | 錯誤 |
|---|---|---|---|
| `POST /api/ask` | `{question}` | `{answer, citations, retrieved}`（BM25 加同義詞檢索，再交給模型回答） | 400 空問題；502 |
| `POST /api/check-penalty` | `{text}` 裁處書草稿 | `{items:[七項要件結果], masked_preview}` | 400 草稿太短；502 |
| `POST /api/mask-preview` | `{text}` | `{masked, pseudonyms}` 本機遮蔽預覽 | — |

### LINE

| 端點 | 行為 |
|---|---|
| `GET /api/line/status` | `{configured, followers}` |
| `GET /api/line/preview?days=30&lang=zh` | `{count, text}`，lang 可為 zh／id／vi |
| `POST /api/line/send` `{days, lang}` | **推播給所有曾加好友的使用者**（`line_users.json`）。未設定金鑰時只回預覽。→ S05 改成只發給指定帳號 |
| `POST /line/webhook` | 先用原始 body 驗證 `x-line-signature`（HMAC-SHA256），失敗回 403。follow／message 事件會把 userId 加入全域清單；傳「待辦」會回覆**全部**待辦 → S05 |

---

## B. 目標契約（工作包第 3 節，尚未實作）

| 端點 | 用途 | 權限與關鍵回應 | 工作包 |
|---|---|---|---|
| `POST /api/session`、`DELETE /api/session` | 登入／登出 | 示範帳號＋一次性登入碼；401、429；過期失效 | S01 |
| `GET /api/cases`、`GET /api/cases/{id}` | 案件清單／詳情 | 只能讀自己的案件；無權限回 404 | S01 |
| `POST /api/documents/extract` | 暫時擷取欄位 | `field, value, source_excerpt, warning, document_kind`；不寫入案件 | S02 |
| `POST /api/documents/confirm` | 確認欄位並產生待辦 | 必填 `case_id`；回傳 `task_ids`、警示 | S02／S03 |
| `GET/PATCH /api/tasks/{id}` | 詳情與狀態 | 檢查 owner；只允許合法的狀態轉移 | S03 |
| `POST /api/tasks/{id}/proof`、`/confirm` | 證明判讀／人工確認完成 | 兩步分開；判讀結果不會自動變成 `done` | S03 |
| `POST /api/line/pair-code`、`DELETE /api/line/binding` | 配對／解除綁定 | 一次性配對碼，10 分鐘失效 | S05 |
| `POST /api/line/preview`、`/send-demo` | 預覽／對指定帳號測試發送 | 禁止廣播；回傳實際收件人數 | S05 |
| `POST /line/webhook` | LINE 事件 | 先驗簽章；只處理配對、待辦查詢、取消綁定 | S05 |
| `POST /api/missing/guide` | 聯絡不上移工 | 回傳狀態、缺少的事實、聯絡資訊、法源；不下「行蹤不明」的結論 | S06 |
| `POST /api/qa` | 個案問答 | 只使用已確認的個案事實 | S07 |
| `POST /api/penalty/check` | 處分書檢核 | 只限 officer；雇主回 403 | S08 |
| `POST /api/demo/reset` | 重置展示資料 | 只限示範管理帳號 | S10 |

共通要求：匿名呼叫回 401；角色不符回 403；跨站的變更請求檢查 `Origin`；個案與問答回應帶 `Cache-Control: no-store`。
