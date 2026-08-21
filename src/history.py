"""歷史日 K 儲存與抓取。

為什麼需要這個模組:
    datasource.py 抓的是「今天的全市場收盤」，一天一筆，沒有歷史。
    但波段策略要算均線、要看 20 日高點、要回測，都需要逐檔的日 K 序列。

儲存格式刻意選 CSV 而不是 JSON:
    data/history/{code}.csv  —  date,open,high,low,close,volume
    純文字、可以用 Excel 打開、可以用 git diff 看變化、
    出事的時候你能直接用眼睛檢查，不需要寫程式。

兩種填資料的方式（互補，可以混用）:
    1. rebuild_from_raw()  從 data/raw/ 的每日全市場存檔重建。
       格式百分之百正確（datasource.py 本來就在解析它），
       但只有系統開始跑之後的日子。
    2. fetch_twse_month()  從 TWSE 逐檔月報表補歷史。
       可以一次補幾年，但端點格式若改版就得跟著修。

日期一律用 ISO (YYYY-MM-DD)，民國年在進來的時候就轉掉。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"
RAW_DIR = ROOT / "data" / "raw"

TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_TRADING_STOCK = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
# 逾時要短。這兩支端點偶爾會卡住不回應，配上重試的話
# 單一個月最壞情況會拖到 timeout × RETRIES —— 30 秒 × 3 = 90 秒，
# 一個月就吃掉一分半，這是回補會慢到不合理的主因。
TIMEOUT = float(os.environ.get("INVEST_FETCH_TIMEOUT", "12"))
RETRIES = 3

# 兩次請求之間至少間隔多久（秒）。這是**全域**的發送頻率上限，
# 不是「等完上一個回應再等這麼久」——差別很大，見 _RateLimiter。
# 可用環境變數調整：INVEST_FETCH_DELAY=0.8 python3 src/history.py
# 調太低會被端點擋，被擋了反而更慢，不建議低於 0.5。
POLITE_DELAY = float(os.environ.get("INVEST_FETCH_DELAY", "1.2"))

# 同時有幾檔在抓。併發是為了「蓋掉網路延遲」，不是為了提高發送頻率——
# 發送頻率仍由上面的 POLITE_DELAY 全域控管，開幾個 worker 都不會送得更密，
# 只是允許更多請求同時在路上等回應。
#
# 要蓋掉延遲，worker 數大約需要 延遲 ÷ 間隔。端點慢到每次 6 秒時，
# 6 個 worker 才勉強跟得上 1.2 秒的發送節奏；worker 太少的話，
# 瓶頸會從「發送頻率」變成「等回應」，那就白設限速器了。
#
# 預設 3：實測 5～6 個併發會讓 TWSE 開始回 read-timeout（伺服器端限流），
# 逾時再重試反而更慢，而且會留下殘缺的月份。寧可慢一點也不要製造破洞。
MAX_WORKERS = int(os.environ.get("INVEST_FETCH_WORKERS", "3"))

# 一個月「抓齊了」的判斷，刻意不寫死根數。
#
# 寫死過一次，錯得很典型：門檻設 13，但 2026 年 2 月因為農曆年
# （2/12–2/20 休市）真的只有 12 個交易日。結果每一檔都被永遠標成
# 「缺 2026-02」，補了也不會消——因為那個月本來就是完整的。
# 颱風假、補班日也會製造同樣的問題，維護一份假日表則是另一個坑。
#
# 改成從資料本身推導：所有追蹤標的中，某個月最多的那個根數，
# 就是那個月真正的交易日數（只要有任何一檔抓齊了）。自我校準，
# 不需要假日表，農曆年和颱風假都自動處理。
#
# 兩個比例分開用，因為兩件事的代價不對稱：
COMPLETE_RATIO = 0.8   # 回補判斷「這個月夠完整了嗎」——寧可多抓一次
GAP_RATIO = 0.5        # 對使用者報破洞——寧可漏報，也不要謊報永遠補不掉的洞

# 完全沒有其他標的可以比對時（只追蹤一檔）的絕對下限。
MIN_BARS_FALLBACK = 5
# 最近幾個月一律重抓：當月還在累積，上個月也可能有補登。
ALWAYS_REFRESH_MONTHS = 2

FIELDNAMES = ["date", "open", "high", "low", "close", "volume"]


@dataclass
class Bar:
    """一根日 K。"""

    date: str      # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int    # 成交股數

    @property
    def is_valid(self) -> bool:
        """高低價要合理，收盤要在區間內——防止解析錯位靜靜地污染資料。"""
        if min(self.open, self.high, self.low, self.close) <= 0:
            return False
        if self.high < self.low:
            return False
        return self.low <= self.close <= self.high


# --------------------------------------------------------------------------
# 讀寫
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 併發保護
# --------------------------------------------------------------------------
# save_bars 是「讀出全部 → 合併 → 整個寫回」，沒有保護的話有兩種壞法:
#
#   1. 檔案被清空。open("w") 會立刻截斷檔案再重寫，
#      這期間被中斷或被別人讀到，就是一個空檔或半截檔——
#      你辛苦補的好幾個月歷史會直接消失，這比更新遺失嚴重得多。
#   2. 更新遺失。兩個行程各自讀到舊內容、各自合併、後寫的蓋掉先寫的。
#
# 這不是理論問題。實際會撞到的情況至少有兩種:
#   · 手動跑 src/history.py 回補時，剛好碰上 15:00 的每日排程
#     （main.py → paperdaily.ingest_quotes → save_bars 寫同一批檔案）
#   · 兩個回補行程並行
#
# 解法分兩層：原子寫入解決第 1 種，檔案鎖解決第 2 種。

try:
    import fcntl
except ImportError:      # Windows 沒有 fcntl
    fcntl = None         # type: ignore[assignment]

# 同一個行程內的執行緒用這把鎖。寫檔只有幾毫秒、抓資料要好幾秒，
# 所以用一把全域鎖就夠，不值得為了每檔一把鎖增加複雜度。
_write_lock = threading.Lock()


@contextlib.contextmanager
def _exclusive(code: str):
    """取得某檔的獨佔寫入權，跨執行緒也跨行程。

    鎖檔是獨立的一個檔案，不是 CSV 本身——因為原子寫入會用
    os.replace 換掉 CSV 的 inode，鎖在被換掉的 inode 上等於沒鎖。
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = HISTORY_DIR / f".{code}.lock"
    with _write_lock:
        if fcntl is None:
            yield                      # Windows：只有執行緒層級的保護
            return
        with lock_file.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write_atomic(path: Path, rows: list[Bar]) -> None:
    """先寫暫存檔再一次換過去。

    os.replace 在同一個檔案系統內是原子操作：讀的人要嘛看到舊的完整檔案，
    要嘛看到新的完整檔案，不會看到寫到一半的狀態。
    寫的過程中當掉的話，原本的檔案也還在。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for bar in rows:
            writer.writerow(asdict(bar))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def history_path(code: str) -> Path:
    return HISTORY_DIR / f"{code}.csv"


def load_bars(code: str) -> list[Bar]:
    """讀出某檔的完整日 K，依日期排序。檔案不存在回傳空 list。"""
    path = history_path(code)
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                bars.append(
                    Bar(
                        date=row["date"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row["volume"] or 0)),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue  # 壞掉的一行不該讓整個檔案讀不出來
    bars.sort(key=lambda b: b.date)
    return bars


def load_bars_adjusted(code: str) -> list[Bar]:
    """讀出**還原權值後**的日 K —— 做分析的都該用這支。

    load_bars() 回傳的是檔案裡的原始價格，也就是當天真正成交的數字。
    那是磁碟上的事實，寫檔時必須用它；但拿來算均線、期間報酬、
    停損停利就會出事，因為除權息與股票分割會在序列裡留下假斷崖
    （0050 在 2025-06-18 是一根 -74.8% 的 K，實際上是 1 拆 4）。

    為什麼不直接讓 load_bars 預設還原：save_bars() 內部會先 load 再合併寫回，
    如果 load 出來是還原後的價格，第一次存檔就會把還原值寫進檔案，
    之後每存一次再還原一次——原始資料就永久毀了。
    所以「寫檔用原始、分析用還原」這條界線要很清楚。

    公司行為登記在 config/corporate_actions.yaml，見 src/adjust.py。
    """
    import adjust

    bars = load_bars(code)
    if not bars:
        return bars
    actions = adjust.load_actions().get(code)
    if not actions:
        return bars
    return adjust.apply_actions(bars, actions)


def save_bars(code: str, bars: list[Bar]) -> int:
    """合併寫入。同一天以新資料覆蓋，回傳寫入後的總筆數。

    合併而不是覆寫，是為了讓「從 raw 重建」和「從 TWSE 補歷史」
    可以混用，兩邊各補各的區間不會互相清掉。

    ⚠️ 這裡的 load_bars() 一定要是**未還原**的版本。
       寫回檔案的必須是當天實際成交的價格，還原只發生在讀出來做分析的時候。
    """
    with _exclusive(code):
        # 讀取也要在鎖內：讀完才合併，中間不能有別人插進來寫，
        # 否則對方的資料會被我們手上的舊快照蓋掉。
        merged: dict[str, Bar] = {b.date: b for b in load_bars(code)}
        for bar in bars:
            if bar.is_valid:
                merged[bar.date] = bar

        ordered = [merged[d] for d in sorted(merged)]
        _write_atomic(history_path(code), ordered)
    return len(ordered)


def available_codes() -> list[str]:
    if not HISTORY_DIR.exists():
        return []
    # 排除 .tmp 與 .lock 之類的內部檔案，它們不是股票代號
    return sorted(
        p.stem for p in HISTORY_DIR.glob("*.csv") if not p.name.startswith(".")
    )


def _months_between(start: str, end: str) -> list[tuple[int, int]]:
    """涵蓋 start 到 end 的所有 (年, 月)，含頭尾。"""
    y, m = int(start[:4]), int(start[5:7])
    end_y, end_m = int(end[:4]), int(end[5:7])
    out: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def trading_calendar(codes: list[str] | None = None) -> dict[tuple[int, int], int]:
    """每個月實際有幾個交易日，從已存的歷史資料推導出來。

    取所有追蹤標的中該月的最大根數。某一檔可能停牌、可能抓漏，
    但只要有任何一檔抓齊了，最大值就是那個月真正的交易日數。

    這樣就不需要維護假日表：農曆年、颱風假、補班日全都自動反映在資料裡。
    """
    counts: dict[tuple[int, int], int] = {}
    for code in codes if codes is not None else available_codes():
        per_month: dict[tuple[int, int], int] = {}
        for bar in load_bars(code):
            key = (int(bar.date[:4]), int(bar.date[5:7]))
            per_month[key] = per_month.get(key, 0) + 1
        for key, value in per_month.items():
            counts[key] = max(counts.get(key, 0), value)
    return counts


def _month_counts(bars: list[Bar]) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for bar in bars:
        key = (int(bar.date[:4]), int(bar.date[5:7]))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _expected_for(
    month: tuple[int, int],
    calendar: dict[tuple[int, int], int],
    own_counts: dict[tuple[int, int], int],
) -> int:
    """那個月應該要有幾根 K。

    優先用跨標的推導出來的交易日曆；沒有可比對的資料時（例如只追蹤一檔），
    退回用這一檔自己各月份的中位數當基準。
    """
    expected = calendar.get(month, 0)
    if expected:
        return expected
    values = sorted(v for v in own_counts.values() if v > 0)
    if not values:
        return 0
    return values[len(values) // 2]


def gaps_in(
    bars: list[Bar],
    calendar: dict[tuple[int, int], int] | None = None,
) -> list[tuple[int, int]]:
    """從一串 K 棒找出中間缺掉的月份。

    跟 find_gaps() 的差別只在資料來源：這支吃記憶體裡的資料，
    讓已經把 bars 拿在手上的呼叫端不必再讀一次磁碟
    （也才能對還沒存檔的資料做檢查）。
    """
    if len(bars) < 2:
        return []

    counts = _month_counts(bars)
    calendar = calendar if calendar is not None else {}

    # 頭尾兩個月本來就可能是部分月份（剛開始追蹤、當月還沒過完），
    # 所以只檢查中間的月份。
    interior = _months_between(bars[0].date, bars[-1].date)[1:-1]

    gaps: list[tuple[int, int]] = []
    for month in interior:
        expected = _expected_for(month, calendar, counts)
        floor = max(expected * GAP_RATIO, MIN_BARS_FALLBACK) if expected else 0
        if floor and counts.get(month, 0) < floor:
            gaps.append(month)
    return gaps


def find_gaps(code: str) -> list[tuple[int, int]]:
    """找出某檔「已涵蓋範圍之內」缺資料的月份。

    為什麼重要：months_needed() 只看你這次要求的區間，
    更早的殘缺月份（例如上一輪抓到一半被中斷留下的）永遠不會被發現。
    檔案看起來涵蓋兩年，中間卻是空的。

    這種破洞不會報錯，但會讓「往回數 N 根」的指標算錯——
    有洞的話，往回 250 根拿到的可能是兩年前的價格，
    「一年報酬」就變成了兩年報酬，而報告上仍然寫著一年。

    頭尾兩個月本來就可能是部分月份（剛開始追蹤、當月還沒過完），
    所以只檢查中間的月份。
    """
    return gaps_in(load_bars(code), trading_calendar())


def coverage(code: str) -> tuple[str, str, int] | None:
    """回傳 (最早日期, 最晚日期, 筆數)，沒資料回傳 None。"""
    bars = load_bars(code)
    if not bars:
        return None
    return bars[0].date, bars[-1].date, len(bars)


# --------------------------------------------------------------------------
# 來源 1：從 data/raw/ 的每日全市場存檔重建
# --------------------------------------------------------------------------


def _roc_to_iso(roc: str) -> str | None:
    """'1150728' 或 '115/07/28' -> '2026-07-28'。無法解析回傳 None。"""
    text = str(roc).strip().replace("/", "")
    if len(text) != 7 or not text.isdigit():
        return None
    return f"{int(text[:3]) + 1911:04d}-{text[3:5]}-{text[5:7]}"


def _num(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("+", "")
    if text in ("", "--", "---", "null", "N/A", "X"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def rebuild_from_raw(codes: set[str] | None = None) -> dict[str, int]:
    """掃過 data/raw/ 所有存檔，把指定代號的日 K 抽出來寫進 history/。

    codes=None 代表把 raw 裡出現過的所有代號都建起來（檔案會很多）。
    回傳 {代號: 總筆數}。
    """
    if not RAW_DIR.exists():
        return {}

    collected: dict[str, list[Bar]] = {}

    for path in sorted(RAW_DIR.glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(rows, list):
            continue

        is_tpex = path.name.endswith("_tpex.json")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if is_tpex:
                code = str(row.get("SecuritiesCompanyCode", "")).strip()
                keys = ("Open", "High", "Low", "Close", "TradingShares")
            else:
                code = str(row.get("Code", "")).strip()
                keys = (
                    "OpeningPrice",
                    "HighestPrice",
                    "LowestPrice",
                    "ClosingPrice",
                    "TradeVolume",
                )
            if not code or (codes is not None and code not in codes):
                continue

            iso = _roc_to_iso(row.get("Date", ""))
            values = [_num(row.get(k)) for k in keys]
            if iso is None or any(v is None for v in values[:4]):
                continue

            bar = Bar(
                date=iso,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=int(values[4] or 0),
            )
            if bar.is_valid:
                collected.setdefault(code, []).append(bar)

    return {code: save_bars(code, bars) for code, bars in collected.items()}


# --------------------------------------------------------------------------
# 來源 2：TWSE 逐檔月報表
# --------------------------------------------------------------------------
# ⚠️ 這兩支端點都是「網頁後端 API」，不是 openapi.twse.com.tw 那種正式開放資料，
#    改版時路徑或欄位名稱可能變動。解析刻意用「欄位名稱比對」而不是寫死索引，
#    就是為了讓改版時比較不容易靜靜地解析錯位。
#    如果哪天壞了，先用 --self-test 看它抓回來的 fields 長什麼樣子。
#
#    已知的回應格式差異（TPEx 有三個地方跟 TWSE 不一樣，每個都會靜靜地算錯）:
#      1. 資料包在 tables[0]["data"] 裡，不是頂層的 "data"
#      2. 欄位叫「開盤/最高/最低/收盤」，沒有「價」字
#      3. 成交量的單位是**張**不是股 —— 要 ×1000 才能跟 TWSE 對齊


def _squash(text: object) -> str:
    """把欄位名稱裡的所有空白拿掉再比對。

    這不是防禦性過頭 —— TPEx 實際回傳的日期欄位就叫「日 期」，中間有一個空格，
    同一份回應裡其他欄位卻沒有。子字串比對會因此找不到日期欄，
    然後整個月的資料靜靜地變成零根 K：不會拋例外、不會有錯誤訊息，
    只會讓你的均線少了一段。全形空格（\u3000）同樣要清掉。
    """
    return "".join(str(text).split()).replace("\u3000", "")


class _RateLimiter:
    """全域發送頻率上限。

    原本的寫法是「送出請求 → 等回應 → sleep 1.5 秒 → 下一個」，
    總時間會是 次數 ×（網路延遲 + 1.5 秒）。端點慢的時候延遲才是大頭，
    sleep 完全不是瓶頸——99 個月因此跑了八分多鐘，而不是預估的 2.6 分鐘。

    這個類別把兩件事分開:
        「多久發一次請求」由這裡統一控管（對端點的禮貌）
        「等回應」交給併發去蓋掉（我們自己的效率）
    總時間變成 max(次數 × 間隔, 網路延遲)，跟延遲幾乎脫鉤。
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if delay > 0:
            time.sleep(delay)


