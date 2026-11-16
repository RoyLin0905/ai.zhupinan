# 開發與驗收環境（S00）

本文件讓一台全新的 Windows 電腦在 20 分鐘內把決賽示範系統跑起來、跑測試、重置資料。所有資料都是**合成資料**（`DEMO_DATA=true`）。

## 1. 已驗證的環境

2026-11-15 在 Windows 11 Home 10.0.26200 上，用全新的 venv 驗證過：

| 項目 | 版本 |
|---|---|
| Python | 3.12.10 |
| fastapi | 0.163.1 |
| starlette | 1.10.0 |
| pydantic / pydantic_core | 2.14.3 / 2.50.4 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| python-multipart | 0.0.33 |
| PyMuPDF | 2.1 |
| Pillow | 13.0.0 |

`demo/requirements.txt` 只寫下限版本；如果新版套件出問題，改裝上表的版本。

另需（只有擷取、問答、處分書檢核要用）：[Claude Code CLI](https://docs.anthropic.com/claude-code)，並已執行 `claude auth login`。沒有 CLI 時，伺服器照樣能啟動，健康檢查、期限計算、離線評分都能用。

## 2. 安裝（約 1 分鐘，不含下載 Python）

在 repo 根目錄開 PowerShell：

```powershell
git clone https://github.com/RoyLin0905/ai.zhupinan.git
cd ai.zhupinan
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r demo\requirements.txt
```

`.venv/` 已列入 `.gitignore`。

## 3. 環境變數

範本在 `demo/.env.example`。

| 變數 | 必要 | 說明 |
|---|---|---|
| `DEMO_DATA` | 否（預設 `true`） | 合成資料示範模式的唯一標記。設成 `true` 以外的值，伺服器會**拒絕啟動**，因為這個版本沒有經過處理真實資料所需的審查。 |
| `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` | 否 | LINE Messaging API。也可以寫在 `demo/server/line.env`（見 `line.env.example`）。環境變數優先。**不可提交。** |
| `DEMO_LOGIN_CODES` | 否 | 團隊共用、可重複使用的登入碼，格式為 `employer_a:碼,employer_b:碼,officer:碼`。**沒有設定時**，伺服器每次啟動都會隨機產生一組，並**只印在伺服器自己的主控台**。不要寫進任何會提交的檔案。 |
| `ZP_DB_PATH` | 否 | SQLite 檔案位置，預設為 `demo/server/data/zhupinan.db`（已列入 `.gitignore`）。 |
| `ZP_UPLOAD_DIR` | 否 | 舊版留下的上傳資料夾，預設為 `demo/server/uploads`。S02 起新上傳不再寫入這裡，重置時會清空。 |
| `ZP_TMP_DIR` | 否 | 合成掃描圖在模型讀取期間的暫存資料夾，預設為 `demo/server/tmp/ingest`；每次呼叫結束（含失敗）都會刪除。 |
| `COOKIE_SECURE` | 否 | 設為 `true` 時，Cookie 一律加上 `Secure`。使用 HTTPS 時（包括反向代理送出 `X-Forwarded-Proto: https`）也會自動加上。 |
| `DOCS_DIR`、`QSET` | 否 | 只給驗證腳本使用，見第 5 節。 |

## 4. 啟動

```powershell
.venv\Scripts\python -m uvicorn app:app --app-dir demo\server --host 127.0.0.1 --port 8765
```

開瀏覽器到 <http://127.0.0.1:8765>。根目錄的 `啟動DEMO.bat` 會使用系統的 `python`，不是 `.venv`，只適合已經全域安裝好依賴的展示機。

健康檢查：

```powershell
curl.exe -s http://127.0.0.1:8765/api/health
```

應該會看到 `"demo_mode":true`、`"calendar_years":["2026","2027"]`、`"articles":359`，以及六部法規的修正日期。回應中不含任何金鑰。

### 登入（S01）

打開網頁會先看到登入畫面。選擇示範帳號後輸入登入碼：

- 團隊開發時：先設定 `DEMO_LOGIN_CODES` 再啟動，或直接看伺服器主控台印出的隨機登入碼。
- 給評審或試用者：產生**一次性**登入碼（用過一次就失效，預設 12 小時內有效）：

```powershell
.venv\Scripts\python demo\server\auth.py issue employer_a 12
```

| 帳號 | 角色 | 可以做的事 |
|---|---|---|
| `employer_a` | 雇主（家庭看護），案件 `DEMO-A01` | 上傳公文、管理待辦、上傳證明、法規問答、LINE 預覽 |
| `employer_b` | 雇主（產業），案件 `DEMO-B01` | 和 A 一樣，但只看得到自己的案件 |
| `officer` | 承辦人，兼示範管理 | 處分書檢核、法規問答、重置展示資料；**看不到任何雇主案件** |

## 5. 測試

### 5.1 單元測試：S00 健康檢查、S01 登入與隔離、S02 文件與隱私（不需模型）

```powershell
.venv\Scripts\python -m unittest discover -s demo\tests -v
```

S00 的 3 項：`/api/health` 欄位正確、回應不含 LINE 金鑰、`DEMO_DATA=false` 時拒絕啟動。

S01 的 25 項（`test_s01_auth.py`）：登入成功與失敗、第 6 次嘗試被擋（429）、Cookie 旗標正確且資料庫只存雜湊、登出與過期後失效、匿名呼叫 11 個受保護端點都回 401、A 和 B 雇主互相讀寫都被擋下（404）、雇主不能用承辦人功能且承辦人讀不到雇主案件（403）、跨站寫入被拒、重複匯入不會重複產生待辦、重置後資料清空，以及 LINE 方面：未綁定的帳號收不到任何待辦、已綁定的帳號只收到自己的、簽章錯誤會被拒、沒有綁定時不會發送。

S02 的 22 項（`test_s02_documents.py`，模型以替身取代）：副檔名與內容不符、超過 15 MB、損毀的 PDF、超過 10 頁、含二進位的文字檔都會被拒且不留檔；文字送出前已遮蔽，而且送出的內容和預覽一字不差；真實圖片與非範例的掃描 PDF 不會呼叫模型；合成範例圖在模型讀取期間存在、結束後刪除；模型失敗時不留檔；擷取不寫入案件；未核對的標示日期無法產生待辦；勾選後才產生，而且只能確認一次；改過的日期視為已確認，並依新日期重算；別人的辨識結果不能用；取消後失效；手動輸入可用；不認得的文件類型會提示需人工確認；證明不保存原件、不送姓名、真實圖片被擋。

### 5.2 既有驗證基準：離線重新評分（不需模型）

這些指令用 `validation/runs/` 裡已存的模型擷取結果，**重新跑一次確定性的期限規則**，再和標準答案比對。跑完之後 `git status` 應該不會出現任何差異（`validation/results/*.md` 和已提交的版本逐字相同）。

```powershell
$env:PYTHONIOENCODING = "utf-8"
cd validation\scripts
..\..\.venv\Scripts\python score.py extract_haiku extract_haiku_mask
$env:DOCS_DIR = "docs_holiday"; ..\..\.venv\Scripts\python score.py extract_haiku_docs_holiday extract_haiku_mask_docs_holiday; Remove-Item Env:DOCS_DIR
..\..\.venv\Scripts\python test_privacy.py
..\..\.venv\Scripts\python qa.py score
$env:QSET = "holdout"; ..\..\.venv\Scripts\python qa.py score; Remove-Item Env:QSET
..\..\.venv\Scripts\python penalty.py score
cd ..\..
git status --short validation
```

2026-11-15 的基準（全部重現，無差異）：

| 基準 | 結果 | 檔案 |
|---|---|---|
| 公文期限（遮蔽後，純文字 35 份） | 應辦期限 133/133 | `validation/results/score_extract_haiku_vs_extract_haiku_mask.md` |
| 連假／補假期限 | 33/33 | `validation/results/score_extract_haiku_docs_holiday_vs_extract_haiku_mask_docs_holiday.md` |
| 個資遮蔽洩漏測試（266 筆合成個資，**不含掃描圖**） | 洩漏 0、誤遮關鍵行 0 | `validation/results/privacy_leak_test.md` |
| 法規問答 30 題（rag2s） | 內容 29/30、條號 28/30 | `validation/results/score_qa.md` |
| 法規問答 10 題保留題（rag2s） | 10/10 | `validation/results/score_qa_holdout.md` |
| 處分書缺漏 30 份 | 偵測 29/30（遮蔽後 30/30） | `validation/results/score_penalty.md` |

### 5.3 重新呼叫模型（需要 Claude CLI，會花費額度）

只有在修改擷取提示詞、問答或處分書檢核時才需要重跑，指令見 README「重現驗證」與 `validation/驗證報告.md`。PR 中請附上新舊兩份分數。

## 6. 資料位置與重置

| 路徑 | 內容 | 版控 |
|---|---|---|
| `demo/server/data/zhupinan.db` | SQLite：帳號、Session（只存雜湊）、案件、文件欄位、待辦、證明判讀、LINE 綁定、通知紀錄、稽核紀錄 | 不提交 |
| `demo/server/uploads/` | 只剩 S02 以前版本留下的檔案；新上傳不寫入 | 不提交 |
| `demo/server/tmp/ingest/` | 合成掃描圖在模型讀取期間的暫存檔，用完即刪 | 不提交 |
| `demo/server/cache/` | 模型回應快取。**只快取合成範例**；使用者上傳的內容不寫入快取 | 不提交 |
| `demo/server/schema.sql` | 資料表定義，伺服器啟動時自動建立 | 提交 |

**重置展示資料**：用 `officer` 登入後呼叫 `POST /api/demo/reset`。它會在一次交易中清除所有案件資料、LINE 綁定與通知紀錄，重建兩個空白的示範案件，再清空上傳與暫存資料夾。帳號與登入碼會保留。如果要完全從頭開始，先停止伺服器，再刪除資料庫：

```powershell
Remove-Item demo\server\data\zhupinan.db
```

雇主在網頁上按「清除展示資料」（`DELETE /api/todos`）時，只會清除**自己**案件的資料和證明檔。

**舊資料遷移**：S01 起不再讀取 `demo/server/data/state.json`。要把裡面的資料搬進 SQLite：

```powershell
.venv\Scripts\python demo\server\migrate_state.py --account employer_a           # 試跑，不寫入
.venv\Scripts\python demo\server\migrate_state.py --account employer_a --apply   # 先備份到 data\backup-*\，再寫入
```

重複執行不會產生重複的待辦。證明的判讀結果不會遷移。

S10 會提供 `demo/seed.py` 與 `demo/reset.py`，用來重建固定的合成文件與待辦。

## 7. 目前已知限制

S01 已修正以下問題：匿名就能讀取全部待辦、所有人共用 `state.json`、LINE 推播會發給所有好友。

S02 已修正以下問題：原件會落地、清空時不會刪除原件、真實圖片未遮蔽就送給外部模型、副檔名不檢查內容、證明判讀把移工全名放在送給模型的提示中、提示檔殘留在 `tmp/`、使用者上傳的內容被寫入模型快取、AI 讀到的日期不需核對就能產生待辦。

仍待修正：

- 證明判讀為「準時」時，畫面仍會顯示為已完成；期限沒有 `calc_trace`；超出辦公日曆範圍的日期沒有改成「需人工確認」 → **S03**
- 既有介面的全域樣式把輸入框字級設成 11px（規格要求至少 16px，否則 iOS 會自動放大畫面）。登入頁已修正，其他畫面待修 → **S04**
- LINE 配對碼、提醒排程、訊息去識別化（目前訊息仍含移工姓名） → **S05**
- 失聯流程是用日期直接推算，沒有判斷「實際應工作日」 → **S06**
- 沒有 Manifest 與 Service Worker；靜態檔目前以 `Cache-Control: no-cache` 每次重新驗證 → **S09**
- 登入失敗次數的限制記在記憶體中，伺服器重啟後會歸零（單機示範可接受）

- 個資遮蔽的洩漏測試只涵蓋文字（266 筆合成個資，**不含掃描圖**）；掃描圖目前只允許合成範例。
- 真實文字檔仍會送到外部模型（已先遮蔽）。決賽仍應只用合成文件，不可請真實雇主上傳。
