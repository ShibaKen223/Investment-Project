"""驗證監控層接上程式交易引擎部位之後的行為。

這支測試的主軸只有一句話：
**畫面上的停損線，必須跟引擎明天真的會賣的價格是同一個數字。**

其餘的檢查（來源切換、唯讀、委託備註、峰值不互相污染）都是繞著它來的——
一旦兩邊算出不同的數字，使用者不會收到任何錯誤，只會看到一條
看起來很合理、但實際上沒有人會照它執行的線。
"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import monitor
import paper
import paperdaily
import portfolio
from datasource import Quote
from portfolio import Position, Rules, Signal, summarize

failures: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL':4} {desc}{'  ' + extra if extra else ''}")
    if not ok:
        failures.append(desc)


def q(code: str, close: float) -> Quote:
    return Quote(code, f"測試{code}", "2026-08-21", close, 0.0,
                 None, None, None, None, "上市")


# 測試自己給設定，不讀 config/paper.yaml——
# 不然使用者改一次策略參數，這裡就會莫名其妙紅一片。
PCT_CONFIG = {
    "enabled": True,
    "account": {"initial_cash": 1_000_000},
    "strategy": {
        "stop_mode": "pct",
        "stop_loss_pct": 8.0,
        "take_profit_pct": 15.0,
        "max_hold_bars": 15,
    },
}
ATR_CONFIG = {
    "enabled": True,
    "account": {"initial_cash": 1_000_000},
    "strategy": {
        "stop_mode": "atr",
        "atr_period": 14,
        "atr_stop_multiple": 2.0,
        "atr_target_multiple": 3.5,
        "max_hold_bars": 15,
    },
}


def use_config(config: dict) -> None:
    paperdaily.load_config = lambda: config          # type: ignore[assignment]


def pos(code: str, entry_price: float, **kw) -> paper.PaperPosition:
    defaults = dict(
        shares=1000, entry_date="2026-08-01", entry_fee=171.0,
        entry_reason="突破前 20 日高點", peak_close=0.0, bars_held=3,
        entry_atr=0.0,
    )
    defaults.update(kw)
    return paper.PaperPosition(code=code, entry_price=entry_price, **defaults)


def account_with(*positions, pending=(), last_date="2026-08-21") -> paper.Account:
    return paper.Account(
        cash=500_000.0,
        positions={p.code: p for p in positions},
        pending=list(pending),
        last_date=last_date,
    )


# --------------------------------------------------------------------------
print("\n--- 來源解析 ---")
# --------------------------------------------------------------------------

for raw, expected in [
    ("manual", "manual"), ("engine", "engine"), ("both", "both"),
    ("ENGINE", "engine"), (None, "manual"),
]:
    got, warns = monitor.resolve_source({"source": raw} if raw else {})
    check(f"source={raw!r} → {expected}", got == expected and not warns)

got, warns = monitor.resolve_source({"source": "broker"})
check("看不懂的 source 退回 manual 並且會講出來", got == "manual" and len(warns) == 1)

check("只有 engine 模式是唯讀",
      monitor.MonitorSet(source="engine").read_only
      and not monitor.MonitorSet(source="manual").read_only
      and not monitor.MonitorSet(source="both").read_only)


# --------------------------------------------------------------------------
print("\n--- 手動持股：未出場 / 已出場要分得開 ---")
# --------------------------------------------------------------------------

doc = {
    "positions": [
        {"code": "2330", "shares": 1000, "cost": 100.0, "entry_date": "2026-01-01"},
        {"code": "2603", "shares": 2000, "cost": 50.0, "entry_date": "2026-01-01",
         "exit_date": "2026-02-01", "exit_price": 55.0},
    ]
}
open_rows, closed_rows = monitor.manual_positions(doc)
check("未出場 1 筆、已出場 1 筆", len(open_rows) == 1 and len(closed_rows) == 1)
check("手動持股標記為 manual",
      open_rows[0].source == "manual" and not open_rows[0].is_engine)


# --------------------------------------------------------------------------
print("\n--- 引擎部位轉換（pct 模式）---")
# --------------------------------------------------------------------------

use_config(PCT_CONFIG)
rows, info = monitor.engine_positions(account_with(pos("2330", 100.0)))

check("轉出 1 筆引擎部位", len(rows) == 1)
p0 = rows[0]
check("標記為 engine", p0.is_engine and p0.source == "engine")
check("每股成本含買進手續費",
      abs(p0.cost - (100.0 * 1000 + 171.0) / 1000) < 1e-9,
      f"cost={p0.cost:.4f}")
check("進場理由沿用引擎寫的那句", p0.thesis == "突破前 20 日高點")
check("出場條件講出時間出場的根數", "15 根 K 線" in p0.invalidate)
check("核心部位旗標一定是關的（引擎沒有核心部位的概念）", p0.core is False)

stop, target, basis = info.levels["2330"]
check("停損 = 進場價 × (1 - 8%)", abs(stop - 92.0) < 1e-9, f"stop={stop}")
check("停利 = 進場價 × (1 + 15%)", abs(target - 115.0) < 1e-9, f"target={target}")
check("停損停利以 entry_price 為基準，不是含手續費的成本",
      abs(stop - 92.0) < 1e-9 and p0.cost > 100.0)


# --------------------------------------------------------------------------
print("\n--- 引擎部位轉換（atr 模式）---")
# --------------------------------------------------------------------------

use_config(ATR_CONFIG)
rows, info = monitor.engine_positions(account_with(pos("2454", 500.0, entry_atr=12.0)))
stop, target, basis = info.levels["2454"]
check("停損 = 進場價 - 2×ATR", abs(stop - (500.0 - 24.0)) < 1e-9, f"stop={stop}")
check("停利 = 進場價 + 3.5×ATR", abs(target - (500.0 + 42.0)) < 1e-9, f"target={target}")
check("基準說明講得出 ATR 是多少錢", "ATR(14)" in basis, basis)

# ATR 拿不到時要退回百分比，而且要在說明裡講清楚換了規則
rows, info = monitor.engine_positions(account_with(pos("2454", 500.0, entry_atr=0.0)))
_, _, basis_fallback = info.levels["2454"]
check("進場時沒有 ATR 就退回百分比，並且說明有寫出來",
      "退回百分比" in basis_fallback, basis_fallback)


# --------------------------------------------------------------------------
print("\n--- 核心回歸：停損線必須是引擎的，不是監控層的百分比 ---")
# --------------------------------------------------------------------------

use_config(ATR_CONFIG)
# 進場 500、ATR 12 → 引擎停損 476。
# 監控層的 rules 是 10%（停損線 450），兩者差了 26 元。
# 收盤 470 落在中間：引擎會賣，監控層自己算的話會說「續抱」。
mset = monitor.load({"source": "engine"}, account_with(pos("2454", 500.0, entry_atr=12.0)))
rules = Rules(stop_loss_pct=10.0, take_profit_pct=22.0, near_threshold_pct=3.0)
evals, warns = monitor.evaluate_all(mset, {"2454": q("2454", 470.0)}, rules, {})

ev = evals[0]
check("停損線用引擎的 476，不是 rules 的 450",
      abs(ev.stop_price - 476.0) < 1e-9, f"stop_price={ev.stop_price}")
check("收盤 470 判定為觸發停損（用 rules 算會是續抱）",
      ev.signal is Signal.STOP_LOSS, ev.signal.value)
check("這一列帶著停損基準的說明", bool(ev.basis_label), ev.basis_label)

# 對照組：同一筆部位若當成手動持股、用 rules 算，結論確實不一樣。
# 這一條是用來證明上面那個測試真的有在測東西。
manual_ev = portfolio.evaluate(
    Position("2454", 1000, 500.0, "2026-08-01"), q("2454", 470.0), rules
)
check("對照組：同樣的價格用監控層百分比算是續抱",
      manual_ev.signal is Signal.HOLD, manual_ev.signal.value)

# 個股例外規則不該套到引擎部位上
overrides = {"2454": {"stop_loss_pct": 1.0}}
evals_ovr, _ = monitor.evaluate_all(
    mset, {"2454": q("2454", 470.0)}, rules, overrides
)
check("個股例外規則不會動到引擎部位的停損線",
      abs(evals_ovr[0].stop_price - 476.0) < 1e-9)


# --------------------------------------------------------------------------
print("\n--- 引擎接下來要做什麼，要寫在這一列上 ---")
# --------------------------------------------------------------------------

use_config(PCT_CONFIG)
order = paper.Order(code="2330", side="SELL", shares=1000,
                    decided_on="2026-08-21", reason="STOP",
                    detail="收盤 91.0 跌破停損線 92.0")
mset = monitor.load(
    {"source": "engine"},
    account_with(pos("2330", 100.0, bars_held=3), pending=[order]),
)
evals, _ = monitor.evaluate_all(mset, {"2330": q("2330", 91.0)}, rules, {})
notes = " ".join(evals[0].notes)
check("待賣委託會寫進備註", "明日開盤賣出" in notes, notes)
check("備註帶著引擎給的理由", "跌破停損線" in notes)

# 時間出場：快到了要提醒，到了要說已經觸發
mset = monitor.load({"source": "engine"},
                    account_with(pos("2330", 100.0, bars_held=13)))
evals, _ = monitor.evaluate_all(mset, {"2330": q("2330", 100.0)}, rules, {})
check("再 2 根就時間出場會提醒", "再 2 根" in " ".join(evals[0].notes),
      " ".join(evals[0].notes))

mset = monitor.load({"source": "engine"},
                    account_with(pos("2330", 100.0, bars_held=15)))
evals, _ = monitor.evaluate_all(mset, {"2330": q("2330", 100.0)}, rules, {})
check("抱滿了要說已觸發時間出場",
      "觸發時間出場" in " ".join(evals[0].notes), " ".join(evals[0].notes))


# --------------------------------------------------------------------------
print("\n--- source 切換的取捨要講出來，不能安靜地少東西 ---")
# --------------------------------------------------------------------------

use_config(PCT_CONFIG)
doc_both = {
    "source": "engine",
    "positions": [
        {"code": "2330", "shares": 1000, "cost": 90.0, "entry_date": "2026-01-01"}
    ],
}
acct = account_with(pos("2317", 50.0))
mset = monitor.load(doc_both, acct)
check("engine 模式下手動持股不列入監控",
      [p.code for p in mset.positions] == ["2317"])
check("但一定要講出來有幾筆被略過",
      any("1 筆手動持股" in w for w in mset.warnings), str(mset.warnings))

doc_both["source"] = "both"
mset = monitor.load(doc_both, account_with(pos("2330", 95.0)))
check("both 模式兩邊都監控", len(mset.positions) == 2)
check("同一檔在兩邊都有時要明講會出現兩列",
      any("2330" in w and "各列一行" in w for w in mset.warnings), str(mset.warnings))

mset = monitor.load({"source": "manual", "positions": doc_both["positions"]})
check("manual 模式完全不碰引擎",
      len(mset.positions) == 1 and not mset.engine.available)

# 引擎關掉了還設成 engine：部位是舊的，不能安靜地照常顯示
use_config({**PCT_CONFIG, "enabled": False})
mset = monitor.load({"source": "engine"}, account_with(pos("2330", 100.0)))
check("引擎被關掉時會警告部位停在最後一次執行",
      any("enabled 是 false" in w for w in mset.warnings), str(mset.warnings))


# --------------------------------------------------------------------------
print("\n--- 引擎沒跑而行情更新了 ---")
# --------------------------------------------------------------------------

use_config(PCT_CONFIG)
_, info = monitor.engine_positions(account_with(pos("2330", 100.0),
                                                last_date="2026-08-18"))
check("引擎落後於行情日會提醒",
      monitor.staleness_warning(info, "2026-08-21") is not None)
check("引擎跟上了就不囉嗦",
      monitor.staleness_warning(info, "2026-08-18") is None)


# --------------------------------------------------------------------------
print("\n--- 峰值不能互相污染 ---")
# --------------------------------------------------------------------------

# 引擎部位的移動停損基準來自它自己的 peak_close，
# 手動持股的來自 signals.jsonl；兩者同一個代號時不可以互相沾到。
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "signals.jsonl"
    log.write_text(
        json.dumps({
            "trade_date": "2026-08-20",
            "positions": [
                {"code": "2330", "close": 999.0, "source": "engine"},
                {"code": "2330", "close": 120.0, "source": "manual"},
            ],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    original = portfolio.SIGNAL_LOG
    portfolio.SIGNAL_LOG = log
    try:
        peaks = portfolio.load_peaks([Position("2330", 1000, 100.0, "2026-01-01")])
    finally:
        portfolio.SIGNAL_LOG = original
    check("手動持股的峰值不會吃到引擎那一列",
          peaks.get("2330") == 120.0, str(peaks))

    # 舊紀錄沒有 source 欄位（那時候還沒有引擎部位），要當成手動
    log.write_text(
        json.dumps({"trade_date": "2026-08-20",
                    "positions": [{"code": "2330", "close": 130.0}]},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    portfolio.SIGNAL_LOG = log
    try:
        peaks = portfolio.load_peaks([Position("2330", 1000, 100.0, "2026-01-01")])
    finally:
        portfolio.SIGNAL_LOG = original
    check("沒有 source 欄位的舊紀錄仍然算數", peaks.get("2330") == 130.0, str(peaks))

# 引擎自己的 peak_close 有進來
use_config(PCT_CONFIG)
mset = monitor.load({"source": "engine"},
                    account_with(pos("2330", 100.0, peak_close=118.0)))
evals, _ = monitor.evaluate_all(mset, {"2330": q("2330", 110.0)}, rules, {})
check("引擎部位的峰值取自 peak_close", evals[0].peak_price == 118.0)


# --------------------------------------------------------------------------
print("\n--- 彙總數字仍然算得出來 ---")
# --------------------------------------------------------------------------

use_config(PCT_CONFIG)
mset = monitor.load({"source": "engine"},
                    account_with(pos("2330", 100.0), pos("2317", 50.0)))
evals, warns = monitor.evaluate_all(
    mset, {"2330": q("2330", 110.0), "2317": q("2317", 45.0)}, rules, {}
)
s = summarize(evals)
check("兩檔都算進投組彙總", s["positions"] == 2 and s["priced"] == 2)
check("市值 = 110×1000 + 45×1000",
      abs(s["market_value"] - 155_000.0) < 1e-6, str(s["market_value"]))

# 查無行情不該讓整輪掛掉
evals, warns = monitor.evaluate_all(mset, {"2330": q("2330", 110.0)}, rules, {})
check("查無行情的那檔只警告，不中斷",
      len(evals) == 2 and any("2317" in w for w in warns))
check("沒行情的部位判定為 NO_DATA",
      any(e.signal is Signal.NO_DATA for e in evals))


print()
if failures:
    print(f"{len(failures)} 項失敗 ❌")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("監控層接引擎部位：全部通過 ✅")
