# 交接筆記

> 這份檔案的用途是「下一次打開這個專案的人（很可能是幾週後的你自己）
> 五分鐘內知道現在到哪了」。不是變更紀錄——那個看 git log 就有。
> 只寫**從程式碼裡看不出來**的東西：為什麼這樣做、什麼還沒驗證、下一步卡在哪。

最後更新：2026-08-22

---

## 現在的狀態

### 分支

| 分支 | 內容 |
| --- | --- |
| `main` | `8bb75e4`，落後於下面兩條 |
| `fix/review-p0-p1-p2` | 審核修正（自動排程沒在跑、四個顯示錯誤數字的 bug）。**已合併進下面那條**，本身還沒進 main |
| `claude/auto-trading-arbitrage-reporting-dcu57l` | 目前的工作分支：桌面捷徑修正 ＋ 上面那條的合併 ＋ 監控層改接引擎部位 |

三條都還沒合回 `main`。要收斂的話順序是：工作分支 → main，`fix/review-p0-p1-p2` 的內容已經包在裡面了。

### 開發環境（Windows）

這個專案原本在 macOS 上開發，搬到 Windows 之後有幾個坑：

- **`python` 指令是空殼。** Microsoft Store 的 app execution alias，跑起來直接 exit 49。
  能用的是 **`py`**（`D:\Python\python.exe`，3.12.8）。文件裡寫的 `python3 xxx.py` 請都換成 `py xxx.py`。
- **主控台編碼是 GBK。** 測試的輸出全是中文，子行程一印就 `UnicodeEncodeError`——
  失敗的是編碼不是被測的東西。已在 `tests/run_all.py` 用 `PYTHONIOENCODING=utf-8`
  和每支測試檔的 `sys.stdout.reconfigure()` 修掉。
- **`flask` 與 `ruamel.yaml` 沒裝**，所以 `test_store.py` 會失敗、儀表板跑不起來。
  這是環境缺套件，不是程式壞掉：`py -m pip install -r requirements.txt` 就會好。
- 桌面捷徑與每日排程仍然是 macOS 專用（`.app` ＋ LaunchAgent），Windows 上還沒有對應的東西。

---

## 這一輪做了什麼：監控層改接程式交易引擎的部位

### 原本的問題

系統裡有兩套平行的東西，中間沒有連起來：

- **監控層**：`config/positions.yaml`（**手動**登記）→ `portfolio.evaluate()` → 今日頁 / 日報 / `signals.jsonl`
- **程式交易引擎**：`strategy.py` + `paper.py` → 部位在 `data/paper_state.json`

引擎每天自己進出場，但監控層完全看不到它。要在畫面上看到引擎的部位，
得手動把每一筆成交抄進 YAML——而**抄漏一筆不會有任何徵兆**：
損益、停損線、投組總覽照樣算得出數字，只是全部是錯的。

### 怎麼解的

`config/positions.yaml` 最上面加一個 `source` 開關：`manual` / `engine` / `both`。
新的 `src/monitor.py` 依它決定這一輪監控誰，並把引擎的 `PaperPosition`
轉成監控層看得懂的 `portfolio.Position`。**目前設定是 `engine`。**

### 一個關鍵決定：停損線用誰的規則

引擎部位的停損停利價**一律由引擎自己算**（`strategy.exit_levels()`），
不套用 `strategy.yaml` 的 `rules` 百分比，個股例外規則（`overrides`）也不套用。

這是整件事最容易做錯的地方。兩套參數本來就獨立：
監控層預設停損 10%，引擎是 8% 或 2×ATR。如果讓監控層用自己的百分比重算，
畫面上會出現一條**引擎明天根本不會照它執行**的停損線——
那比沒有停損線更危險，因為它看起來完全正確，不會有任何錯誤訊息。

實作上是給 `Evaluation` 加了 `stop_override` / `target_override` / `basis_label`，
`stop_price` / `target_price` 優先用它們。
`tests/test_monitor.py` 裡「核心回歸」那一段就是在守這件事，
而且附了一個對照組（同樣的價格用監控層百分比算會是「續抱」），
用來證明那個測試真的有在測東西，而不是碰巧通過。

### 其他跟著改的

- **`main.py` 的執行順序反過來了**：引擎先跑，監控層後跑。
  `source=engine` 時監控的是引擎的帳本，先評估再跑引擎的話，
  今天早上剛成交的那幾筆不會出現在今天的報告裡。
- **`signals.jsonl` 每一列多了 `source`**，`load_peaks()` 會跳過 `engine` 的列。
  不跳的話，引擎部位的收盤價會污染手動持股的移動停損基準
  （同一個代號、不同的帳）。舊紀錄沒有這個欄位，一律當 `manual`——
  那時候還沒有引擎部位，這個預設是對的。
