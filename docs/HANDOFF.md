# 換一台機器接手

這份是給「在另一台電腦上把這個專案接下去」用的。
程式碼 git 上都有，但有幾樣東西**不會跟著 git 走**，那才是會卡住的地方。

最後更新：2026-08-27

---

## 目前進度

`main` 已經合併了兩條平行分支的工作：

- 這台 Mac 上的 `fix/review-p0-p1-p2`：報告頁 HTML 化、四個算錯數字的 bug、
  除權息還原（`src/adjust.py`）、模擬倉 `data_guard`、回測對照基準
- 另一台機器（Windows）上的 `claude/auto-trading-arbitrage-reporting-dcu57l`：
  監控層改接程式交易引擎（`src/monitor.py`，`positions.yaml` 的 `source` 開關）

兩邊都在做「把監控層接上程式交易」，但接的是不同東西——
Mac 這邊接的是**真實券商成交明細**（`src/fills.py` 讀 `data/fills.csv`），
Windows 那邊接的是**模擬倉引擎自己的部位帳本**（`data/paper_state.json`）。
合併時保留了 Windows 那邊已經上線的 `monitor.py` 架構，
`src/fills.py` 與 `config/strategy.yaml` 的 `mode: auto` 目前還沒接進
`main.py` / `report.py` 的主流程——見下面「還沒解決的事」。

```bash
git clone https://github.com/ShibaKen223/Investment-Project.git
cd Investment-Project
git checkout main
```

---

## 在新機器上開起來

```bash
pip3 install -r requirements.txt
python3 tests/run_all.py          # 應該全部通過
```

測試不連外網，所以這一步可以立刻驗證環境對不對。

接著**一定要補歷史日 K**，否則模擬倉會整個空轉：

```bash
python3 src/history.py --months 24
python3 src/history.py --status    # 確認每檔累積到哪、中間有沒有缺
```

然後就能跑：

```bash
python3 webapp/app.py             # 儀表板
python3 src/backtest.py           # 回測
python3 src/main.py               # 產生今日報告（模擬倉也會前進一天）
```

---

## 不會跟著 git 走的東西

### 1. `data/history/`（最重要）

41 個 CSV、約 400 KB。刻意不進版控——它是衍生資料，理論上隨時可重建。

**但重建會痛**：資料源會限流。實測跑 18 個月的回補花了 21 分鐘，
而且結束時仍有 9 檔是「查無資料」抓不到。

所以更快的做法是**直接把整個 `data/history/` 資料夾複製過去**
（USB、雲端硬碟、scp 都行）。過去的日 K 不會變，複製過去就是對的。

要重建的話：

```bash
python3 src/history.py --months 24
python3 src/history.py --fill-gaps    # 補中間缺掉的月份
```

跑完務必看 `--status`。有標 ⚠️ 缺月份的就再跑一次 `--fill-gaps`，
中間有破洞不會報錯，但會讓指標算錯期間。

### 2. 每日排程 —— **PC 上要重做，而且做法不一樣**

`launch/install_daily.sh` 裝的是 **macOS LaunchAgent**，Windows 上不能用。

PC 上要改用工作排程器（Task Scheduler），執行的內容是：

```
每個交易日 15:00 執行  launch/daily_run.sh
```

那支 shell script 在 Windows 上要嘛用 WSL / Git Bash 跑，
要嘛照它的順序自己寫一個 .bat：

```
1. python src/history.py --months 2
2. python src/history.py --fill-gaps
3. python src/main.py --quiet
```

**順序不能反。** 模擬倉必須在歷史補齊之後才掃描，
否則會得到一個假的「今天沒有訊號」——那只代表沒東西可看。
（這是實際踩過的坑，見 `launch/daily_run.sh` 開頭的說明。）

**只能有一台機器同時跑排程。** 兩台都裝的話會各自跑各自的，
`data/paper_state.json` 只能有一台在寫，否則兩邊的持股與現金會各走各的。

### 3. `config/mail.yaml`

目前沒建立（Email 通知沒開）。要開的話照 `config/mail.example.yaml` 填。
Gmail 需要「應用程式密碼」，不是登入密碼。

### 4. `data/backups/`、`data/raw/`、log 檔

本機產物，不用搬。

---

## 還沒解決的事

### 技術項

**上櫃（TPEX）的除權息沒有自動來源。**
`src/adjust.py --fetch` 抓的 TWSE 除權息結果表只涵蓋上市。
觀察清單裡 3324（雙鴻）是上櫃，它的除息目前要手動補進
`config/corporate_actions.yaml`，格式那個檔案裡有寫。

要做的是找 TPEX 的對應端點接上去，架構已經留好了——
`fetch_twse_month()` 旁邊加一支 `fetch_tpex_month()` 回傳同樣的 `Action`。

**真實成交回報（`src/fills.py`）還沒接進主流程。**
它能把 `data/fills.csv`（券商匯出的成交明細）重播成持股與已完成的來回，
但 `main.py` / `report.py` 目前只認 `monitor.py` 的 `mset.source`
（manual / engine / both），沒有第三種「real」來源。
要接上的話至少要想清楚：`monitor.MonitorSet` 要不要多一種 source、
`report.build_report()` 的覆盤區要不要照 `mode: auto` 換成對帳語氣
（`src/report.py` 舊版本有一份 `_build_reconcile_section` 的實作可以參考，
合併時為了避免跟 `mset` 架構打架先拿掉了，邏輯還在 git 歷史裡）。

**`config/strategy.yaml` 的 `mode: auto` 目前沒有任何程式碼在讀它。**
是上面那項的前置狀態，先留著等 fills.py 接上再啟用。

### 投資決策項（比技術項重要）

回測用還原權值後的價格重跑，結論沒變：策略總報酬大幅落後於 0050 買進持有，
且交易集中在少數幾檔、同一段多頭行情，有效樣本遠少於帳面筆數。
`config/strategy.yaml` 的 objective 明文寫著要贏過 0050，
這件事在動任何策略參數之前要先想清楚。
`python3 src/backtest.py` 跑完會直接把數字逐條印出來。

---

## 給接手的 Claude Code session

開場先看這幾個地方就能接上：

1. `git log --oneline -10` —— commit message 寫的是「為什麼這樣改」，不是「改了什麼」
2. `python3 tests/run_all.py` —— 確認環境
3. `python3 src/history.py --status` —— 確認資料補齊了沒

`src/adjust.py`、`src/fills.py`、`src/monitor.py`、`src/paperdaily.py`
的模組 docstring 都寫了設計理由與踩過的坑，改之前先讀。
