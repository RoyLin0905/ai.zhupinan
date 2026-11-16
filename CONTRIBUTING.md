# 協作規則（決賽開發期）

工作範圍與驗收依據：[`docs/竹聘安_決賽系統開發_130人時.md`](docs/竹聘安_決賽系統開發_130人時.md)（S00–S10）。

## 分支

| 分支 | 用途 |
|---|---|
| `main` | 整合基線。**不直接推功能**，只透過審查過的 PR 合併。 |
| `release/hackathon-2026` | S00 鎖定的可運行基線；11/19 晚間凍結時更新並打 tag，之後只收 P0／P1 修正。 |
| `feature/<人或代號>-sXX-主題` | 每項工作一支，例：`feature/roy-s04-today-list`、`feature/claude-s01-sqlite`。從最新 `main` 開。 |

```bash
git clone https://github.com/RoyLin0905/ai.zhupinan.git
cd ai.zhupinan
git switch -c feature/<代號>-s01-<主題> origin/main
# ……修改、測試……
git push -u origin feature/<代號>-s01-<主題>
```

每天至少把 `main` 合併或 rebase 進自己的分支一次；同一資料表與 API 契約由一人定稿，避免同時大改 `demo/server/app.py`。

## PR 必附

PR 標題以工作包編號開頭，例：`S01: SQLite 與 Session`。內文需有：

1. **工作包編號**與本 PR 對應的交付／驗收條目。
2. **變更檔案**、使用者可看到的結果、API／資料表更動（同步更新 `demo/API.md`）。
3. **驗收證據**：實際執行的測試命令與輸出、通過／失敗的案例、執行畫面截圖（手機版附寬度）。
4. **已知限制**與下一個工作包的交接條件。

規劃、靜態畫面或概念稿不算完成；`design/concept/` 只是草稿。

## 資料與祕密

- 一律使用合成資料（`DEMO_DATA=true`）。不得提交真實公文、真實移工資料、訪談紀錄、個資。
- 不得提交 `line.env`、`.env`、API token、SQLite 資料庫、`demo/server/data|uploads|cache|tmp`。推送前執行 `git status` 確認。
- 發現祕密已進入提交：**停止推送**，通知整合負責人，撤銷該金鑰後再處理歷史。

## 合併前

- 跑過 `demo/DEVELOPMENT.md` 的測試命令，既有 `validation/` 基準不得退步。
- 至少一人審查；涉及權限、隱私、LINE 收件人的變更需第二人看過。
