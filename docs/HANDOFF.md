# 換一台機器接手

這份是給「在另一台電腦上把這個專案接下去」用的。
程式碼 git 上都有，但有幾樣東西**不會跟著 git 走**，那才是會卡住的地方。

最後更新：2026-08-29

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

---

## 2026-08-22：Windows 環境實際跑起來了

上一輪列的「沒驗證的」三項，前兩項做完了：**儀表板真的開起來看過**
（六個路由都回 200），**`main.py` 完整跑過一次**（`--dry-run` 與排程各一次）。
第三項（引擎有部位時的畫面）還是空的，因為引擎目前就是 0 檔。

過程中撞到的幾乎都是同一類問題：**這個專案在 macOS 上寫的，很多地方假設了 POSIX。**

### 修掉的

| 問題 | 症狀 | 修法 |
| --- | --- | --- |
| `requirements.txt` 沒有 BOM | `pip install -r` 用 cp936 讀中文註解直接 `UnicodeDecodeError`，套件一個都裝不了 | 加 PEP 263 的 `# -*- coding: utf-8 -*-` |
| 五個進入點沒有 `reconfigure` | `py src/main.py` 印到 ⚠️ 就 `UnicodeEncodeError` 中斷，報告只出來半截 | 比照 `tests/` 的做法補上（`00c43f2` 當時只修了測試） |
| `history.py` 在 Windows 沒有跨行程鎖 | `fcntl` 不存在時只剩執行緒鎖，兩個行程還共用同一個 `.tmp` 檔名 | 加 `msvcrt` 檔案鎖、暫存檔名帶 PID、`os.replace` 重試 |
| 限速器測試 flaky | 約每三次紅一次，紅的是 Windows `sleep` 的 15.6ms 抖動不是限速器 | 測試間隔 0.05 → 0.2 秒，讓抖動相對可忽略 |
| 儀表板查排程查錯平台 | 只看 macOS 的 LaunchAgent plist，Windows 上永遠回「沒裝」 | `_schedule_installed()` 依平台分流，Windows 查 `schtasks` |

那個檔案鎖的洞值得多講一句：README 明講「手動回補撞上 15:00 排程是安全的」，
在 Windows 上**那句話原本是假的**。而且它壞的方式很難發現——
`os.replace` 丟 `PermissionError` 算好的，更糟的是兩邊各自把同一個 `.tmp`
搬過去，結果檔案是完整的、內容是錯的，不會有任何錯誤訊息。

### 新增的 `launch/win/`

macOS 那套 `.app` ＋ LaunchAgent 的對應物：

```
安裝.bat / install.ps1          裝套件 → 跑測試 → 建桌面捷徑 → 問要不要補歷史和開排程
投資儀表板.bat / dashboard.ps1   沒在跑就啟動，已經在跑就開分頁
投資工具箱.bat / toolbox.ps1     編號選單，長工作就在同一個視窗裡跑
install_daily.ps1               工作排程器版的每日排程（工作名 InvestmentDailyUpdate）
daily_run.ps1                   排程實際執行的包裝，負責把輸出接進 data/daily.log
_lib.ps1                        共用：路徑解析、Python 偵測、UTF-8 主控台、啟動儀表板
```

三個 Windows 特有的坑，改的時候別踩回去：

1. **`.ps1` 一定要存成 UTF-8 with BOM。** Windows PowerShell 5.1 沒有 BOM
   就當 ANSI 讀，整份中文變亂碼。
2. **`.bat` 的內容必須全 ASCII。** cmd.exe 用 OEM 編碼（這台是 GBK）解析批次檔內容，
   中文路徑會變亂碼然後「找不到檔案」。所以 `.ps1` 用 ASCII 檔名，
   中文只留在使用者看得到的 `.bat` 檔名上。
3. **別把 `TcpClient` ＋ `cmd.exe` 重導向 ＋ `-WindowStyle Hidden` 湊在一起。**
   那是 PowerShell reverse shell 的招牌組合，Windows Defender 會把整個 `.ps1`
   判成惡意程式並鎖住檔案（實際發生過一次）。查連接埠改用
   `Get-NetTCPConnection`，啟動子行程直接用 `Start-Process` 分別導向兩個檔。