_limiter = _RateLimiter(POLITE_DELAY)


def _request_json(url: str, params: dict) -> dict:
    """送出一次請求並回傳 JSON。頻率由 _limiter 控管，失敗會重試。

    重試之間用遞增的等待（1×、2× 間隔），避免在端點正忙的時候
    用同樣的節奏一直撞上去。
    """
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        _limiter.wait()
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < RETRIES:
                time.sleep(POLITE_DELAY * attempt)
    raise RuntimeError(str(last_error)) from last_error


def _field_index(fields: list[str], *keywords: str) -> int | None:
    """在 fields 裡找第一個包含任一關鍵字的欄位位置（忽略空白）。"""
    squashed_keywords = [_squash(kw) for kw in keywords]
    for idx, name in enumerate(fields):
        text = _squash(name)
        if any(kw in text for kw in squashed_keywords):
            return idx
    return None


def parse_stock_day(payload: dict) -> list[Bar]:
    """解析 TWSE STOCK_DAY 的回應。格式不符就回傳空 list。"""
    if not isinstance(payload, dict):
        return []
    if str(payload.get("stat", "")).upper() not in ("OK", ""):
        return []

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not fields or not rows:
        return []

    idx_date = _field_index(fields, "日期")
    idx_open = _field_index(fields, "開盤")
    idx_high = _field_index(fields, "最高")
    idx_low = _field_index(fields, "最低")
    idx_close = _field_index(fields, "收盤")
    idx_vol = _field_index(fields, "成交股數")
    if None in (idx_date, idx_open, idx_high, idx_low, idx_close):
        return []

    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= idx_close:
            continue
        iso = _roc_to_iso(row[idx_date])
        o = _num(row[idx_open])
        h = _num(row[idx_high])
        low = _num(row[idx_low])
        c = _num(row[idx_close])
        v = _num(row[idx_vol]) if idx_vol is not None and len(row) > idx_vol else 0
        if iso is None or None in (o, h, low, c):
            continue  # 無成交日，欄位會是 '--'
        bar = Bar(date=iso, open=o, high=h, low=low, close=c, volume=int(v or 0))
        if bar.is_valid:
            bars.append(bar)
    return bars


