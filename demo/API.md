# 竹聘安決賽示範系統 API（S02 版）

本文件分成兩部分：

- **A. 現有 API**：目前程式碼的實際行為（2026-11-17，S02 分支），每個端點都標出權限。
- **B. 目標契約**：工作包第 3 節規定、**尚未實作**的部分，由 S03–S10 完成。

改動 API 的 PR 必須同步更新本文件，附上請求／回應範例與錯誤碼。

除特別註明外，錯誤回應一律是 `{"detail": "<中文錯誤訊息>"}`；未處理的例外會回 500 `{"detail": "系統錯誤：<例外類別>"}`，不帶堆疊或資料內容。

---

## A. 現有 API（S00 基線）

> S01 起，資料存在 SQLite（`demo/server/data/zhupinan.db`，可用 `ZP_DB_PATH` 指定），**不再使用 `state.json`**。除了 `/api/health`、`POST /api/session`、`/line/webhook`，所有 `/api/` 端點都要登入。只能使用合成資料。

### 共通規則（S01）

| 情況 | 回應 |
|---|---|
| 未登入、Session 過期或已登出 | 401 `{"detail":"請先登入"}` 或 `{"detail":"登入已過期，請重新登入"}` |
| 角色不符（雇主呼叫承辦人 API，或反過來） | 403 |
| 存取別人的案件或待辦，或 ID 不存在 | **404**（不回 403，避免透露別人的 ID 是否存在） |
| 跨站寫入（`Origin` 與 Host 不同，或 `Sec-Fetch-Site: cross-site`） | 403 `{"detail":"拒絕跨站請求"}`，請求處理程式不會執行 |
| 所有 `/api/` 回應 | `Cache-Control: no-store` |

權限代號：**公開**＝不需登入；**登入**＝任何角色；**雇主**＝`employer`；**承辦人**＝`officer`；**管理**＝示範管理帳號（目前是 `officer`）。

### 登入與案件（S01 新增）

| 端點 | 權限 | 請求 | 回應 | 錯誤 |
|---|---|---|---|---|
| `POST /api/session` | 公開 | `{"account":"employer_a","code":"<登入碼>"}` | `{account:{login_name, role, display_name, is_demo_admin}, cases:[…]}`，同時設定 Cookie `zp_session`（HttpOnly、SameSite=Lax；HTTPS 時加 Secure；有效 8 小時） | 401：帳號或登入碼錯誤（無論帳號是否存在，訊息都一樣）；同一帳號＋IP 連錯 5 次後，10 分鐘內一律回 429 |
| `GET /api/session` | 登入 | — | 同上 | 401 |
| `DELETE /api/session` | 公開 | — | `{ok:true}`；在伺服器端刪除 Session 並清除 Cookie | — |
| `GET /api/cases` | 雇主 | — | `{cases:[{id, display_code, label, created_at, updated_at}]}`，只回傳自己的案件 | 401、403 |
| `GET /api/cases/{id}` | 雇主 | — | `{case:{…}, tasks:[…]}` | 404 |
| `POST /api/demo/reset` | 管理 | — | `{ok:true}`；在一次交易中刪除所有案件、文件、待辦、證明、LINE 綁定、通知紀錄與稽核紀錄，再重建空白的種子案件，最後清空上傳與暫存資料夾 | 401、403 |

示範帳號有三個：`employer_a`（家庭看護，案件 `DEMO-A01`）、`employer_b`（產業，案件 `DEMO-B01`）、`officer`（承辦人，兼示範管理）。登入碼的設定方式見 `demo/DEVELOPMENT.md`。

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

### 文件擷取與確認（S02）

上傳一律依**檔案內容**判斷格式，不看副檔名；原始位元組只留在記憶體中，**不保存原件**。

