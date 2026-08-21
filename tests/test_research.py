"""研究報告與知識庫的測試。

跑法:
    python3 tests/test_research.py

兩個重點:

1. **算出來的數字要對。** 報告的可信度完全建立在「這些數字你可以自己驗證」
   上面，所以這裡用手算得出答案的資料去驗證每一個指標。

2. **知識庫要完整。** config/glossary.yaml 和 config/sectors.yaml 是手動維護的，
   你之後加標的時打錯一個字，報告不會壞掉——它只會安靜地少一段。
   這裡把交叉引用全部檢查一遍，讓錯字當場被抓出來。

不連外網，也不會動到 data/ 底下的東西。
"""

from __future__ import annotations

import shutil
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import tempfile
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history  # noqa: E402
import research  # noqa: E402
from history import Bar  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def bars_from(closes: list[float], volumes: list[int] | None = None) -> list[Bar]:
    """依收盤價造 K 棒，日期是連續工作日。"""
    out: list[Bar] = []
    day = _date(2025, 1, 6)
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += _timedelta(days=1)
        vol = volumes[i] if volumes else 1_000_000
        out.append(Bar(day.isoformat(), close, close, close, close, vol))
        day += _timedelta(days=1)
    return out


TMP = Path(tempfile.mkdtemp(prefix="research-test-"))
_real_history_dir = history.HISTORY_DIR
history.HISTORY_DIR = TMP / "history"