def parse_trading_stock(payload: dict) -> list[Bar]:
    """解析 TPEx tradingStock 的回應（上櫃逐檔月報表）。

    跟 TWSE 最大的差別是資料藏在 tables[0] 裡，而且成交量以「張」計價。
    """
    if not isinstance(payload, dict):
        return []

    tables = payload.get("tables")
    block: dict = {}
    if isinstance(tables, list) and tables and isinstance(tables[0], dict):
        block = tables[0]
    elif payload.get("data"):
        block = payload  # 少數情況直接放在頂層

    fields = block.get("fields") or []
    rows = block.get("data") or []
    if not fields or not rows:
        return []

    idx_date = _field_index(fields, "日期")
    idx_open = _field_index(fields, "開盤")
    idx_high = _field_index(fields, "最高")
    idx_low = _field_index(fields, "最低")
    idx_close = _field_index(fields, "收盤")
    # 「成交張數」是張，「成交股數」是股——兩種都要能吃，單位換算才會對。
    idx_lots = _field_index(fields, "成交張數")
    idx_shares = _field_index(fields, "成交股數")
    if None in (idx_date, idx_open, idx_high, idx_low, idx_close):
        return []

    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= idx_close:
            continue
        iso = _roc_to_iso(row[idx_date])
        o = _num(row[idx_open])
        h = _num(row[idx_high])
        low = _num(row[idx_low])
        c = _num(row[idx_close])
        if iso is None or None in (o, h, low, c):
            continue

        volume = 0.0
        if idx_shares is not None and len(row) > idx_shares:
            volume = _num(row[idx_shares]) or 0.0
        elif idx_lots is not None and len(row) > idx_lots:
            volume = (_num(row[idx_lots]) or 0.0) * 1000  # 張 → 股

        bar = Bar(date=iso, open=o, high=h, low=low, close=c, volume=int(volume))
        if bar.is_valid:
            bars.append(bar)
    return bars