| 檢查 | 失敗時回應 |
|---|---|
| 大於 15 MB | 413「檔案太大（上限 15 MB）」 |
| 副檔名不是 jpg／jpeg／png／pdf／txt，或內容和副檔名不符（例如把 PNG 改名成 .txt），或文字檔含二進位內容 | 415 |
| PDF 損毀、有密碼、沒有頁面 | 422「PDF 檔案損毀或無法開啟」 |
| PDF 超過 10 頁 | 413 |
| **圖片，或沒有文字的掃描 PDF，且不是團隊的合成範例**（以 SHA-256 比對 `demo/web/samples`、`validation/docs`、`validation/docs_holiday`） | 422 `{"detail":"為保護個資…","code":"real_image_blocked"}`，**不會呼叫外部模型** |

| 端點 | 權限 | 請求 | 回應 | 錯誤 |
|---|---|---|---|---|
| `POST /api/documents/extract`（舊路徑 `/api/extract` 是同一個處理程式） | 雇主 | multipart `file` | `{extract_id, document_kind, fields:[{worker, field, value, source_excerpt, warning}], workers:[…舊格式，含 flags/evidence], source:{type, synthetic_sample, notes}, masked_preview, privacy_note, model, is_image}`。**不寫入案件。** 文字檔在本機遮蔽後才送給模型，`masked_preview` 就是實際送出的內容。`warning` 是白話的需核對理由（掃描件、國字日期、民國年換算、找不到原文），不提供信心分數。辨識結果只存在伺服器記憶體中，30 分鐘後失效 | 上表各項；502 模型失敗 |
| `DELETE /api/documents/extract/{extract_id}` | 雇主 | — | `{ok:true}`，丟棄尚未確認的辨識結果 | 404 |
| `POST /api/documents/confirm` | 雇主 | `{document_kind, workers:[{name, entry_date?, permit_start?, permit_end?, event_date?, notify_date?, job?}], extract_id?, checked:["0:entry_date",…], case_id?}` | `{task_ids, added, warnings, obligations, case_id, calendar_years}`。只保存姓名、工作類別與日期欄位，外加原檔的 SHA-256。不帶 `extract_id` 時視為使用者手動輸入 | **422 `{"detail":…,"unchecked":["0:entry_date"]}`**：有 `warning` 的日期如果維持 AI 讀到的值，就必須列在 `checked` 中（改過的值視為使用者已確認）；404：`extract_id` 不存在、已逾時、已用過或屬於別人 |

不認得的 `document_kind` 會回 200、`task_ids:[]`，並在 `warnings` 提示「需人工確認」。

以下端點都要用**雇主**帳號登入，而且只會讀寫自己的案件。

| 端點 | 請求 | 回應／行為 | 錯誤 |
|---|---|---|---|
| `POST /api/obligations` | `{doc_type, workers:[{name, entry_date?, permit_start?, permit_end?, event_date?}], save=true, case_id?, source_filename?}` | 計算期限預覽，前端不再用它寫入。`save=true` 等同手動輸入。回傳 `{obligations:[…], added, calendar_years, case_id}`。`save=true` 時寫入 `case_id`（預設為自己的第一個案件）的 `documents` 與 `tasks`；同一案件內「同一移工＋同一規則＋同一期間」由唯一索引擋下，不會重複。`save=false` 只計算、不寫入。 | 422 欄位格式錯誤。**`doc_type` 不認得時回 200 加空清單**，不會提示需人工確認（S03 修正） |
| `GET /api/todos` | — | `{workers, items}`，只回傳自己案件的待辦。`items[].id` 是待辦 ID（字串），另含 `case_id`、`case_code`、`status` | 401、403 |
| `DELETE /api/todos` | — | 只刪除自己案件的文件、待辦、證明，以及這些證明的檔案 | 401、403 |
| `POST /api/todos/{tid}/assign` | `{assignee: "仲介" \| null}` | `{ok, assigned}`；狀態改為 `delegated` 或 `todo` | 404 |
| `POST /api/todos/{tid}/proof` | multipart `file`、`by`（預設「雇主」） | 上傳檢查與文件相同（真實圖片同樣會被擋下）。判讀時**不送移工姓名**，姓名比對在本機進行。**不保存原件**。判讀結果 `{verdict, verdict_text, completed_date, …, original_kept:false}` 存入 `proofs`，狀態改為 `proof_submitted`。**畫面仍會把判為準時或逾期的證明顯示成已完成**（沿用基線行為，S03 會改成由人工確認） | 404、413、415、502 |
| `DELETE /api/todos/{tid}/proof` | — | `{ok}` | 404 |
| `GET /api/todos/{tid}/proof/file` | — | S02 起一律回 404「系統不保存證明原件」 | 404 |

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

