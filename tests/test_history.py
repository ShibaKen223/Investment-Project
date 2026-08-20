"""歷史日 K 儲存與解析的測試。

跑法:
    python3 tests/test_history.py

全部在暫存目錄裡進行，不會動到 data/ 底下任何東西，也不連外網。

重點在兩件事:
  1. 合併寫入不會把既有資料弄丟（從 raw 重建和從 TWSE 補歷史要能混用）。
  2. TWSE 端點改版時，解析要「明確地失敗」而不是靜靜地解析錯位——
     錯位的日 K 會產生看起來很正常、但完全錯誤的均線與訊號。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history  # noqa: E402
from history import Bar  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


TMP = Path(tempfile.mkdtemp(prefix="history-test-"))
history.HISTORY_DIR = TMP / "history"
history.RAW_DIR = TMP / "raw"


def bar(date: str, close: float = 100.0, volume: int = 1000) -> Bar:
    return Bar(date=date, open=close, high=close, low=close,
               close=close, volume=volume)


try:
    # ======================================================================
    print("--- K 棒健全性 ---")
    # ======================================================================

    check("正常 K 棒通過檢查", bar("2026-01-05").is_valid)
    check(
        "最高價低於最低價 → 判定無效",
        not Bar(date="d", open=100, high=90, low=95, close=95, volume=1).is_valid,
    )
    check(
        "收盤價落在高低價之外 → 判定無效（典型的欄位錯位徵兆）",
        not Bar(date="d", open=100, high=105, low=99, close=120, volume=1).is_valid,
    )
    check(
        "價格為零或負 → 判定無效",
        not Bar(date="d", open=0, high=0, low=0, close=0, volume=1).is_valid,
    )

    # ======================================================================
    print()
    print("--- 存檔與合併 ---")
    # ======================================================================

    history.save_bars("TEST", [bar("2026-01-06", 101), bar("2026-01-05", 100)])
    loaded = history.load_bars("TEST")
    check("寫入後讀得回來", len(loaded) == 2, str(loaded))
    check(
        "自動依日期排序",
        [b.date for b in loaded] == ["2026-01-05", "2026-01-06"],
        str([b.date for b in loaded]),
    )
    check("數值型別正確", isinstance(loaded[0].close, float)
          and isinstance(loaded[0].volume, int))

    history.save_bars("TEST", [bar("2026-01-07", 102)])
    merged = history.load_bars("TEST")
    check(
        "再次寫入是合併，不是覆蓋（舊資料還在）",
        len(merged) == 3,
        str([b.date for b in merged]),
    )

    history.save_bars("TEST", [bar("2026-01-05", 999)])
    updated = {b.date: b.close for b in history.load_bars("TEST")}
    check("同一天重複寫入 → 以新資料為準", updated["2026-01-05"] == 999.0)

    history.save_bars(
        "TEST", [Bar(date="2026-01-08", open=1, high=0, low=5, close=3, volume=1)]
    )
    check(
        "無效的 K 棒不會被寫進檔案",
        "2026-01-08" not in {b.date for b in history.load_bars("TEST")},
    )

    info = history.coverage("TEST")
    check(
        "涵蓋範圍回報正確",
        info == ("2026-01-05", "2026-01-07", 3),
        str(info),
    )
    check("查無資料的代號回傳 None", history.coverage("NOPE") is None)
    check("查無資料的代號讀出空 list", history.load_bars("NOPE") == [])

    # ======================================================================
    print()
    print("--- 民國日期 ---")
    # ======================================================================

    check("'1150728' → 2026-07-28", history._roc_to_iso("1150728") == "2026-07-28")
    check("'115/07/28' → 2026-07-28", history._roc_to_iso("115/07/28") == "2026-07-28")
    check("格式不符回傳 None，不丟例外", history._roc_to_iso("2026-07-28") is None)
    check("空字串回傳 None", history._roc_to_iso("") is None)

    # ======================================================================
    print()
    print("--- TWSE STOCK_DAY 解析 ---")
    # ======================================================================

    payload = {
        "stat": "OK",
        "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價",
                   "最低價", "收盤價", "漲跌價差", "成交筆數"],
        "data": [
            ["115/07/01", "25,000,000", "50,000,000,000",
             "2,000.00", "2,020.00", "1,990.00", "2,010.00", "+10.00", "30,000"],
            ["115/07/02", "0", "0", "--", "--", "--", "--", "0.00", "0"],
        ],
    }
    parsed = history.parse_stock_day(payload)
    check("解析出有成交的那一天", len(parsed) == 1, str(parsed))
    check("日期轉為西元", parsed[0].date == "2026-07-01", parsed[0].date)
    check("千分位逗號有被去掉", parsed[0].close == 2010.0, str(parsed[0].close))
    check("開高低收對應正確",
          (parsed[0].open, parsed[0].high, parsed[0].low) == (2000.0, 2020.0, 1990.0),
          str(parsed[0]))
    check("成交量解析正確", parsed[0].volume == 25_000_000, str(parsed[0].volume))
    check("無成交日（'--'）被略過，不會變成 0 元的假 K 棒", len(parsed) == 1)

    # 欄位順序改變時要靠名稱找對位置，而不是寫死索引
    shuffled = {
        "stat": "OK",
        "fields": ["日期", "開盤價", "收盤價", "最高價", "最低價", "成交股數"],
        "data": [["115/07/01", "2,000.00", "2,010.00", "2,020.00",
                  "1,990.00", "25,000,000"]],
    }
    reparsed = history.parse_stock_day(shuffled)
    check(
        "欄位順序調換後仍解析正確（靠名稱比對，不是寫死索引）",
        len(reparsed) == 1 and reparsed[0].close == 2010.0
        and reparsed[0].high == 2020.0,
        str(reparsed),
    )

    check("stat 不是 OK → 回傳空 list",
          history.parse_stock_day({"stat": "很抱歉，沒有符合條件的資料"}) == [])
    check("欄位名稱全變 → 回傳空 list（明確失敗，不亂猜）",
          history.parse_stock_day(
              {"stat": "OK", "fields": ["a", "b", "c"], "data": [["1", "2", "3"]]}
          ) == [])
    check("空回應不會丟例外", history.parse_stock_day({}) == [])

    # ======================================================================
    print()
    print("--- TPEx tradingStock 解析（上櫃逐檔月報表）---")
    # ======================================================================
    # 這個格式跟 TWSE 有三處不同，每一處算錯都不會報錯、只會產生錯的均線：
    #   資料在 tables[0] 裡、欄位沒有「價」字、成交量單位是張不是股。

    tpex_payload = {
        "tables": [
            {
                "fields": ["日期", "成交張數", "成交仟元", "開盤", "最高",
                           "最低", "收盤", "漲跌", "筆數"],
                "data": [
                    ["115/07/01", "1,234", "620,000",
                     "500.00", "510.00", "495.00", "505.00", "+5.00", "800"],
                    ["115/07/02", "0", "0", "--", "--", "--", "--", "0.00", "0"],
                ],
            }
        ]
    }
    tpex_bars = history.parse_trading_stock(tpex_payload)
    check("從 tables[0] 裡取得資料", len(tpex_bars) == 1, str(tpex_bars))
    check("日期轉為西元", tpex_bars and tpex_bars[0].date == "2026-07-01")
    check(
        "開高低收對應正確（欄位名稱沒有「價」字）",
        tpex_bars and (tpex_bars[0].open, tpex_bars[0].high,
                       tpex_bars[0].low, tpex_bars[0].close)
        == (500.0, 510.0, 495.0, 505.0),
        str(tpex_bars[0]) if tpex_bars else "",
    )
    check(
        "成交張數 1,234 張 → 換算成 1,234,000 股（與 TWSE 單位對齊）",
        tpex_bars and tpex_bars[0].volume == 1_234_000,
        f"得到 {tpex_bars[0].volume if tpex_bars else None}",
    )
    check("無成交日被略過", len(tpex_bars) == 1)

    # 若某天端點改成回傳「成交股數」，就不該再乘 1000
    shares_payload = {
        "tables": [
            {
                "fields": ["日期", "成交股數", "開盤", "最高", "最低", "收盤"],
                "data": [["115/07/01", "1,234,000", "500.00", "510.00",
                          "495.00", "505.00"]],
            }
        ]
    }
    shares_bars = history.parse_trading_stock(shares_payload)
    check(
        "欄位若是「成交股數」則不做張→股換算（避免多乘 1000 倍）",
        shares_bars and shares_bars[0].volume == 1_234_000,
        f"得到 {shares_bars[0].volume if shares_bars else None}",
    )

    check(
        "資料直接放在頂層時也吃得下",
        len(history.parse_trading_stock({
            "fields": ["日期", "開盤", "最高", "最低", "收盤", "成交張數"],
            "data": [["115/07/01", "500.00", "510.00", "495.00", "505.00", "10"]],
        })) == 1,
    )
    check("空回應不會丟例外", history.parse_trading_stock({}) == [])
    check("tables 是空的不會丟例外", history.parse_trading_stock({"tables": []}) == [])
    check(
        "欄位名稱全變 → 回傳空 list（明確失敗，不亂猜）",
        history.parse_trading_stock(
            {"tables": [{"fields": ["a", "b"], "data": [["1", "2"]]}]}
        ) == [],
    )

    # ======================================================================
    print()
    print("--- 實際端點欄位（迴歸測試）---")
    # ======================================================================
    # 以下欄位名稱抄自真實 API 回應，不是我們想像出來的。
    # TPEx 的日期欄位實際上叫「日 期」，中間有一個空格，同一份回應裡
    # 其他欄位卻沒有。曾經因此整份解析成零根 K：不拋例外、沒有錯誤訊息，
    # 只是均線少了一段。這兩條測試就是為了讓那個情況不會再無聲發生。

    live_twse = {
        "stat": "OK",
        "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價",
                   "最低價", "收盤價", "漲跌價差", "成交筆數", "註記"],
        "data": [["115/07/01", "25,000,000", "50,000,000,000", "2,000.00",
                  "2,020.00", "1,990.00", "2,010.00", "+10.00", "30,000", ""]],
    }
    twse_live = history.parse_stock_day(live_twse)
    check(
        "上市：實際欄位（末尾多一個「註記」）解析正常",
        len(twse_live) == 1 and twse_live[0].close == 2010.0,
        str(twse_live),
    )

    live_tpex = {
        "tables": [{
            "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高",
                       "最低", "收盤", "漲跌", "筆數"],
            "data": [["115/07/01", "1,234", "620,000", "500.00", "510.00",
                      "495.00", "505.00", "+5.00", "800"]],
        }]
    }
    tpex_live = history.parse_trading_stock(live_tpex)
    check(
        "上櫃：日期欄位叫「日 期」（中間有空格）仍解析得出來",
        len(tpex_live) == 1 and tpex_live[0].date == "2026-07-01",
        str(tpex_live),
    )
    check(
        "上櫃：實際欄位的價量都對",
        tpex_live and tpex_live[0].close == 505.0
        and tpex_live[0].volume == 1_234_000,
        str(tpex_live[0]) if tpex_live else "",
    )
    check(
        "全形空格（\u3000）同樣不影響比對",
        len(history.parse_trading_stock({
            "tables": [{
                "fields": ["日\u3000期", "開盤", "最高", "最低", "收盤", "成交張數"],
                "data": [["115/07/01", "500.00", "510.00", "495.00",
                          "505.00", "10"]],
            }]
        })) == 1,
    )
    check(
        "_squash 把各種空白都清掉",
        history._squash(" 日 期 ") == "日期"
        and history._squash("日\u3000期") == "日期",
        history._squash("日\u3000期"),
    )

    # ======================================================================
    print()
    print("--- 從 data/raw/ 重建 ---")
    # ======================================================================

    history.RAW_DIR.mkdir(parents=True, exist_ok=True)
    (history.RAW_DIR / "2026-07-28_twse.json").write_text(
        json.dumps([
            {"Code": "2330", "Date": "1150728", "OpeningPrice": "2,000.00",
             "HighestPrice": "2,020.00", "LowestPrice": "1,990.00",
             "ClosingPrice": "2,010.00", "TradeVolume": "25,000,000"},
            {"Code": "9999", "Date": "1150728", "OpeningPrice": "--",
             "HighestPrice": "--", "LowestPrice": "--",
             "ClosingPrice": "--", "TradeVolume": "0"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    (history.RAW_DIR / "2026-07-28_tpex.json").write_text(
        json.dumps([
            {"SecuritiesCompanyCode": "6488", "Date": "1150728", "Open": "500.00",
             "High": "510.00", "Low": "495.00", "Close": "505.00",
             "TradingShares": "1,000,000"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    result = history.rebuild_from_raw({"2330", "6488", "9999"})
    check("上市資料重建成功", result.get("2330") == 1, str(result))
    check("上櫃資料重建成功（欄位名稱不同）", result.get("6488") == 1, str(result))
    check("當日無成交的代號不會產生 K 棒", "9999" not in result, str(result))

    rebuilt = history.load_bars("2330")
    check("重建出的價格正確",
          rebuilt and rebuilt[0].close == 2010.0 and rebuilt[0].open == 2000.0,
          str(rebuilt))

    check(
        "只重建指定的代號",
        history.rebuild_from_raw({"2330"}).keys() == {"2330"},
    )

    history.save_bars("2330", [bar("2026-07-27", 1980)])
    history.rebuild_from_raw({"2330"})
    combined = history.load_bars("2330")
    check(
        "從 raw 重建不會蓋掉先前補的歷史（兩種來源可混用）",
        len(combined) == 2,
        str([b.date for b in combined]),
    )

    # ======================================================================
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 項失敗 ❌")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通過 ✅")

finally:
    shutil.rmtree(TMP, ignore_errors=True)