def fetch_tpex_month(code: str, year: int, month: int) -> list[Bar]:
    """抓某檔某個月的日 K（上櫃股票）。"""
    params = {
        "code": code,
        "date": f"{year:04d}/{month:02d}/01",
        "id": "",
        "response": "json",
    }
    try:
        return parse_trading_stock(_request_json(TPEX_TRADING_STOCK, params))
    except RuntimeError as exc:
        raise RuntimeError(
            f"抓取失敗 {code} {year}-{month:02d}（上櫃，{RETRIES} 次重試）：{exc}"
        ) from exc


def fetch_twse_month(code: str, year: int, month: int) -> list[Bar]:
    """抓某檔某個月的日 K（上市股票）。"""
    params = {
        "date": f"{year:04d}{month:02d}01",
        "stockNo": code,
        "response": "json",
    }
    try:
        return parse_stock_day(_request_json(TWSE_STOCK_DAY, params))
    except RuntimeError as exc:
        raise RuntimeError(
            f"抓取失敗 {code} {year}-{month:02d}（上市，{RETRIES} 次重試）：{exc}"
        ) from exc


def _month_sequence(months: int, today: date | None = None) -> list[tuple[int, int]]:
    """回傳最近 N 個月的 (年, 月)，由舊到新。"""
    today = today or date.today()
    year, month = today.year, today.month
    seq: list[tuple[int, int]] = []
    for _ in range(months):
        seq.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(seq))