---

## 2026-09-01：排程斷線後補跑的教訓 + 跨機器同步自動化

`InvestmentDailyUpdate` 從 8/22 起沒有真正跑完過，9/1 才恢復。過程中踩了兩個坑，
之後接手的人（或 Claude session）遇到「排程斷了一陣子」時，照這個順序處理：

**1. 工作排程器被中止（結束碼 267014 / `SCHED_S_TASK_TERMINATED`）不是只有電池一種原因。**
上一輪只修了 `DisallowStartIfOnBatteries`/`StopIfGoingOnBatteries`，結果排程還是被中止過一次
（`daily_run.ps1` 連 log 的第一行都沒寫到就被砍）。後來確認 `WakeToRun` 是關的——
電腦如果在排定時間處於睡眠，工作排程器不會喚醒它，任務直接跳過或被中止。
已改成打開。如果之後還是偶發性中止，下一個該查的是 `Principal.LogonType`
（目前是 `Interactive`，需要有登入的工作階段；`Password` 類型不論登入與否都會執行，
但需要在 `Set-ScheduledTask` 時輸入一次 Windows 密碼，不能純腳本代勞）。

**2. `main.py` 沒有逐日補跑機制，排程斷線超過一天會憑空跳過中間的交易日。**
`trade_date = datasource.market_date(quotes) or date.today()`——它永遠只處理
「資料源目前給的那一天」，不會知道自己漏跑了幾天。9/1 排程恢復時，
`paper_state.json` 的 `last_date` 還停在 8/21，資料源給的是 8/31，
於是引擎直接把 8/22～8/30 這 9 個交易日的訊號全部跳過，
2881 那筆 8/21 決定的委託單也因此用 8/31（而非正確的 8/24 開盤）的價格成交。

**補救方式**（已經做過一次，之後排程再斷線可以照搬）：
1. 用 `git checkout -- data/paper_state.json data/paper_equity.jsonl data/paper_runs.jsonl`
   把這幾個檔案復原回最後一次 commit（斷線前）的狀態——前提是排程搶跑的結果
   還沒 push，還來得及復原。
2. 確認 `data/history/` 已經補齊斷線期間的日 K（`history.py --months N` + `--status` 檢查無缺口）。
3. 寫一支一次性腳本，讀 `paper_state.json` 的 `last_date`，
   找出所有 `> last_date` 且早於「今天」的交易日，依序呼叫 `paper.run_day()`
   逐日推進（每天呼叫 `append_trade`/`append_equity`/`save_state`/`append_run`），
   而不是讓 `main.py` 一次跳過去。
4. 補跑完再 commit + push。

**TWSE 封鎖是真的會反覆發生的**，不是只有 8/27 那次。這次的「全部 54 檔查無資料」
一開始被誤判成程式或資料問題，後來用 `history.py --self-test`
（唯一會印出真正 HTTP 狀態碼/例外訊息的路徑，其他地方失敗都被 `detect_market()`
吞掉變成「查無資料」）才確認是封鎖。之後遇到大量「查無資料」，
第一步永遠是先跑 `--self-test`，不要急著調大 `--months` 或加平行度硬撞。

**跨機器同步已經自動化，不用再手動複製 `paper_state.json`。**
`launch/win/daily_run.ps1`（Windows，決策機）跑完 `main.py` 成功後會自動
`git add` + `commit` + `push` 模擬倉相關檔案；`launch/run_dashboard.sh`（Mac，唯讀端）
開儀表板前會自動 `git pull --ff-only`。這代表下面「只能有一台機器同時跑排程」
那條規則現在有自動化保護了，但**前提是 Mac 那邊要先移除自己的 LaunchAgent**
（`bash launch/install_daily.sh --uninstall`），否則 Mac 還是會繼續自己跑、自己寫
`paper_state.json`，跟 Windows push 上來的版本衝突。

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
