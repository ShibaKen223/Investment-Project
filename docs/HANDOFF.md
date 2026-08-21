# 換一台機器接手

這份是給「在另一台電腦上把這個專案接下去」用的。
程式碼 git 上都有，但有幾樣東西**不會跟著 git 走**，那才是會卡住的地方。

最後更新：2026-08-21

---

## 目前進度

分支 `fix/review-p0-p1-p2`，最新 `d2da0c5`，工作區乾淨、全部已推送。

```bash
git clone https://github.com/ShibaKen223/Investment-Project.git
cd Investment-Project
git checkout fix/review-p0-p1-p2
```

三個 commit 的內容（每個 commit message 都寫了「為什麼」，比這份摘要詳細）：

| Commit | 做了什麼 |
| --- | --- |
| `bdcda57` | 報告頁改成渲染後的 HTML、辭典與產業地圖擴充 |
| `cb3c15f` | 修正會讓數字算錯的四件事，監控層接上程式交易 |
| `d2da0c5` | 改抓官方除權息參考價，清掉模擬倉空轉那天的假紀錄 |

---

## 在新機器上開起來

```bash
pip3 install -r requirements.txt
python3 tests/run_all.py          # 應該印「9 支測試檔全部通過 ✅」
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

Mac 這邊目前排程是裝好的，兩台都裝的話會各自跑各自的，
`data/paper_state.json` 會衝突。**建議只留一台跑排程。**

### 3. `config/mail.yaml`

目前沒建立（Email 通知沒開）。要開的話照 `config/mail.example.yaml` 填。
Gmail 需要「應用程式密碼」，不是登入密碼。

### 4. `data/backups/`、`data/raw/`、log 檔

本機產物，不用搬。裡面有一份 2026-08-21 清掉假紀錄前的模擬倉備份
（`paper_state-20260821-221619.json`），要還原才需要。

---

## 還沒解決的事

### 技術項

**上櫃（TPEX）的除權息沒有自動來源。**
`src/adjust.py --fetch` 抓的 TWSE 除權息結果表只涵蓋上市。
觀察清單裡 3324（雙鴻）是上櫃，它的除息目前要手動補進
`config/corporate_actions.yaml`，格式那個檔案裡有寫。

要做的是找 TPEX 的對應端點接上去，架構已經留好了——
`fetch_twse_month()` 旁邊加一支 `fetch_tpex_month()` 回傳同樣的 `Action`。

**`mode: auto` 還沒啟用。**
`config/strategy.yaml` 的 `mode` 目前是 `manual`。
切成 `auto` 之前要先有成交明細來源：`src/fills.py` 讀
`data/fills.csv`（格式見該檔開頭），但還沒接上實際的券商匯出檔。

### 投資決策項（比技術項重要）

回測用還原權值後的價格重跑，結論沒變：

```
策略總報酬        +43.88%
0050 買進持有    +128.72%
超額報酬          -84.84%   ❌ 輸給基準
平均曝險            24.4%
完全空手            35.6%   的交易日
```

`config/strategy.yaml` 的 objective 明文寫著「總報酬需勝過 0050 買進持有」，
**目前大幅落敗**。而且 52 筆交易只分布在少數幾檔、同一段多頭行情，
有效樣本遠少於帳面筆數。

另外「最大回撤 ≤ 15%」那條門檻在 24% 曝險下等於自動過關，
它現在沒有在管任何事情。

這兩件事在動任何策略參數之前要先想清楚。
`python3 src/backtest.py` 跑完會直接把這些逐條印出來。

---

## 模擬倉現在的狀態

2026-08-21 真正開始跑，在那之前的紀錄是空轉產生的、已經清掉。

```
現金        1,000,000
持股        無
待執行委託   買進 2881 一張
            （站上 60 日均線 124.25、突破前 20 日高點 132.50）
```

接手的機器跑下一次 `src/main.py` 時，這筆委託會以當天開盤價成交。

**`data/paper_state.json` 只能有一台機器在寫。** 兩台同時跑排程的話，
兩邊的持股與現金會各走各的，而且都是錯的。

想確認模擬倉到底跑過幾次、每次看到什麼，看 `data/paper_runs.jsonl`：

```json
{"ran_at":"2026-08-21T22:17:37","trade_date":"2026-08-21","universe":40,
 "with_bars":40,"ready":23,"short":17,"status":"ok","entry_signals":1,"orders":1}
```

`ready` 遠小於 `universe` 就代表歷史沒補齊，那天的判斷不能信。
低於 `config/paper.yaml` 的 `data_guard.min_ready_codes`（預設 10）時，
系統會直接宣告那天不算數，不會把它記掉。

---

## 給接手的 Claude Code session

開場先看這三個地方就能接上：

1. `git log --oneline -3` 再 `git show --stat` 那三個 commit ——
   commit message 寫的是「為什麼這樣改」，不是「改了什麼」
2. `python3 tests/run_all.py` —— 確認環境
3. `python3 src/history.py --status` —— 確認資料補齊了沒

`src/adjust.py`、`src/fills.py`、`src/paperdaily.py` 三支的
模組 docstring 都寫了設計理由與踩過的坑，改之前先讀。