def months_needed(
    code: str,
    months: int,
    today: date | None = None,
    force: bool = False,
) -> list[tuple[int, int]]:
    """哪些月份真的需要連線去抓。

    已經抓齊的過去月份會跳過——過去的日 K 不會再變，重抓只是浪費時間。
    這讓第二次以後的執行從好幾分鐘縮短到幾秒。
    最近兩個月一律重抓：當月還在累積，上個月也可能有事後補登。
    """
    sequence = _month_sequence(months, today)
    if force:
        return sequence

    counts = _month_counts(load_bars(code))
    calendar = trading_calendar()
    always = set(sequence[-ALWAYS_REFRESH_MONTHS:])

    needed: list[tuple[int, int]] = []
    for month in sequence:
        if month in always:
            needed.append(month)
            continue
        expected = _expected_for(month, calendar, counts)
        # 完全不知道那個月該有幾根（第一次跑，什麼資料都沒有）就抓
        if not expected or counts.get(month, 0) < expected * COMPLETE_RATIO:
            needed.append(month)
    return needed


def detect_market(code: str, year: int, month: int) -> str:
    """用一次請求判斷這檔在哪個市場掛牌。

    以前的做法是「整段 24 個月先打上市，一根都沒有再整段打上櫃」，
    上櫃股因此要付兩倍的請求數。改成先探一個月就好。
    """
    try:
        if fetch_twse_month(code, year, month):
            return "twse"
    except RuntimeError:
        pass
    try:
        if fetch_tpex_month(code, year, month):
            return "tpex"
    except RuntimeError:
        pass
    return "unknown"