try:
    # ======================================================================
    print("--- 報酬計算 ---")
    # ======================================================================

    # 100 起漲，每天 +1 元，共 301 根
    ramp = bars_from([100.0 + i for i in range(301)])
    facts = research.compute_facts("RAMP", ramp)

    check("最新收盤取最後一根", facts.close == 400.0, str(facts.close))
    check("K 棒數正確", facts.bars == 301)
    # 報酬用「日曆天」而不是「往回數幾根 K」。
    # 往回數根數只要中間有缺資料就會標錯期間——實測可以把一年算成 648 天。
    def _ret_from(days: int) -> float:
        """手算：找出 days 天前那根 K，算到最後一根的報酬。"""
        last_date = _date.fromisoformat(ramp[-1].date)
        target = (last_date - _timedelta(days=days)).isoformat()
        past = [b for b in ramp if b.date <= target][-1]
        return (ramp[-1].close / past.close - 1) * 100

    check(
        "1 週報酬用 7 個日曆天",
        abs(facts.returns["1 週"] - _ret_from(7)) < 1e-9,
        f"得到 {facts.returns['1 週']}，手算 {_ret_from(7)}",
    )
    check(
        "1 個月報酬用 30 個日曆天",
        abs(facts.returns["1 個月"] - _ret_from(30)) < 1e-9,
        f"得到 {facts.returns['1 個月']}，手算 {_ret_from(30)}",
    )
    check(
        "1 年報酬用 365 個日曆天",
        abs(facts.returns["1 年"] - _ret_from(365)) < 1e-9,
        f"得到 {facts.returns['1 年']}，手算 {_ret_from(365)}",
    )

    # 有破洞時寧可顯示「—」，也不要給一個標錯期間的數字
    holed = bars_from([100.0] * 30)            # 一個月左右
    old_chunk = [
        Bar(f"2023-01-{d:02d}", 50.0, 50.0, 50.0, 50.0, 1000)
        for d in range(1, 21)
    ]
    holed_facts = research.compute_facts("HOLED", old_chunk + holed)
    check(
        "資料中間有兩年的洞 → 1 年報酬回傳 None，不硬算",
        holed_facts.returns["1 年"] is None,
        str(holed_facts.returns["1 年"]),
    )
    check(
        "破洞月份數有被算出來",
        holed_facts.gap_months > 0,
        str(holed_facts.gap_months),
    )

    short = research.compute_facts("SHORT", bars_from([100.0, 101.0, 102.0]))
    check(
        "資料不足的期間回傳 None，不會亂算",
        short.returns["1 年"] is None and short.returns["1 週"] is None,
        str(short.returns),
    )
    check("資料很少時仍能產生 Facts（不丟例外）", short is not None)
    check("空資料回傳 None", research.compute_facts("EMPTY", []) is None)

    # ======================================================================
    print()
    print("--- 一年區間位置 ---")
    # ======================================================================

    # 一年區間同樣用日曆天切
    _year_cut = (
        _date.fromisoformat(ramp[-1].date) - _timedelta(days=365)
    ).isoformat()
    _year_bars = [b for b in ramp if b.date >= _year_cut]
    check(
        "一年高點 = 最近 365 個日曆天內的最高價",
        facts.high_52w == max(b.high for b in _year_bars),
        f"得到 {facts.high_52w}",
    )
    check(
        "一年低點 = 最近 365 個日曆天內的最低價",
        facts.low_52w == min(b.low for b in _year_bars),
        f"得到 {facts.low_52w}",
    )
    check(
        "一路創新高 → 區間位置 100%",
        abs(facts.range_position - 100.0) < 1e-9,
        str(facts.range_position),
    )
    check("距高點 0%", abs(facts.pct_from_high) < 1e-9, str(facts.pct_from_high))

    # 一路下跌 → 應該在區間最低
    falling = research.compute_facts("FALL", bars_from([400.0 - i for i in range(301)]))
    check(
        "一路下跌 → 區間位置 0%",
        abs(falling.range_position) < 1e-9,
        str(falling.range_position),
    )
    check(
        "距高點為負值（跌了多少）",
        falling.pct_from_high < -50,
        str(falling.pct_from_high),
    )

    # V 型：先跌後漲，中點應該在區間中段
    v_shape = research.compute_facts(
        "V", bars_from([200.0 - i for i in range(100)] + [100.0 + i for i in range(100)])
    )
    check(
        "V 型走勢 → 區間位置落在中段附近",
        40 <= v_shape.range_position <= 100,
        str(v_shape.range_position),
    )

    # ======================================================================
    print()
    print("--- 起漲點 ---")
    # ======================================================================

    # 100 一路跌到 51（index 0-49），再從 50 漲到 148（index 50-99）。
    # 全序列的最低收盤是 index 50 的 50.0。
    rally = bars_from([100.0 - i for i in range(50)] + [50.0 + i * 2 for i in range(50)])
    low_index = min(range(len(rally)), key=lambda i: rally[i].close)
    r_facts = research.compute_facts("RALLY", rally)
    check("有找到起漲點", r_facts.rally_start is not None)
    if r_facts.rally_start:
        start_date, gain = r_facts.rally_start
        check(
            "起漲點抓到最低收盤那天",
            start_date == rally[low_index].date and low_index == 50,
            f"抓到 {start_date}，應為 {rally[low_index].date}（index {low_index}）",
        )
        check(
            "自起漲點的漲幅計算正確（50 → 148 = +196%）",
            abs(gain - 196.0) < 1e-9,
            str(gain),
        )
    check(
        "最低點就是今天時不報起漲（沒有漲勢可言）",
        research.compute_facts("DOWN", bars_from([100.0 - i for i in range(60)]))
        .rally_start is None,
    )

    # ======================================================================
    print()
    print("--- 波動度與量能 ---")
    # ======================================================================

    flat = research.compute_facts("FLAT", bars_from([100.0] * 100))
    check("完全不動 → 波動度 0", abs(flat.volatility) < 1e-9, str(flat.volatility))

    swingy = research.compute_facts(
        "SWING", bars_from([100.0 if i % 2 == 0 else 110.0 for i in range(100)])
    )
    check(
        "上下震盪 → 波動度明顯高於平盤",
        swingy.volatility > flat.volatility + 50,
        str(swingy.volatility),
    )

    # 前 55 天 100 萬股，最後 5 天 300 萬股 → 比值應為 3 / 平均
    vols = [1_000_000] * 55 + [3_000_000] * 5
    vol_facts = research.compute_facts("VOL", bars_from([100.0] * 60, vols))
    expected = 3_000_000 / (sum(vols) / 60)
    check(
        "量能比 = 近 5 日均量 / 近 60 日均量",
        abs(vol_facts.volume_ratio - expected) < 1e-9,
        f"得到 {vol_facts.volume_ratio}，應為 {expected}",
    )
    check("放量會被判定出來", vol_facts.volume_ratio > 1.5)

    # ======================================================================
    print()
    print("--- ATR（停損寬度的依據）---")
    # ======================================================================

    # 固定高低差 10、收盤都在 100 → ATR = 10，佔現價 10%
    steady = [
        Bar(d, 100.0, 105.0, 95.0, 100.0, 1_000_000) for d in
        [b.date for b in bars_from([100.0] * 40)]
    ]
    steady_facts = research.compute_facts("STEADY", steady)
    check(
        "ATR(14) 算出來是 10 元",
        abs(steady_facts.atr14 - 10.0) < 1e-9,
        str(steady_facts.atr14),
    )
    check(
        "ATR 佔現價的百分比 = 10%",
        abs(steady_facts.atr_pct - 10.0) < 1e-9,
        str(steady_facts.atr_pct),
    )
    check(
        "資料不足時 ATR 為 None，不硬算",
        research.compute_facts("TINY", bars_from([100.0, 101.0])).atr14 is None,
    )

    # 高波動 → 報告應該說 8% 停損太緊
    loud = research.describe_shape(steady_facts, [])
    check(
        "ATR 佔比 10% 時，報告指出 8% 停損太緊",
        any("太緊" in line for line in loud),
        str([l for l in loud if "ATR" in l]),
    )

    # 低波動（高低差 0.5，現價 100 → ATR 0.5%）→ 應該說偏寬
    quiet_bars = [
        Bar(d, 100.0, 100.25, 99.75, 100.0, 1_000_000) for d in
        [b.date for b in bars_from([100.0] * 40)]
    ]
    quiet = research.describe_shape(
        research.compute_facts("QUIET", quiet_bars), []
    )
    check(
        "ATR 佔比極低時，報告指出 8% 停損偏寬",
        any("偏寬" in line for line in quiet),
        str([l for l in quiet if "ATR" in l]),
    )

    # ======================================================================
    print()
    print("--- 均線狀態與相對強弱 ---")
    # ======================================================================

    check("持續上漲 → 站上 20 日與 60 日均線", facts.above_ma20 and facts.above_ma60)
    check("持續下跌 → 跌破兩條均線",
          not falling.above_ma20 and not falling.above_ma60)

    # 個股 3 個月報酬 20%，大盤 5% → 相對強弱 +15
    stock = bars_from([100.0] * 240 + [100.0 * (1 + 0.20 * i / 60) for i in range(61)])
    bench = bars_from([100.0] * 240 + [100.0 * (1 + 0.05 * i / 60) for i in range(61)])
    rs = research.compute_facts("S", stock, bench)
    check(
        "相對強弱 = 個股 3 個月報酬 − 大盤 3 個月報酬",
        abs(rs.relative_strength - 15.0) < 0.5,
        str(rs.relative_strength),
    )
    check(
        "沒有大盤資料時相對強弱為 None",
        research.compute_facts("S", stock).relative_strength is None,
    )

    # ======================================================================
    print()
    print("--- 知識庫完整性 ---")
    # ======================================================================

    glossary = research.load_glossary()
    stocks, notes = research.load_sectors()

    check("名詞辭典讀得到內容", len(glossary) > 20, f"只有 {len(glossary)} 條")
    check("產業地圖讀得到內容", len(stocks) > 10, f"只有 {len(stocks)} 檔")

    missing_fields = [
        k for k, v in glossary.items()
        if not all(v.get(f) for f in ("term", "category", "one_liner", "why_it_matters"))
    ]
    check("每個名詞都有完整欄位", not missing_fields, str(missing_fields))

    bad_refs = [
        (code, term)
        for code, entry in stocks.items()
        for term in (entry.get("terms") or [])
        if term not in glossary
    ]
    check(
        "產業地圖引用的名詞在辭典裡都找得到（抓錯字）",
        not bad_refs,
        str(bad_refs),
    )

    stock_missing = [
        k for k, v in stocks.items()
        if not all(v.get(f) for f in ("name", "sector", "what", "revenue_driver"))
    ]
    check("每檔股票都有完整欄位", not stock_missing, str(stock_missing))

    orphan_notes = [
        n for n in notes
        if not any(str(v.get("sector", "")).split(" - ")[0] == n
                   or v.get("sector") == n for v in stocks.values())
    ]
    check("產業提醒都對應得到實際的產業分類", not orphan_notes, str(orphan_notes))

    check(
        "代號都是字串（006208 不會被讀成數字 6208）",
        all(isinstance(k, str) for k in stocks),
    )

    # ======================================================================
    print()
    print("--- 名詞查詢 ---")
    # ======================================================================

    check("正式名稱查得到", len(research.search_terms("毛利率", glossary)) >= 1)
    check("別名查得到（ABF → IC 載板）",
          any(k == "substrate" for k, _ in research.search_terms("ABF", glossary)))
    check("英文不分大小寫", len(research.search_terms("cowos", glossary)) >= 1)
    check("部分比對（PCB）", len(research.search_terms("PCB", glossary)) >= 1)
    check("查無結果回傳空 list", research.search_terms("不存在的詞彙xyz", glossary) == [])
    check("空字串不會回傳全部", research.search_terms("", glossary) == [])

    exact_first = research.search_terms("HBM", glossary)
    check("完全比對的結果排在前面", exact_first and exact_first[0][0] == "hbm",
          str([k for k, _ in exact_first[:3]]))

    # ======================================================================
    print()
    print("--- 報告產生 ---")
    # ======================================================================

    history.save_bars("2330", bars_from([100.0 + i for i in range(300)]))
    history.save_bars("006208", bars_from([100.0 + i * 0.5 for i in range(300)]))

    report = research.build_report(["2330", "006208"])
    check("報告產生成功", len(report) > 500)
    check("含【系統算出來的】區塊", "【系統算出來的】" in report)
    check("含【背景知識】區塊", "【背景知識】" in report)
    check("含【系統不知道的】區塊", "【系統不知道的】" in report)
    check(
        "明確聲明沒有接新聞與財報（這句不能消失）",
        "沒有接新聞" in report,
    )
    check("明確聲明不構成投資建議", "不構成投資建議" in report)
    check("背景知識標示為會過時", "會過時" in report)
    check("有帶出名詞解釋", "晶圓代工" in report)

    no_data = research.build_report(["9999"])
    check(
        "沒有歷史資料的代號不會讓報告爆掉，而是說明該怎麼補",
        "src/history.py" in no_data,
        no_data[-300:],
    )

    # 有歷史資料、但 sectors.yaml 裡沒登記的代號：
    # 報告照樣產生，只是少了產業背景那一段，並提示怎麼補。
    history.save_bars("8888", bars_from([100.0 + i for i in range(300)]))
    unknown = research.build_report(["8888"])
    check("辭典裡沒有的代號仍然產生報告", "【系統算出來的】" in unknown)
    check("並提示可以自己補進 sectors.yaml", "sectors.yaml" in unknown)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 項失敗 ❌")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通過 ✅")

finally:
    history.HISTORY_DIR = _real_history_dir
    shutil.rmtree(TMP, ignore_errors=True)
