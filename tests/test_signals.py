"""驗證停損停利判斷的每一條分支都真的會觸發。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datasource import Quote
from portfolio import Position, Rules, Signal, evaluate, resolve_rules, summarize


def q(close: float) -> Quote:
    return Quote("T", "測試", "2026-07-28", close, 0.0, None, None, None, None, "上市")


RULES = Rules(stop_loss_pct=10.0, take_profit_pct=22.0, near_threshold_pct=3.0)
POS = Position("T", 1000, 100.0, "2026-01-01")

# 成本 100 → 停損線 90，停利線 122
cases = [
    (85.0, Signal.STOP_LOSS,   "遠低於停損線"),
    (90.0, Signal.STOP_LOSS,   "恰好等於停損線（含等號）"),
    (92.0, Signal.NEAR_STOP,   "距停損線 2.2% 內"),
    (105.0, Signal.HOLD,       "中間地帶"),
    (119.0, Signal.NEAR_TARGET,"距停利線 2.5% 內"),
    (122.0, Signal.TAKE_PROFIT,"恰好等於停利線"),
    (150.0, Signal.TAKE_PROFIT,"遠高於停利線"),
]

failures = []
for close, expected, desc in cases:
    ev = evaluate(POS, q(close), RULES)
    ok = ev.signal is expected
    if not ok:
        failures.append((close, expected, ev.signal, desc))
    print(f"{'PASS' if ok else 'FAIL':4} 收盤 {close:7.2f} → {ev.signal.value:12} "
          f"損益 {ev.pnl_pct:+6.2f}% 距停損 {ev.pct_to_stop:+6.2f}% "
          f"距停利 {ev.pct_to_target:+6.2f}%  ({desc})")

# 核心部位不觸發
ev = evaluate(Position("T", 1000, 100.0, "2026-01-01", core=True), q(50.0), RULES)
ok = ev.signal is Signal.CORE
failures += [] if ok else [("core", Signal.CORE, ev.signal, "核心部位")]
print(f"{'PASS' if ok else 'FAIL':4} 核心部位暴跌 50% → {ev.signal.value}（不觸發停損）")

# 無行情
ev = evaluate(POS, None, RULES)
ok = ev.signal is Signal.NO_DATA and ev.pnl_pct is None
failures += [] if ok else [("nodata", Signal.NO_DATA, ev.signal, "無行情")]
print(f"{'PASS' if ok else 'FAIL':4} 查無行情 → {ev.signal.value}")

# 移動停損：成本 100，進場後最高 150 → 停損線應為 135 而非 90
tr = Rules(stop_loss_pct=10.0, take_profit_pct=22.0, stop_basis="trailing")
ev = evaluate(POS, q(130.0), tr, peak_price=150.0)
ok = abs(ev.stop_price - 135.0) < 1e-9 and ev.signal is Signal.STOP_LOSS
failures += [] if ok else [("trailing", 135.0, ev.stop_price, "移動停損")]
print(f"{'PASS' if ok else 'FAIL':4} 移動停損 峰值150 現價130 → 停損線 {ev.stop_price:.2f} "
      f"訊號 {ev.signal.value}（固定停損下這裡還是 HOLD）")

# 移動停損無歷史 → 退回成本基準
ev = evaluate(POS, q(130.0), tr, peak_price=None)
ok = abs(ev.stop_price - 90.0) < 1e-9 and len(ev.notes) == 1
failures += [] if ok else [("trailing-cold", 90.0, ev.stop_price, "移動停損冷啟動")]
print(f"{'PASS' if ok else 'FAIL':4} 移動停損無歷史 → 停損線 {ev.stop_price:.2f}（退回成本基準）+ 提醒")

# 個股例外規則
over = resolve_rules(RULES, {"T": {"stop_loss_pct": 20.0}}, "T")
ok = over.stop_loss_pct == 20.0 and over.take_profit_pct == 22.0
failures += [] if ok else [("override", 20.0, over.stop_loss_pct, "個股例外")]
print(f"{'PASS' if ok else 'FAIL':4} 個股例外 停損放寬到 {over.stop_loss_pct}%，停利沿用 {over.take_profit_pct}%")

# 彙總只計入有行情的部位
evs = [evaluate(POS, q(110.0), RULES), evaluate(POS, None, RULES)]
s = summarize(evs)
ok = abs(s["cost_basis"] - 100000) < 1e-9 and abs(s["pnl_pct"] - 10.0) < 1e-9
failures += [] if ok else [("summary", 100000, s["cost_basis"], "彙總")]
print(f"{'PASS' if ok else 'FAIL':4} 彙總排除無行情部位 → 成本 {s['cost_basis']:.0f} 報酬 {s['pnl_pct']:+.2f}%")

# --------------------------------------------------------------------------
# 移動停損的峰值必須從「進場日」起算
# --------------------------------------------------------------------------
# 曾經的 bug：整份 signals.jsonl 掃過去取最大值，於是峰值會吃到進場之前的
# 價格、甚至上一輪早就出場的那個部位的價格，把停損線莫名其妙地往上拉。
import json      # noqa: E402
import tempfile  # noqa: E402

import portfolio  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="peaks-test-"))
portfolio.SIGNAL_LOG = _tmp / "signals.jsonl"
with portfolio.SIGNAL_LOG.open("w", encoding="utf-8") as fh:
    for trade_date, close in [
        ("2026-01-10", 300.0),   # 進場前的高點：不該被算進去
        ("2026-03-02", 120.0),   # 進場當天
        ("2026-03-10", 180.0),   # 進場後的真高點
        ("2026-03-15", 140.0),
    ]:
        fh.write(json.dumps({
            "trade_date": trade_date,
            "positions": [{"code": "T", "close": close}],
        }) + "\n")

held = Position("T", 1000, 100.0, "2026-03-02")
peaks = portfolio.load_peaks([held])
ok = peaks.get("T") == 180.0
failures += [] if ok else [("peaks", 180.0, peaks.get("T"), "峰值起算日")]
print(f"{'PASS' if ok else 'FAIL':4} 峰值從進場日起算 → {peaks.get('T')}（不是進場前的 300）")

ok = portfolio.load_peaks([Position("T", 1000, 100.0, "2026-04-01")]) == {}
failures += [] if ok else [("peaks", {}, "非空", "進場日之後沒有紀錄")]
print(f"{'PASS' if ok else 'FAIL':4} 進場日之後還沒有紀錄 → 回傳空的，退回成本基準")

ok = portfolio.load_peaks([]) == {}
failures += [] if ok else [("peaks", {}, "非空", "沒有部位")]
print(f"{'PASS' if ok else 'FAIL':4} 沒有部位時不會去讀檔")

print()
print("全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌ {failures}")
sys.exit(1 if failures else 0)