def backfill(
    code: str,
    months: int = 12,
    today: date | None = None,
    market: str = "auto",
    force: bool = False,
    progress=None,
    only_months: list[tuple[int, int]] | None = None,
) -> tuple[int, str, int]:
    """補齊某檔最近 N 個月的日 K。

    only_months 有給就只抓那些月份（補洞用），忽略 months 與 force。

    回傳 (資料總筆數, 實際用的市場, 這次連線抓了幾個月)。

    每抓完一個月就存檔，不是全部抓完才存——
    中途 Ctrl+C 或斷線時，已經抓到的不會消失，重跑會從缺的地方接下去。
    """
    todo = list(only_months) if only_months else months_needed(
        code, months, today, force
    )
    if not todo:
        bars = load_bars(code)
        return len(bars), "已是最新", 0

    if market == "auto":
        detected = detect_market(code, *todo[-1])   # 用最近的月份探，最可能有資料
        if detected == "unknown":
            return len(load_bars(code)), "查無資料", 0
        market = detected

    fetch = fetch_twse_month if market == "twse" else fetch_tpex_month
    label = "上市" if market == "twse" else "上櫃"

    fetched = 0
    for year, month in todo:
        # 這裡不再 sleep —— 發送頻率由 _limiter 全域控管，
        # 在這裡等只會把併發的效果抵銷掉。
        try:
            bars = fetch(code, year, month)
        except RuntimeError as exc:
            if progress:
                progress(code, year, month, None, str(exc))
            continue
        if bars:
            save_bars(code, bars)      # 每個月存一次 → 可中斷、可續傳
            fetched += 1
        if progress:
            progress(code, year, month, len(bars), None)

    return len(load_bars(code)), label, fetched


def backfill_many(
    codes: list[str],
    months: int = 12,
    today: date | None = None,
    market: str = "auto",
    force: bool = False,
    workers: int = MAX_WORKERS,
    progress=None,
    plan: dict[str, list[tuple[int, int]]] | None = None,
) -> dict[str, tuple[int, str, int]]:
    """同時回補多檔。回傳 {代號: (總筆數, 市場, 這次抓了幾個月)}。

    併發是跨「標的」而不是跨「月份」:
      · 每檔寫自己的 CSV，不會互相踩到
      · 同一檔的月份仍照順序抓，出錯時比較好判斷是哪一段有問題

    發送頻率仍由 _limiter 全域控管，所以開幾個 worker 都不會讓
    對端點的請求變密——只是把等回應的時間疊在一起而已。
    """
    if not codes:
        return {}

    results: dict[str, tuple[int, str, int]] = {}
    lock = threading.Lock()

    def one(code: str) -> None:
        try:
            outcome = backfill(
                code, months, today, market, force, progress,
                only_months=(plan or {}).get(code),
            )
        except RuntimeError as exc:
            with lock:
                results[code] = (len(load_bars(code)), f"失敗：{exc}", 0)
            return
        with lock:
            results[code] = outcome

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, codes))

    return results


# --------------------------------------------------------------------------
# 命令列
# --------------------------------------------------------------------------


