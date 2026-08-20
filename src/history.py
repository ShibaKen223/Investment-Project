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
import csv
import json
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"
RAW_DIR = ROOT / "data" / "raw"

TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TIMEOUT = 30
RETRIES = 3
# TWSE 對逐檔查詢有流量限制，連續抓多個月份時每次之間要停一下。
POLITE_DELAY = 3.0

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


def save_bars(code: str, bars: list[Bar]) -> int:
    """合併寫入。同一天以新資料覆蓋，回傳寫入後的總筆數。

    合併而不是覆寫，是為了讓「從 raw 重建」和「從 TWSE 補歷史」
    可以混用，兩邊各補各的區間不會互相清掉。
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    merged: dict[str, Bar] = {b.date: b for b in load_bars(code)}
    for bar in bars:
        if bar.is_valid:
            merged[bar.date] = bar

    ordered = [merged[d] for d in sorted(merged)]
    path = history_path(code)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for bar in ordered:
            writer.writerow(asdict(bar))
    return len(ordered)


def available_codes() -> list[str]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(p.stem for p in HISTORY_DIR.glob("*.csv"))


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
# ⚠️ 這支端點是「網頁後端 API」，不是 openapi.twse.com.tw 那種正式開放資料，
#    TWSE 改版時路徑或欄位名稱可能變動。解析刻意用「欄位名稱比對」而不是
#    寫死索引，就是為了讓改版時比較不容易靜靜地解析錯位。
#    如果哪天壞了，先用 --self-test 看它抓回來的 fields 長什麼樣子。


def _field_index(fields: list[str], *keywords: str) -> int | None:
    """在 fields 裡找第一個包含任一關鍵字的欄位位置。"""
    for idx, name in enumerate(fields):
        text = str(name)
        if any(kw in text for kw in keywords):
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


def fetch_twse_month(code: str, year: int, month: int) -> list[Bar]:
    """抓某檔某個月的日 K（上市股票）。"""
    params = {
        "date": f"{year:04d}{month:02d}01",
        "stockNo": code,
        "response": "json",
    }
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(TWSE_STOCK_DAY, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return parse_stock_day(resp.json())
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < RETRIES:
                time.sleep(POLITE_DELAY)
    raise RuntimeError(
        f"抓取失敗 {code} {year}-{month:02d}（{RETRIES} 次重試）"
    ) from last_error


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


def backfill(code: str, months: int = 12, today: date | None = None) -> int:
    """補齊某檔最近 N 個月的日 K，回傳總筆數。"""
    all_bars: list[Bar] = []
    for year, month in _month_sequence(months, today):
        all_bars.extend(fetch_twse_month(code, year, month))
        time.sleep(POLITE_DELAY)
    return save_bars(code, all_bars)


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="歷史日 K 管理。預設對象是持股 + 觀察清單。"
    )
    parser.add_argument(
        "--codes",
        help="逗號分隔的股票代號；省略則用 config/positions.yaml 裡的持股與觀察清單",
    )
    parser.add_argument(
        "--months", type=int, default=12, help="從 TWSE 補幾個月（預設 12）"
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
        "--self-test",
        action="store_true",
        help="抓一個月的 2330 並印出原始欄位，用來確認端點格式沒改版",
    )
    args = parser.parse_args()

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else universe_from_config()
    )

    if args.self_test:
        resp = requests.get(
            TWSE_STOCK_DAY,
            params={"date": "20260701", "stockNo": "2330", "response": "json"},
            timeout=TIMEOUT,
        )
        payload = resp.json()
        print(f"HTTP {resp.status_code}  stat={payload.get('stat')!r}")
        print(f"fields = {payload.get('fields')}")
        rows = payload.get("data") or []
        print(f"共 {len(rows)} 列，第一列: {rows[0] if rows else '（無）'}")
        parsed = parse_stock_day(payload)
        print(f"解析出 {len(parsed)} 根 K；第一根: {parsed[0] if parsed else '（無）'}")
        if not parsed and rows:
            print("⚠️  端點有回應但解析不出來，欄位名稱可能已改版。")
        return

    if args.status:
        if not codes:
            codes = available_codes()
        for code in codes:
            info = coverage(code)
            if info is None:
                print(f"{code}: （無資料）")
            else:
                start, end, count = info
                print(f"{code}: {start} ~ {end}  共 {count} 根")
        return

    if not codes:
        raise SystemExit("沒有要處理的代號，請用 --codes 指定。")

    if args.from_raw:
        result = rebuild_from_raw(set(codes))
        if not result:
            print("data/raw/ 裡沒有可用的存檔。系統每天跑過之後這裡才會累積資料。")
        for code, count in sorted(result.items()):
            print(f"{code}: {count} 根（從 data/raw/ 重建）")
        return

    print(f"從 TWSE 補 {len(codes)} 檔 × {args.months} 個月，每次請求間隔 {POLITE_DELAY}s…")
    for code in codes:
        try:
            count = backfill(code, args.months)
            print(f"{code}: {count} 根")
        except RuntimeError as exc:
            print(f"{code}: 失敗 —— {exc}")


if __name__ == "__main__":
    main()
