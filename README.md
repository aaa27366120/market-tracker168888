# Market Tracker（市場觀察）

這是一個以 GitHub Pages 發布的靜態市場觀察頁面。資料由 GitHub Actions 執行 `fetch_data.py` 更新，寫入 `docs/data.json`。

## 專案檔案

- `fetch_data.py`：抓取公開行情、計算近 60 日支撐、60 日均線、量比與一年高點回落幅度。
- `docs/data.json`：網站讀取的資料檔；每個標的保留自己的行情日期。
- `docs/index.html`：市場觀察頁面。
- `.github/workflows/update.yml`：每週二至週六 UTC 00:30（台灣時間 08:30）排程更新，也可手動執行。

## 第一階段資料品質規則

1. 一年高點回落 15%／25% 僅為回落幅度觀察，不是買進、加碼或進場訊號。
2. 指數與匯率不顯示不具可比性的成交量比；成交量缺漏或為 0 時，量比顯示為無資料，不當成 0 倍量。
3. 外資資料記錄抓取狀態、來源資料日期與檢查時間。抓取失敗時沿用既有歷史資料，但頁面會明確標示舊資料。
4. 單一行情來源失敗時，嘗試備援來源；若仍失敗，保留上一筆有效行情並標示為舊資料，不把更新失敗偽裝成新行情。
5. 更新程式以暫存檔寫入 JSON，避免寫檔中斷造成資料檔損毀。
6. 若所有行情標的都更新失敗，GitHub Actions 驗證步驟會失敗，不會把失敗快照提交到正式網站。

## 本機檢查

需要 Python 3.10 以上，以及 `pandas`、`requests`、`yfinance`。可執行：

```bash
pip install pandas requests yfinance
python -m py_compile fetch_data.py
python fetch_data.py
```

執行 `fetch_data.py` 需要網路連線，且公開資料來源可能延遲、限流或暫時不可用。請檢查 `docs/data.json` 中的 `update_status`、各標的 `date`／`stale`，以及 `foreign_meta`，不要只看程式執行時間。

## 使用限制

本頁僅整理公開市場資訊，不構成投資建議。行情可能延遲或有誤，應以交易所及資料提供者公告為準。