| 端點 | 權限 | 請求 | 回應 | 錯誤 |
|---|---|---|---|---|
| `POST /api/ask` | 登入 | `{question}` | `{answer, citations, retrieved}`（先用 BM25 加同義詞檢索，再交給模型回答） | 400：問題為空；502 |
| `POST /api/check-penalty` | 承辦人 | `{text}`（裁處書草稿） | `{items:[七項要件結果], masked_preview}` | 403：雇主帳號；400：草稿太短；502 |
| `POST /api/mask-preview` | 登入 | `{text}` | `{masked, pseudonyms}`（本機遮蔽預覽） | — |

### LINE

| 端點 | 行為 |
|---|---|
| `GET /api/line/status`（雇主） | `{configured, bound, followers}`；`bound` 是這個帳號目前有效的 LINE 綁定數 |
| `GET /api/line/preview?days=30&lang=zh`（雇主） | `{count, text}`，只用自己的待辦；`lang` 可為 zh、id、vi |
| `POST /api/line/send` `{days, lang}`（雇主） | 只把**自己的**待辦推播給**綁定在自己帳號**的 LINE 使用者（`line_bindings`）。S01 起不再有全域好友清單。配對功能（S05）完成前沒有任何綁定，因此只回預覽：`mode:"preview"`、`sent:0` |
| `POST /line/webhook`（公開，以簽章驗證） | 先用原始 body 驗證 `x-line-signature`（HMAC-SHA256），驗證失敗回 403。使用者傳「待辦」時：已綁定的帳號只會收到該雇主自己的待辦；**未綁定的帳號只會收到配對說明，不含任何資料**。配對碼由 S05 實作 |

---

## B. 目標契約（工作包第 3 節，尚未實作）

| 端點 | 用途 | 權限與關鍵回應 | 工作包 |
|---|---|---|---|
| `GET/PATCH /api/tasks/{id}` | 詳情與狀態 | 檢查 owner；只允許合法的狀態轉移 | S03 |
| `POST /api/tasks/{id}/proof`、`/confirm` | 證明判讀／人工確認完成 | 兩步分開；判讀結果不會自動變成 `done` | S03 |
| `POST /api/line/pair-code`、`DELETE /api/line/binding` | 配對／解除綁定 | 一次性配對碼，10 分鐘失效 | S05 |
| `POST /api/line/preview`、`/send-demo` | 預覽／對指定帳號測試發送 | 禁止廣播；回傳實際收件人數 | S05 |
| `POST /line/webhook` | LINE 事件 | 先驗簽章；只處理配對、待辦查詢、取消綁定 | S05 |
| `POST /api/missing/guide` | 聯絡不上移工 | 回傳狀態、缺少的事實、聯絡資訊、法源；不下「行蹤不明」的結論 | S06 |
| `POST /api/qa` | 個案問答 | 只使用已確認的個案事實 | S07 |
| `POST /api/penalty/check` | 處分書檢核 | 只限 officer；雇主回 403 | S08 |
| `POST /api/demo/reset`（重建合成案件） | 重置後重建固定的合成文件與待辦（S01 目前只重建空白案件） | 只限示範管理帳號 | S10 |

以下共通要求已在 S01 落實：匿名呼叫回 401、角色不符回 403、跨站寫入回 403、所有 `/api/` 回應一律 `no-store`。新增端點時必須沿用 `auth.require_*` 依賴與 `owned_case()`，並在 `demo/tests/` 加上越權測試。
