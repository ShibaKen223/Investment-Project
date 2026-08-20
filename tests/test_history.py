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
    print("--- 破洞偵測 ---")
    # ======================================================================
    # months_needed() 只看你這次要求的區間，更早的殘缺月份永遠不會被發現。
    # 檔案看起來涵蓋兩年、中間卻是空的——這種破洞不會報錯，
    # 但會讓「往回數 N 根」的指標算錯期間。

    def month_bars(year: int, month: int, count: int = 20) -> list[Bar]:
        return [bar(f"{year:04d}-{month:02d}-{day:02d}") for day in range(1, count + 1)]

    continuous = month_bars(2026, 1) + month_bars(2026, 2) + month_bars(2026, 3)
    check("連續的資料沒有破洞", history.gaps_in(continuous) == [])

    # 真實情境：抓了 2024-09~11，中斷；後來只補了最近 12 個月
    holed = (
        month_bars(2024, 9) + month_bars(2024, 10) + month_bars(2024, 11)
        + [b for m in range(9, 13) for b in month_bars(2025, m)]
    )
    detected = history.gaps_in(holed)
    check(
        "中間缺掉的月份被抓出來（2024-12 ~ 2025-08 共 9 個月）",
        len(detected) == 9 and detected[0] == (2024, 12) and detected[-1] == (2025, 8),
        str(detected),
    )

    check(
        "頭尾的部分月份不算破洞（剛開始追蹤、當月還沒過完）",
        history.gaps_in(
            month_bars(2026, 1, count=3) + month_bars(2026, 2)
            + month_bars(2026, 3, count=2)
        ) == [],
    )
    check(
        "中間只有零星幾根的月份算破洞",
        history.gaps_in(
            month_bars(2026, 1) + month_bars(2026, 2, count=3) + month_bars(2026, 3)
        ) == [(2026, 2)],
    )
    check("資料太少不會誤判", history.gaps_in([]) == []
          and history.gaps_in([bar("2026-01-05")]) == [])

    history.save_bars("HOLED", holed)
    check(
        "find_gaps 從檔案讀出來的結果一致",
        history.find_gaps("HOLED") == detected,
        str(history.find_gaps("HOLED")),
    )

    # ======================================================================
    print()
    print("--- 增量回補（決定哪些月份要連線）---")
    # ======================================================================
    # 過去的日 K 不會再變，重抓只是浪費時間。
    # 這段邏輯讓第二次以後的執行從好幾分鐘縮短到幾秒。

    from datetime import date as _date

    ref = _date(2026, 8, 20)

    fresh = history.months_needed("NEVER_FETCHED", 6, today=ref)
    check(
        "完全沒資料 → 6 個月全部都要抓",
        len(fresh) == 6,
        str(fresh),
    )
    check(
        "月份由舊到新排序，最後一個是當月",
        fresh[-1] == (2026, 8) and fresh[0] == (2026, 3),
        str(fresh),
    )

    # 幫 2026-03 ~ 2026-06 各塞滿一個月的資料
    filled: list[Bar] = []
    for month in (3, 4, 5, 6):
        for day in range(1, 21):
            filled.append(bar(f"2026-{month:02d}-{day:02d}"))
    history.save_bars("PARTIAL", filled)

    todo = history.months_needed("PARTIAL", 6, today=ref)
    check(
        "已抓齊的過去月份會跳過（只剩 7 月與 8 月）",
        todo == [(2026, 7), (2026, 8)],
        str(todo),
    )

    # 最近兩個月即使有資料也要重抓（當月還在累積、上月可能補登）
    recent: list[Bar] = list(filled)
    for month in (7, 8):
        for day in range(1, 21):
            recent.append(bar(f"2026-{month:02d}-{day:02d}"))
    history.save_bars("RECENT", recent)
    todo_recent = history.months_needed("RECENT", 6, today=ref)
    check(
        "最近兩個月一律重抓，即使已經有資料",
        todo_recent == [(2026, 7), (2026, 8)],
        str(todo_recent),
    )

    # 資料稀疏的月份要重抓（可能是上次抓到一半被中斷）
    sparse = [bar(f"2026-03-{day:02d}") for day in range(1, 4)]
    history.save_bars("SPARSE", sparse)
    check(
        "某月只有零星幾根（上次抓一半被中斷）→ 該月要重抓",
        (2026, 3) in history.months_needed("SPARSE", 6, today=ref),
        str(history.months_needed("SPARSE", 6, today=ref)),
    )

    check(
        "--force 會忽略既有資料，全部重抓",
        len(history.months_needed("PARTIAL", 6, today=ref, force=True)) == 6,
    )

    check(
        "跨年時月份序列正確（2026-01 往前推到 2025-11）",
        history.months_needed("NEVER", 3, today=_date(2026, 1, 15))
        == [(2025, 11), (2025, 12), (2026, 1)],
        str(history.months_needed("NEVER", 3, today=_date(2026, 1, 15))),
    )

    # ======================================================================
    print()
    print("--- 發送頻率控管與併發 ---")
    # ======================================================================
    # 原本的寫法是「送出 → 等回應 → sleep → 下一個」，總時間會是
    # 次數 ×（延遲 + 間隔）。端點慢的時候延遲才是大頭，
    # 99 個月因此跑了八分多鐘而不是預估的 2.6 分鐘。
    # 現在頻率由全域限制器控管、延遲用併發蓋掉，這兩條測試就是在確認
    # 「變快了，但沒有對端點送得更密」。

    import threading as _threading
    import time as _time

    limiter = history._RateLimiter(0.05)
    stamps: list[float] = []
    stamps_lock = _threading.Lock()

    def hit() -> None:
        limiter.wait()
        with stamps_lock:
            stamps.append(_time.monotonic())

    threads = [_threading.Thread(target=hit) for _ in range(10)]
    begin = _time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = _time.monotonic() - begin

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    check(
        "10 個請求分散在 4+ 個執行緒，仍照全域間隔排隊",
        elapsed >= 9 * 0.05 * 0.9,
        f"只花了 {elapsed:.3f}s，應至少 {9 * 0.05:.3f}s",
    )
    check(
        "任兩個請求之間都有間隔（併發沒有讓發送變密）",
        all(g >= 0.05 * 0.8 for g in gaps),
        f"最小間隔 {min(gaps):.4f}s",
    )
    check(
        "但也沒有比必要的更慢（沒有累加等待）",
        elapsed < 9 * 0.05 * 2.5,
        f"花了 {elapsed:.3f}s",
    )

    # --- 併發回補：用假的抓取函式，不連外網 ---
    call_log: list[str] = []
    log_lock = _threading.Lock()

    def fake_fetch(code: str, year: int, month: int) -> list[Bar]:
        _limiter_wait()
        with log_lock:
            call_log.append(f"{code}:{year}-{month:02d}")
        _time.sleep(0.02)   # 假裝有網路延遲
        return [bar(f"{year:04d}-{month:02d}-{day:02d}") for day in range(1, 21)]

    _limiter_wait = history._limiter.wait
    real_twse, real_detect = history.fetch_twse_month, history.detect_market
    history.fetch_twse_month = fake_fetch
    history.detect_market = lambda code, year, month: "twse"
    try:
        outcome = history.backfill_many(
            ["AAA", "BBB", "CCC"], months=4, today=_date(2026, 8, 20), workers=3
        )
    finally:
        history.fetch_twse_month, history.detect_market = real_twse, real_detect

    check(
        "三檔都回補完成",
        set(outcome) == {"AAA", "BBB", "CCC"},
        str(sorted(outcome)),
    )
    check(
        "每檔各抓了 4 個月",
        all(fetched == 4 for _, _, fetched in outcome.values()),
        str(outcome),
    )
    check(
        "資料真的寫進各自的檔案，沒有互相污染",
        len(history.load_bars("AAA")) == 80
        and len(history.load_bars("BBB")) == 80,
        f"AAA {len(history.load_bars('AAA'))} 根、BBB {len(history.load_bars('BBB'))} 根",
    )
    check(
        "同一檔的月份仍照順序抓（出錯時才知道是哪一段）",
        [c.split(":")[1] for c in call_log if c.startswith("AAA")]
        == ["2026-05", "2026-06", "2026-07", "2026-08"],
        str([c for c in call_log if c.startswith("AAA")]),
    )
    check(
        "回補完之後再跑一次，不會重抓已經抓齊的月份",
        history.months_needed("AAA", 4, today=_date(2026, 8, 20))
        == [(2026, 7), (2026, 8)],
        str(history.months_needed("AAA", 4, today=_date(2026, 8, 20))),
    )
    check("空清單不會爆炸", history.backfill_many([], months=4) == {})

    # ======================================================================
    print()
    print("--- 併發寫入保護 ---")
    # ======================================================================
    # save_bars 是「讀出全部 → 合併 → 整個寫回」。沒有保護的話，
    # 兩個行程各自讀到舊內容、各自合併、後寫的蓋掉先寫的 —— 資料就沒了。
    # 修正前用同樣的情境實測，120 根只剩 60 根。
    #
    # 會撞到的不是理論情況：手動跑 history.py 回補時碰上 15:00 的每日排程
    # （main.py 也會寫同一批檔案），或兩個回補行程並行，都會踩到。

    import subprocess as _subprocess

    race_dir = TMP / "race"
    worker_src = TMP / "race_worker.py"
    worker_src.write_text(
        "import sys, pathlib\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        "import history\n"
        "from history import Bar\n"
        "history.HISTORY_DIR = pathlib.Path(sys.argv[1])\n"
        "month = int(sys.argv[2])\n"
        "for _ in range(6):\n"
        "    history.save_bars('RACE', [\n"
        "        Bar(f'2026-{month:02d}-{d:02d}', 100.0, 100.0, 100.0, 100.0, 1000)\n"
        "        for d in range(1, 21)\n"
        "    ])\n",
        encoding="utf-8",
    )

    procs = [
        _subprocess.Popen([sys.executable, str(worker_src), str(race_dir), str(month)])
        for month in (1, 2, 3, 4)
    ]
    for proc in procs:
        proc.wait(timeout=60)

    saved_dir = history.HISTORY_DIR
    history.HISTORY_DIR = race_dir
    try:
        race_bars = history.load_bars("RACE")
        race_months = sorted({b.date[:7] for b in race_bars})
        leftovers = [q.name for q in race_dir.glob("*.tmp")]
        codes_seen = history.available_codes()
    finally:
        history.HISTORY_DIR = saved_dir

    check(
        "4 個行程同時寫同一個檔案，沒有任何一個月的資料被蓋掉",
        len(race_bars) == 80,
        f"得到 {len(race_bars)} 根（應為 80），涵蓋 {race_months}",
    )
    check(
        "四個月份全部保留",
        len(race_months) == 4,
        str(race_months),
    )
    check(
        "沒有殘留的暫存檔（原子寫入有收乾淨）",
        not leftovers,
        str(leftovers),
    )
    check(
        "鎖檔不會被當成股票代號",
        codes_seen == ["RACE"],
        str(codes_seen),
    )

    # 原子性：任何時刻讀到的都是完整檔案，不會是寫到一半的狀態
    history.HISTORY_DIR = race_dir
    try:
        before = len(history.load_bars("RACE"))
        history.save_bars("RACE", [bar("2026-09-01")])
        after = history.load_bars("RACE")
        check(
            "寫入後檔案立即可讀且完整（沒有截斷）",
            len(after) == before + 1 and all(b.close > 0 for b in after),
            f"{before} → {len(after)}",
        )
    finally:
        history.HISTORY_DIR = saved_dir

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