- **`engine` 模式下「持股異動」表單被停用**（`webapp/app.py` 的 `_manual_edit_blocked()`）。
  不擋的話表單還是會寫進 `positions.yaml`，但畫面完全不會變。
- **今日頁與日報對引擎部位換了措辭**：不是「等你人工確認」，
  而是「它會在下一個交易日開盤自動執行」。還會顯示引擎已排定的明日委託、
  以及時間出場還剩幾根 K 線。
- `main.py` 與 `webapp/app.py` 各有一份幾乎一樣的「建 Position → 評估」迴圈，
  合併成 `monitor.evaluate_all()`。（`load_peaks` 上一輪已經做過同樣的收斂。）

### ⚠️ 沒有接任何券商

`source: engine` 只是換了**監控對象**。引擎的「自動進出場」全部發生在
`data/paper_state.json` 裡，成交價是模擬出來的，**沒有任何一張單真的送出去**。
README 的「尚未做」那一節仍然成立。

---

## 驗證到什麼程度

跑得過的：

- `py tests/run_all.py` → 7 支裡 6 支通過。
  唯一失敗的 `test_store.py` 是 `ruamel.yaml` 沒裝，跟這次的改動無關。
- `tests/test_monitor.py`（新增，約 40 項）全通過：來源切換、成本含手續費、
  pct/ATR 兩種停損、ATR 缺值退回百分比、委託與時間出場的備註、
  峰值不互相污染、查無行情不中斷。
- 所有 Jinja 模板編譯通過；「今日」頁在 `engine` 模式下用假資料**實際渲染過**，
  確認引擎橫幅、明日委託、停用的持股異動都出現，而且
  「下單與否由你決定」那句話**沒有**出現在引擎部位上。
- 日報用同一份假資料產出過一次，人工讀過全文。

**沒驗證的：**

- **真的把儀表板跑起來看**。這台機器沒裝 flask，只能用 jinja2 直接渲染，
  所以 CSS 實際長什麼樣（新增的 `.engine-banner`、`.order-list`）沒有人眼看過。
- **`main.py` 完整跑一次**。它會連外網抓全市場行情，這次沒跑。
  引擎先跑、監控層後跑的新順序只有讀過程式碼，沒有端到端跑過。
- **引擎真的有部位時的畫面**。目前 `paper_state.json` 是空的（0 檔），
  所以線上看到的會是「引擎目前沒有持有任何部位」那個空狀態。

裝好套件之後建議第一件事就是這三個：

```
py -m pip install -r requirements.txt
py src/main.py --dry-run     # 不寫檔，確認新順序跑得完
py webapp/app.py             # 開儀表板看 engine 模式的畫面
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

### 這台機器目前的狀態

- 套件裝好（flask 3.1.3、ruamel.yaml 0.19.1），`py tests/run_all.py` 7 支全過，連跑三輪穩定
- 歷史日 K：00919 / 2603 / 2330 各 478 根，涵蓋 24 個月
- 每日排程已安裝並手動觸發成功一次（產出 `2026-08-21.md`，模擬倉前進到 08-21）
- 桌面上有「投資儀表板」「投資工具箱」兩個捷徑
- `.claude/settings.local.json` 加了 Stop / Notification hook，工作完成會跳桌面通知
  （個人設定，已在 `.gitignore` 排除）

**以上全部還在工作區，沒有 commit。**

---

## 接下來可以做什麼

按「現在最痛」排序，不是按難度：

1. **把三條分支收斂回 `main`**。現在的狀態很容易讓下一次改動基於錯的起點。
   這是目前最痛的一項——上面那批 Windows 修正還全部躺在工作區沒進版控。
2. **`both` 模式的實際體驗**。程式支援了，但沒有真的用它跑過一天，
   同一檔股票在兩邊都有時的畫面只有測試看過。
3. **引擎真的有部位時的畫面**。`paper_state.json` 目前仍是 0 檔，
   線上看到的還是空狀態。等模擬倉真的進場一次再看一次。
4. **`store.py` 的原子寫入沒有跨行程鎖**（`save_doc`、`_write_cache`）。
   共用固定的 `.tmp` 檔名，跟 `history.py` 修掉的是同一類問題，
   只是撞上的機會小得多（單一行程、寫入視窗短）。儀表板和排程同時寫
   `quotes_cache.json` 時理論上會踩到。

再往後就是 README「尚未做」列的那些（真實下單、研究筆記接真實資訊源），
那是完全不同的風險等級，先累積幾個月模擬績效再說。
