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

## 5. 測試

### 5.1 S00 單元測試（不需模型）

```powershell
.venv\Scripts\python -m unittest discover -s demo\tests -v
```

會檢查 `/api/health` 的欄位、確認回應不含 LINE 金鑰、確認 `DEMO_DATA=false` 時拒絕啟動。

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
| `demo/server/data/state.json` | 目前**所有案件共用**的待辦狀態 | 不提交 |
| `demo/server/data/line_users.json` | LINE 好友清單（全域） | 不提交 |
| `demo/server/uploads/` | 上傳的原件與證明 | 不提交 |
| `demo/server/cache/`、`tmp/` | 模型回應快取、暫存 | 不提交 |

S01 之前的重置方式（先停止伺服器）：

```powershell
Remove-Item -Recurse -Force demo\server\data, demo\server\uploads, demo\server\tmp -ErrorAction SilentlyContinue
```

網頁上的「清空待辦」（`DELETE /api/todos`）只會重設 `state.json`，**不會刪除上傳的原件**，這是已知缺口（S02）。`cache/` 裡存的是預存的模型回應，網路斷線時展示會用到，平常不要刪。

S10 會提供 `demo/seed.py`、`demo/reset.py`，用來取代上述手動步驟。

## 7. 目前已知限制（S00 基線，未修正）

以下限制原樣保留在基線中，依工作包排程修正：

- 沒有登入、Session 或案件隔離；匿名呼叫 `GET /api/todos` 會回 200 和全部待辦 → **S01**
- 上傳原件會寫入磁碟，清空時不會刪除；掃描圖片會直接送外部模型 → **S02**
- 證明判讀為「準時」時會自動視為完成 → **S03**
- LINE 推播會發給所有好友；沒有配對碼，也沒有排程 → **S05**
- 失聯流程是用日期推算，沒有判斷「實際應工作日」 → **S06**
- 沒有 Manifest／Service Worker → **S09**

**在 S01、S02 完成前，不可讓評審或真實雇主掃碼上傳資料。**
