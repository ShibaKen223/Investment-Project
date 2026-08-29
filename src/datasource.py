"""台股收盤行情抓取。

資料源皆為公開、免費、免金鑰:
  上市 (TWSE): https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
  上櫃 (TPEx): https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes

兩支 API 都是「一次回傳全市場」，所以不管追蹤幾檔股票都只打兩次 request。
每次抓取的原始回應會存到 data/raw/，之後要重算或除錯都能回頭比對。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

import requests

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

TIMEOUT = 30
RETRIES = 3


@dataclass
class Quote:
    code: str
    name: str
    trade_date: str  # YYYY-MM-DD
    close: float
    change: float
    open: float | None
    high: float | None
    low: float | None
    volume: int | None  # 成交股數
    market: str  # "上市" | "上櫃"

    @property
    def prev_close(self) -> float | None:
        """由收盤價與漲跌反推昨收，用來算漲跌幅。"""
        prev = self.close - self.change
        return prev if prev > 0 else None

    @property
    def change_pct(self) -> float | None:
        prev = self.prev_close
        return None if prev is None else (self.change / prev) * 100


def _to_float(raw: object) -> float | None:
    """行情欄位可能是 '--'、''、'1,234.00'、'+0.02 '，都要能吃。"""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("+", "")
    if text in ("", "--", "---", "null", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(raw: object) -> int | None:
    value = _to_float(raw)
    return None if value is None else int(value)


def roc_to_iso(roc: str) -> str:
    """民國日期字串 '1150728' -> '2026-07-28'。"""
    roc = str(roc).strip()
    if len(roc) != 7 or not roc.isdigit():
        raise ValueError(f"無法解析的民國日期: {roc!r}")
    return f"{int(roc[:3]) + 1911:04d}-{roc[3:5]}-{roc[5:7]}"


def _get_json(url: str) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 免費 API，任何失敗都重試
            last_error = exc
            if attempt == RETRIES:
                break
    raise RuntimeError(f"抓取失敗 ({RETRIES} 次重試): {url}") from last_error


def _parse_twse(rows: list[dict]) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for row in rows:
        close = _to_float(row.get("ClosingPrice"))
        if close is None:
            continue  # 當日無成交（停牌、無量）
        code = str(row.get("Code", "")).strip()
        quotes[code] = Quote(
            code=code,
            name=str(row.get("Name", "")).strip(),
            trade_date=roc_to_iso(row["Date"]),
            close=close,
            change=_to_float(row.get("Change")) or 0.0,
            open=_to_float(row.get("OpeningPrice")),
            high=_to_float(row.get("HighestPrice")),
            low=_to_float(row.get("LowestPrice")),
            volume=_to_int(row.get("TradeVolume")),
            market="上市",
        )
    return quotes


def _parse_tpex(rows: list[dict]) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for row in rows:
        close = _to_float(row.get("Close"))
        if close is None:
            continue
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        quotes[code] = Quote(
            code=code,
            name=str(row.get("CompanyName", "")).strip(),
            trade_date=roc_to_iso(row["Date"]),
            close=close,
            change=_to_float(row.get("Change")) or 0.0,
            open=_to_float(row.get("Open")),
            high=_to_float(row.get("High")),
            low=_to_float(row.get("Low")),
            volume=_to_int(row.get("TradingShares")),
            market="上櫃",
        )
    return quotes


def fetch_quotes(
    raw_dir: Path | None = None, warnings: list[str] | None = None
) -> dict[str, Quote]:
    """抓取全市場收盤行情，回傳 {股票代號: Quote}。

    上市優先——極少數代號在兩個市場都出現時以上市為準。
    raw_dir 有給就把原始 JSON 落地存檔（append-only，不覆寫既有日期）。

    **兩個市場分開處理，一邊掛掉不會拖垮另一邊。**
    以前是任何一邊失敗就整支拋例外，於是上櫃端點抽風的時候，
    連上市持股的停損提醒都產不出來——那天你等於完全沒有監控。
    兩邊都掛才是真的沒救，那時候才拋。

    失敗的那一邊會寫進 warnings，日報上看得到，
    不會讓人以為「今天上櫃標的都沒有行情」是市場的事。
    """
    twse_rows: list[dict] = []
    tpex_rows: list[dict] = []
    failures: list[str] = []

    for label, url, sink in (
        ("上市 TWSE", TWSE_URL, "twse"),
        ("上櫃 TPEX", TPEX_URL, "tpex"),
    ):
        try:
            rows = _get_json(url)
        except RuntimeError as exc:
            failures.append(label)
            if warnings is not None:
                warnings.append(
                    f"{label} 行情抓取失敗，這次的報告不含該市場的標的。"
                    f"（{exc}）"
                )
            continue
        if sink == "twse":
            twse_rows = rows
        else:
            tpex_rows = rows

    if len(failures) == 2:
        raise RuntimeError("上市與上櫃行情都抓不到，這次無法產生報告。")

    quotes = _parse_tpex(tpex_rows)
    quotes.update(_parse_twse(twse_rows))

    # 只把成功的那一邊落地。把空清單存成當日檔案的話，
    # 之後 --from-raw 重建會拿它當「那天沒有資料」的證據，
    # 而且不會再被覆寫（存檔刻意不覆寫既有日期）。
    if raw_dir is not None:
        _archive_raw(
            raw_dir,
            twse_rows if twse_rows else None,
            tpex_rows if tpex_rows else None,
            quotes,
        )

    return quotes


def _archive_raw(
    raw_dir: Path,
    twse_rows: list[dict] | None,
    tpex_rows: list[dict] | None,
    quotes: dict[str, Quote],
) -> None:
    """把原始 JSON 落地。None 代表那個市場這次沒抓到，直接跳過不存。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    trade_date = market_date(quotes) or date.today().isoformat()
    for name, payload in (("twse", twse_rows), ("tpex", tpex_rows)):
        if payload is None:
            continue
        path = raw_dir / f"{trade_date}_{name}.json"
        if path.exists():
            continue  # 已存檔的交易日不覆寫
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def market_date(quotes: dict[str, Quote]) -> str | None:
    """行情的交易日。取眾數，避免個別標的日期異常。"""
    if not quotes:
        return None
    counts: dict[str, int] = {}
    for quote in quotes.values():
        counts[quote.trade_date] = counts.get(quote.trade_date, 0) + 1
    return max(counts, key=counts.get)


def quote_to_dict(quote: Quote) -> dict:
    return asdict(quote)


def is_stale(trade_date: str, today: date | None = None) -> int:
    """行情日期距今幾天。>1 通常代表遇到假日或資料源沒更新。"""
    today = today or date.today()
    return (today - datetime.strptime(trade_date, "%Y-%m-%d").date()).days