def universe_from_config() -> list[str]:
    """從 config/positions.yaml 取出「持股 + 觀察清單」的代號。"""
    import yaml

    path = ROOT / "config" / "positions.yaml"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    codes: list[str] = []
    for entry in raw.get("positions") or []:
        code = str(entry.get("code", "")).strip()
        if code and code not in codes:
            codes.append(code)
    for entry in raw.get("watchlist") or []:
        code = str(entry.get("code", "")).strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="歷史日 K 管理。預設對象是持股 + 觀察清單。"
    )
    parser.add_argument(
        "--codes",
        help="逗號分隔的股票代號；省略則用 config/positions.yaml 裡的持股與觀察清單",
    )
    parser.add_argument(
        "--months", type=int, default=12,
        help="回補幾個月（預設 12）。已經有資料的月份會自動跳過"
    )
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="改用 data/raw/ 的每日存檔重建，不連外網",
    )
    parser.add_argument(
        "--status", action="store_true", help="只顯示目前每檔的資料涵蓋範圍"
    )
    parser.add_argument(
        "--fill-gaps",
        action="store_true",
        help="只補「已涵蓋範圍之內」缺掉的月份（上次抓到一半留下的破洞）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"同時抓幾檔（預設 {MAX_WORKERS}）。發送頻率仍由全域限制器控管",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="連已經有資料的月份也重抓（預設會跳過，只補缺的）",
    )
    parser.add_argument(
        "--market",
        choices=["auto", "twse", "tpex"],
        default="auto",
        help="指定市場。預設 auto：先試上市，抓不到再試上櫃",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="對上市與上櫃各抓一次並印出實際欄位，確認端點格式沒改版",
    )
    args = parser.parse_args()

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else universe_from_config()
    )

    if args.self_test:
        ok = True
        today = date.today()
        probe = f"{today.year:04d}{today.month:02d}01"

        print("=== 上市 TWSE STOCK_DAY（2330）===")
        try:
            resp = requests.get(
                TWSE_STOCK_DAY,
                params={"date": probe, "stockNo": "2330", "response": "json"},
                timeout=TIMEOUT,
            )
            payload = resp.json()
            print(f"HTTP {resp.status_code}  stat={payload.get('stat')!r}")
            print(f"fields = {payload.get('fields')}")
            parsed = parse_stock_day(payload)
            print(f"解析出 {len(parsed)} 根 K；第一根: {parsed[0] if parsed else '（無）'}")
            if not parsed:
                ok = False
                print("⚠️  解析不出來，欄位名稱可能已改版。把上面的 fields 貼出來就能修。")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"❌ 失敗：{exc}")

        print()
        print("=== 上櫃 TPEx tradingStock（6488）===")
        try:
            resp = requests.get(
                TPEX_TRADING_STOCK,
                params={
                    "code": "6488",
                    "date": f"{today.year:04d}/{today.month:02d}/01",
                    "id": "",
                    "response": "json",
                },
                timeout=TIMEOUT,
            )
            payload = resp.json()
            tables = payload.get("tables") or []
            block = tables[0] if tables and isinstance(tables[0], dict) else payload
            print(f"HTTP {resp.status_code}")
            print(f"fields = {block.get('fields')}")
            parsed = parse_trading_stock(payload)
            print(f"解析出 {len(parsed)} 根 K；第一根: {parsed[0] if parsed else '（無）'}")
            if not parsed:
                ok = False
                print("⚠️  解析不出來。TPEx 端點改版較頻繁，把 fields 貼出來就能修。")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"❌ 失敗：{exc}")

        print()
        print("✅ 兩個市場都正常。" if ok else "⚠️ 有市場抓不到，見上面的訊息。")
        return 0 if ok else 1

    if args.status:
        if not codes:
            codes = available_codes()
        holed: list[str] = []
        for code in codes:
            info = coverage(code)
            if info is None:
                print(f"{code}: （無資料）")
                continue
            start, end, count = info
            gaps = find_gaps(code)
            if gaps:
                holed.append(code)
                shown = "、".join(f"{y}-{m:02d}" for y, m in gaps[:6])
                more = f" 等 {len(gaps)} 個月" if len(gaps) > 6 else ""
                print(f"{code}: {start} ~ {end}  共 {count} 根  ⚠️ 缺 {shown}{more}")
            else:
                print(f"{code}: {start} ~ {end}  共 {count} 根")
        if holed:
            print()
            print("⚠️  上面標記的標的中間有缺月份。這不會報錯，但會讓指標算錯——")
            print("    「往回數 250 根」拿到的可能是兩年前的價格，")
            print("    報告上卻仍寫著「一年報酬」。")
            print()
            print("    補起來：python3 src/history.py --fill-gaps")
        return 0

    if args.fill_gaps:
        if not codes:
            codes = available_codes()
        gap_plan = {c: find_gaps(c) for c in codes}
        gap_plan = {c: v for c, v in gap_plan.items() if v}
        if not gap_plan:
            print("所有標的的歷史都是連續的，沒有破洞要補。")
            return 0

        total = sum(len(v) for v in gap_plan.values())
        print(f"要補 {total} 個缺漏的月份（{len(gap_plan)} 檔）：")
        for code, months_list in sorted(gap_plan.items()):
            shown = "、".join(f"{y}-{m:02d}" for y, m in months_list[:6])
            more = f" 等 {len(months_list)} 個月" if len(months_list) > 6 else ""
            print(f"  {code}: {shown}{more}")
        print()

        started = time.monotonic()

        def gap_progress(code, year, month, count, error):
            if error:
                print(f"  {code} {year}-{month:02d}  失敗：{error}", flush=True)
            else:
                print(f"  {code} {year}-{month:02d}  {count} 根", flush=True)

        results = backfill_many(
            list(gap_plan),
            months=0,
            market=args.market,
            workers=max(1, min(args.workers, len(gap_plan))),
            progress=gap_progress,
            plan=gap_plan,
        )
        print()
        for code in sorted(results):
            left = find_gaps(code)
            status = "已補齊" if not left else f"仍缺 {len(left)} 個月"
            print(f"{code}: 共 {results[code][0]} 根（{status}）")
        print()
        print(f"總共花了 {(time.monotonic() - started) / 60:.1f} 分鐘。")
        return 0

    if not codes:
        raise SystemExit("沒有要處理的代號，請用 --codes 指定。")

    if args.from_raw:
        result = rebuild_from_raw(set(codes))
        if not result:
            print("data/raw/ 裡沒有可用的存檔。系統每天跑過之後這裡才會累積資料。")
        for code, count in sorted(result.items()):
            print(f"{code}: {count} 根（從 data/raw/ 重建）")
        return 0

    # 先算出實際要抓幾個月，才能給出誠實的時間預估。
    # 第二次以後大部分月份會被跳過，預估數字會小很多。
    plan = {c: months_needed(c, args.months, force=args.force) for c in codes}
    total_months = sum(len(v) for v in plan.values())
    pending = [c for c in codes if plan[c]]
    probes = len(pending) if args.market == "auto" else 0

    if total_months == 0:
        print("所有標的的歷史都已是最新，沒有需要連線抓的月份。")
        print("（要強制重抓請加 --force）")
        for code in codes:
            info = coverage(code)
            if info:
                start, end, count = info
                print(f"  {code}: {start} ~ {end}  共 {count} 根")
        return 0

    workers = max(1, min(args.workers, len(pending)))
    requests_total = total_months + probes
    # 發送頻率由全域限制器控管，所以總時間主要看「要送幾個請求 × 間隔」。
    # 這個估計不再忽略網路延遲——延遲被併發蓋掉了，不再累加。
    estimate = requests_total * POLITE_DELAY / 60

    print(
        f"要抓 {total_months} 個月（{len(pending)} 檔），"
        f"{workers} 檔同時進行，每 {POLITE_DELAY}s 送出一個請求。"
    )
    print(f"預估約 {max(estimate, 0.1):.1f} 分鐘（共 {requests_total} 個請求）。")
    already = args.months * len(codes) - total_months
    if already > 0:
        print(f"已經有資料的 {already} 個月會跳過。")
    print("每抓完一個月就存檔，中途 Ctrl+C 不會弄丟已抓到的部分。")
    print()

    started = time.monotonic()
    done = 0
    counter_lock = threading.Lock()

    def show(code: str, year: int, month: int, count: int | None, error: str | None):
        nonlocal done
        with counter_lock:
            done += 1
            seen = done
        elapsed = time.monotonic() - started
        rate = seen / elapsed if elapsed > 0 else 0
        remain = (requests_total - seen) / rate if rate > 0 else 0
        tail = f"  [{seen}/{requests_total}　剩約 {remain / 60:.1f} 分]"
        if error:
            print(f"  {code} {year}-{month:02d}  失敗：{error}{tail}", flush=True)
        else:
            print(f"  {code} {year}-{month:02d}  {count} 根{tail}", flush=True)

    try:
        results = backfill_many(
            pending,
            args.months,
            market=args.market,
            force=args.force,
            workers=workers,
            progress=show,
        )
    except KeyboardInterrupt:
        print()
        print("已中斷。已經抓到的月份都存好了，重跑會從缺的地方接下去。")
        return 130

    print()
    for code in codes:
        if code not in results:
            info = coverage(code)
            print(f"{code}: 已是最新（{info[2] if info else 0} 根）")
            continue
        count, market_label, fetched = results[code]
        if count:
            print(f"{code}: 共 {count} 根（{market_label}，這次抓了 {fetched} 個月）")
        else:
            print(f"{code}: 查無資料 —— 代號可能有誤，或這檔不在上市／上櫃")

    print()
    print(f"總共花了 {(time.monotonic() - started) / 60:.1f} 分鐘。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
