"""模擬倉與波段策略的測試。

跑法:
    python3 tests/test_paper.py

改過 src/strategy.py、src/paper.py 之後請先跑這支。
理由跟 test_signals.py 一樣：策略被改壞是沉默的，
回測照樣跑完、數字照樣印出來，只是那些決策是錯的。

這裡全部用合成資料，不連外網——測的是引擎的行為，不是策略賺不賺錢。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paper  # noqa: E402
import strategy  # noqa: E402
from history import Bar  # noqa: E402
from paper import Account, AccountParams, Costs, Order, Series  # noqa: E402
from strategy import StrategyParams, check_entry, check_exit  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def _dates(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    """連續 count 個工作日的 ISO 日期。用真日期，避免測試裡出現 2026-02-49。"""
    out: list[str] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def flat_bars(count: int, price: float = 100.0, volume: int = 1_000_000) -> list[Bar]:
    """一段完全沒有波動的日 K，用來當作乾淨的背景。"""
    return [
        Bar(date=d, open=price, high=price, low=price, close=price, volume=volume)
        for d in _dates(count)
    ]


def seq_bars(closes: list[float], volume: int = 1_000_000) -> list[Bar]:
    """依收盤價序列造 K 棒；開盤價 = 前一根收盤，高低價包住兩者。"""
    bars: list[Bar] = []
    prev = closes[0]
    for d, close in zip(_dates(len(closes)), closes):
        open_ = prev
        bars.append(
            Bar(
                date=d,
                open=open_,
                high=max(open_, close),
                low=min(open_, close),
                close=close,
                volume=volume,
            )
        )
        prev = close
    return bars


def extend(bars: list[Bar], *, open: float, high: float, low: float,
           close: float, volume: int = 1_000_000) -> list[Bar]:
    """在序列後面接一根 K，日期自動接續。"""
    next_date = _dates(len(bars) + 1)[-1]
    return bars + [Bar(date=next_date, open=open, high=high, low=low,
                       close=close, volume=volume)]


# ==========================================================================
print("--- 交易成本 ---")
# ==========================================================================

costs = Costs()
check(
    "手續費 = 金額 × 0.1425% × 折扣，捨去到元",
    costs.fee(100_000) == 85.0,
    f"得到 {costs.fee(100_000)}",
)
check(
    "小額交易套用最低手續費 20 元",
    costs.fee(1_000) == 20.0,
    f"得到 {costs.fee(1_000)}",
)
check("證交稅 0.3%", costs.tax(100_000) == 300.0, f"得到 {costs.tax(100_000)}")

round_trip = costs.fee(100_000) * 2 + costs.tax(100_000)
check(
    f"來回成本約 0.47%（10 萬元 → {round_trip:.0f} 元）",
    468 <= round_trip <= 472,
    f"得到 {round_trip}",
)
check(
    "買進滑價往上、賣出滑價往下",
    costs.buy_price(100) > 100 > costs.sell_price(100),
)


# ==========================================================================
print()
print("--- 進場訊號 ---")
# ==========================================================================

params = StrategyParams()

# 80 根平盤 + 1 根突破
breakout = extend(flat_bars(80), open=100, high=105, low=100, close=105)

sig = check_entry("TEST", breakout, len(breakout) - 1, params)
check("四條件同時成立 → 進場訊號", sig.triggered, sig.explanation)
check("訊號帶有可讀的理由", len(sig.reasons) == 4, str(sig.reasons))

# 資料不足
short = flat_bars(30)
sig_short = check_entry("TEST", short, len(short) - 1, params)
check(
    "歷史不足 → 不進場且說明原因",
    not sig_short.triggered and "資料不足" in sig_short.explanation,
    sig_short.explanation,
)

# 沒突破
no_break = flat_bars(81)
sig_nb = check_entry("TEST", no_break, len(no_break) - 1, params)
check(
    "沒突破前 20 日高點 → 不進場",
    not sig_nb.triggered and any("突破" in b for b in sig_nb.blockers),
    sig_nb.explanation,
)

# 量太小
thin = extend(flat_bars(80, volume=100_000), open=100, high=105, low=100,
              close=105, volume=100_000)
sig_thin = check_entry("TEST", thin, len(thin) - 1, params)
check(
    "均量不足 → 不進場（避免買到買不到的冷門股）",
    not sig_thin.triggered and any("均量" in b for b in sig_thin.blockers),
    sig_thin.explanation,
)

# 逆勢：長期均線之下不進場
downtrend = seq_bars([200 - i for i in range(80)])
downtrend = extend(downtrend, open=121, high=135, low=121, close=135)
sig_down = check_entry("TEST", downtrend, len(downtrend) - 1, params)
check(
    "突破了但仍在 60 日均線之下 → 不進場",
    not sig_down.triggered and any("60 日均線" in b for b in sig_down.blockers),
    sig_down.explanation,
)

# 不看未來
future = extend(breakout, open=999, high=999, low=999, close=999,
                volume=9_000_000)
sig_future = check_entry("TEST", future, len(breakout) - 1, params)
check(
    "判斷不受後面幾根 K 影響（無前視偏誤）",
    sig_future.triggered == sig.triggered
    and sig_future.reasons == sig.reasons,
)


# ==========================================================================
print()
print("--- 出場訊號 ---")
# ==========================================================================

entry_price = 100.0

stop_bars = seq_bars([100, 95, 91])
check(
    "跌破 8% 停損線 → STOP_LOSS",
    check_exit(entry_price, stop_bars, 2, 3, params).reason == strategy.STOP_LOSS,
)

profit_bars = seq_bars([100, 110, 116])
check(
    "漲過 15% 停利線 → TAKE_PROFIT",
    check_exit(entry_price, profit_bars, 2, 3, params).reason == strategy.TAKE_PROFIT,
)

hold_bars = seq_bars([100, 103, 105])
check(
    "區間內且未到期 → 不出場",
    not check_exit(entry_price, hold_bars, 2, 3, params).triggered,
)

check(
    f"抱滿 {params.max_hold_bars} 根未觸發 → TIME_EXIT（波段紀律）",
    check_exit(entry_price, hold_bars, 2, params.max_hold_bars, params).reason
    == strategy.TIME_EXIT,
)

trail_params = StrategyParams(trailing_stop_pct=10.0)
# 收盤 114 = 未達 +15% 停利、未跌破 −8% 停損，
# 唯一會讓它出場的就是移動停損（130 × 0.9 = 117）。
retrace = seq_bars([100, 130, 114])
check(
    "啟用移動停損：自高點 130 回落至 114 → TRAILING_STOP",
    check_exit(100.0, retrace, 2, 3, trail_params,
               peak_close=130.0).reason == strategy.TRAILING_STOP,
)
check(
    "未啟用移動停損時，同樣的回落不出場（+14% 還沒到停利線）",
    not check_exit(100.0, retrace, 2, 3, params, peak_close=130.0).triggered,
)

# 同一天停損與停利都成立時（極端跳空），必須先認停損
both = seq_bars([100, 100, 90])
check(
    "停損優先於停利 —— 模擬績效不因排序而偏樂觀",
    check_exit(entry_price, both, 2, params.max_hold_bars, params).reason
    == strategy.STOP_LOSS,
)


# ==========================================================================
print()
print("--- ATR 與波動度自適應停損 ---")
# ==========================================================================

# 固定高低差 10、完全沒有跳空 → ATR 就是 10
steady = [
    Bar(date=d, open=100, high=105, low=95, close=100, volume=1_000_000)
    for d in _dates(30)
]
check(
    "無跳空、固定區間 10 → ATR = 10",
    abs(strategy.atr(steady, 14) - 10.0) < 1e-9,
    str(strategy.atr(steady, 14)),
)

# 跳空的那根，真實區間要算進跳空的幅度，不能只看當根高低差
gapped = steady[:20] + [
    Bar(date="2026-03-02", open=120, high=130, low=120, close=125, volume=1_000_000)
]
check(
    "跳空的 K：TR 取「今高 − 昨收」而不是當根高低差（30 而非 10）",
    strategy.true_range(gapped, 20) == 30,
    str(strategy.true_range(gapped, 20)),
)
check(
    "第一根沒有昨收可比，退回高低差",
    strategy.true_range(steady, 0) == 10,
)
check("索引超出範圍回傳 None", strategy.true_range(steady, 999) is None)
check(
    "歷史不足 period+1 根 → ATR 回傳 None（不硬算）",
    strategy.atr(steady[:10], 14) is None,
)
check(
    "ATR 只看到 end 為止（不偷看後面的跳空）",
    abs(strategy.atr(gapped, 14, end=20) - 10.0) < 1e-9,
    str(strategy.atr(gapped, 14, end=20)),
)

# --- 停損停利價位 ---
p_pct = StrategyParams()
p_atr = StrategyParams(stop_mode="atr", atr_stop_multiple=2.0,
                       atr_target_multiple=3.5)

stop_p, target_p, basis_p = strategy.exit_levels(100.0, p_pct)
check(
    "pct 模式：停損 92 / 停利 115",
    abs(stop_p - 92.0) < 1e-9 and abs(target_p - 115.0) < 1e-9,
    f"{stop_p} {target_p}",
)

stop_a, target_a, basis_a = strategy.exit_levels(100.0, p_atr, entry_atr=5.0)
check(
    "atr 模式：停損 = 進場價 − 2×ATR = 90",
    abs(stop_a - 90.0) < 1e-9,
    str(stop_a),
)
check(
    "atr 模式：停利 = 進場價 + 3.5×ATR = 117.5",
    abs(target_a - 117.5) < 1e-9,
    str(target_a),
)
check("停損基準會說明清楚（供覆盤用）", "ATR(14)" in basis_a, basis_a)

# 這是換 ATR 的整個理由：同樣的參數，不同波動度得到不同的停損寬度
wide, _, _ = strategy.exit_levels(100.0, p_atr, entry_atr=5.0)
narrow, _, _ = strategy.exit_levels(100.0, p_atr, entry_atr=1.0)
check(
    "高波動股停損放寬（−10%）、低波動股收緊（−2%）—— 這就是換 ATR 的理由",
    abs(wide - 90.0) < 1e-9 and abs(narrow - 98.0) < 1e-9,
    f"高波動 {wide}，低波動 {narrow}",
)

_, _, fallback_basis = strategy.exit_levels(100.0, p_atr, entry_atr=None)
check(
    "設定 ATR 但拿不到 ATR → 退回百分比，且在說明裡講明白（不靜靜換規則）",
    "退回百分比" in fallback_basis,
    fallback_basis,
)
check(
    "退回時用的是固定百分比的價位",
    abs(strategy.exit_levels(100.0, p_atr, entry_atr=None)[0] - 92.0) < 1e-9,
)

# --- 出場判斷真的有吃 ATR ---
atr_stop_bars = seq_bars([100, 95, 89])
check(
    "ATR 模式：跌破 2×ATR 觸發停損（固定 8% 下同樣觸發，但基準不同）",
    check_exit(100.0, atr_stop_bars, 2, 3, p_atr, entry_atr=5.0).reason
    == strategy.STOP_LOSS,
)
mild = seq_bars([100, 97, 95])
check(
    "ATR=5 時 −5% 還不出場，ATR=1 時同樣的跌幅就該停損",
    not check_exit(100.0, mild, 2, 3, p_atr, entry_atr=5.0).triggered
    and check_exit(100.0, mild, 2, 3, p_atr, entry_atr=1.0).reason
    == strategy.STOP_LOSS,
)
check(
    "停損訊息裡帶著基準，兩個月後看得出當時用什麼算的",
    "ATR(14)" in check_exit(100.0, atr_stop_bars, 2, 3, p_atr,
                            entry_atr=5.0).detail,
)
check(
    f"ATR 模式的暖身期會把 atr_period 算進去",
    StrategyParams(stop_mode="atr", atr_period=90).warmup_bars == 92,
    str(StrategyParams(stop_mode="atr", atr_period=90).warmup_bars),
)
check(
    "atr_period 小於其他指標時，暖身期不受影響",
    StrategyParams(stop_mode="atr", atr_period=14).warmup_bars
    == StrategyParams().warmup_bars,
)


# ==========================================================================
print()
print("--- 成交 ---")
# ==========================================================================

acct = Account(cash=1_000_000.0)
acct.pending = [Order(code="TEST", side="BUY", shares=5000,
                      decided_on="2026-01-01", detail="測試")]
fills = paper.execute_pending(acct, "2026-01-02", {"TEST": 100.0}, costs)

check("買進成交一筆", len(fills) == 1 and fills[0]["status"] == "FILLED", str(fills))
pos = acct.positions.get("TEST")
check("部位建立", pos is not None and pos.shares == 5000)
check(
    "成交價 = 開盤價 + 滑價",
    pos is not None and abs(pos.entry_price - 100.1) < 1e-9,
    f"得到 {pos.entry_price if pos else None}",
)
check(
    "現金扣掉價金與手續費",
    abs(acct.cash - (1_000_000 - 100.1 * 5000 - costs.fee(100.1 * 5000))) < 1e-6,
    f"得到 {acct.cash}",
)
check("委託執行後清空", acct.pending == [])

# 只能買整張
odd = Account(cash=150_000.0)
odd.pending = [Order(code="TEST", side="BUY", shares=5000, decided_on="d")]
paper.execute_pending(odd, "2026-01-02", {"TEST": 100.0}, costs)
check(
    "資金不足時無條件捨去到整張（15 萬 → 1 張，非 1.5 張）",
    odd.positions["TEST"].shares == 1000,
    f"得到 {odd.positions['TEST'].shares}",
)

# 連一張都買不起
broke = Account(cash=50_000.0)
broke.pending = [Order(code="TEST", side="BUY", shares=1000, decided_on="d")]
rej = paper.execute_pending(broke, "2026-01-02", {"TEST": 100.0}, costs)
check(
    "買不起 1 張 → 退單，不會出現負現金",
    rej[0]["status"] == "REJECTED" and not broke.positions and broke.cash == 50_000.0,
    str(rej),
)

# 停牌 → 作廢不順延
halted = Account(cash=1_000_000.0)
halted.pending = [Order(code="TEST", side="BUY", shares=1000, decided_on="d")]
cancelled = paper.execute_pending(halted, "2026-01-02", {}, costs)
check(
    "當日無開盤價 → 委託作廢而非順延",
    cancelled[0]["status"] == "CANCELLED" and not halted.positions,
)

# 賣出：淨損益要扣掉手續費與證交稅
sell_acct = Account(cash=0.0)
sell_acct.positions["TEST"] = paper.PaperPosition(
    code="TEST", shares=1000, entry_price=100.0, entry_date="2026-01-02",
    entry_fee=85.0, peak_close=100.0, bars_held=5,
)
sell_acct.pending = [Order(code="TEST", side="SELL", shares=1000,
                           decided_on="d", reason=strategy.TAKE_PROFIT)]
sell_fills = paper.execute_pending(sell_acct, "2026-01-10", {"TEST": 120.0}, costs)
trade = sell_acct.trades[0]

gross = (costs.sell_price(120.0) - 100.0) * 1000
check(
    "賣出後部位清空、成交紀錄產生",
    not sell_acct.positions and len(sell_acct.trades) == 1,
)
check(
    "淨損益 = 毛損益 − 買賣手續費 − 證交稅",
    abs(trade.net_pnl - (gross - trade.fees - trade.tax)) < 1e-6,
    f"毛 {gross:.2f} 淨 {trade.net_pnl:.2f} 費 {trade.fees} 稅 {trade.tax}",
)
check(
    "淨損益比毛損益小（成本沒有被偷偷忽略）",
    trade.net_pnl < gross,
)


# ==========================================================================
print()
print("--- 每日流程（訊號 → 隔日開盤成交） ---")
# ==========================================================================

acct_params = AccountParams(initial_cash=1_000_000.0, position_pct=20.0,
                            max_positions=5)

signal_day = extend(flat_bars(80), open=100, high=105, low=100, close=105)
full = extend(signal_day, open=106, high=108, low=105, close=107)
series = Series(code="TEST", bars=full)
universe = {"TEST": series}

engine_acct = Account(cash=acct_params.initial_cash)
day1 = paper.run_day(engine_acct, full[80].date, universe, params,
                     acct_params, costs)

check("訊號日產生買進委託", len(day1.buy_orders) == 1, str(day1.buy_orders))
check("訊號日尚未成交（不用當日收盤價下單）", not engine_acct.positions)
check(
    "委託張數 ≈ 淨值的 20%",
    day1.buy_orders and day1.buy_orders[0].shares == 1000,
    str(day1.buy_orders[0].shares if day1.buy_orders else None),
)

day2 = paper.run_day(engine_acct, full[81].date, universe, params,
                     acct_params, costs)
filled = [f for f in day2.fills if f["status"] == "FILLED"]
check("隔日開盤成交", len(filled) == 1, str(day2.fills))
check(
    "成交價來自隔日開盤 106（＋滑價），不是訊號日收盤 105",
    filled and abs(filled[0]["price"] - 106 * 1.001) < 0.01,
    str(filled[0]["price"]) if filled else "",
)
check("淨值有被記錄下來", day2.equity > 0)

# 同一個部位不會重複買
day3 = paper.run_day(engine_acct, full[81].date, universe, params,
                     acct_params, costs)
check("已持有的標的不再產生買進委託", not day3.buy_orders)

# 名額上限
many_bars = extend(flat_bars(80), open=100, high=105, low=100, close=105)
many = {f"C{i}": Series(code=f"C{i}", bars=list(many_bars)) for i in range(8)}
cap_acct = Account(cash=10_000_000.0)
cap_day = paper.run_day(cap_acct, many_bars[80].date, many, params,
                        acct_params, costs)
check(
    f"8 檔同時有訊號，但只下 {acct_params.max_positions} 張委託（名額上限）",
    len(cap_day.buy_orders) == acct_params.max_positions,
    f"得到 {len(cap_day.buy_orders)}",
)

# 可重現性
rerun_acct = Account(cash=10_000_000.0)
rerun = paper.run_day(rerun_acct, many_bars[80].date, many, params,
                      acct_params, costs)
check(
    "同一份資料跑兩次得到完全相同的委託（結果可重現）",
    [o.code for o in rerun.buy_orders] == [o.code for o in cap_day.buy_orders],
)

# 沒進場的原因要留下來
check(
    "沒有進場的標的會記錄原因（報告要能回答「今天為什麼沒買」）",
    any("code" in r and "reason" in r for r in cap_day.rejected)
    or len(cap_day.buy_orders) == len(many),
)

no_signal_acct = Account(cash=1_000_000.0)
quiet = {"FLAT": Series(code="FLAT", bars=flat_bars(81))}
quiet_day = paper.run_day(no_signal_acct, flat_bars(81)[80].date, quiet,
                          params, acct_params, costs)
check(
    "沒訊號時明確記錄原因，而不是靜靜地什麼都不做",
    len(quiet_day.rejected) == 1 and "突破" in quiet_day.rejected[0]["reason"],
    str(quiet_day.rejected),
)


# ==========================================================================
print()
print("--- 帳戶狀態存取 ---")
# ==========================================================================

restored = Account.from_dict(engine_acct.to_dict())
check(
    "帳戶可以完整存檔再讀回（跨天執行的前提）",
    restored.cash == round(engine_acct.cash, 2)
    and set(restored.positions) == set(engine_acct.positions)
    and len(restored.pending) == len(engine_acct.pending),
)


# ==========================================================================
print()
print("--- 績效統計 ---")
# ==========================================================================

check(
    "最大回撤：100 → 120 → 90 應為 −25%",
    abs(paper.max_drawdown([100, 120, 90]) - (-25.0)) < 1e-9,
    f"得到 {paper.max_drawdown([100, 120, 90])}",
)
check("一路上漲 → 回撤 0%", paper.max_drawdown([100, 110, 120]) == 0.0)
check("空曲線不會爆炸", paper.max_drawdown([]) == 0.0)

sample = [
    paper.Trade(code="A", shares=1000, entry_date="d", entry_price=100.0,
                exit_date="d", exit_price=115.0, entry_reason="", exit_reason="",
                exit_detail="", fees=200.0, tax=300.0, bars_held=10),
    paper.Trade(code="B", shares=1000, entry_date="d", entry_price=100.0,
                exit_date="d", exit_price=92.0, entry_reason="", exit_reason="",
                exit_detail="", fees=200.0, tax=280.0, bars_held=6),
]
stats = paper.performance(sample, [1_000_000, 1_020_000, 1_006_020], 1_000_000)
check("勝率統計正確（1 勝 1 負 = 50%）", stats["win_rate"] == 50.0, str(stats))
check("交易成本有被加總", stats["total_fees"] == 980.0, str(stats["total_fees"]))
check(
    "獲利因子 = 總獲利 / 總虧損",
    stats["profit_factor"] is not None and stats["profit_factor"] > 1,
    str(stats["profit_factor"]),
)
check("零交易時不會除以零", paper.performance([], [], 1_000_000)["trades"] == 0)


# ==========================================================================
print()
print("--- 累計績效的來源是紀錄檔，不是帳戶狀態 ---")
# ==========================================================================
# 曾經的 bug：報告拿 account.trades 算勝率，但 load_state() 根本不還原歷史成交，
# 所以「完成交易 N 筆」每天都從零開始，而且網頁跟報告會顯示兩個不同的數字。

import json       # noqa: E402
import tempfile  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="paper-test-"))
paper.DATA_DIR = _tmp
paper.TRADES_FILE = _tmp / "paper_trades.jsonl"
paper.EQUITY_FILE = _tmp / "paper_equity.jsonl"
paper.STATE_FILE = _tmp / "paper_state.json"

for _t in sample:
    paper.append_trade(_t)

_reloaded = paper.load_trades()
check("成交紀錄可以完整讀回", len(_reloaded) == 2, f"讀回 {len(_reloaded)} 筆")
check(
    "讀回來的淨損益跟寫進去的一致（衍生欄位不會擋住還原）",
    _reloaded and abs(_reloaded[0].net_pnl - sample[0].net_pnl) < 1e-9,
)

_acct = Account(cash=500_000.0, last_date="2026-01-05")
_acct.trades.extend(sample)
paper.save_state(_acct)
_restored = paper.load_state(1_000_000.0)
check("狀態檔不負責記平倉交易", _restored.trades == [], str(_restored.trades))
check("但現金與日期有還原", _restored.cash == 500_000.0 and _restored.last_date == "2026-01-05")
check(
    "由紀錄檔算出的累計筆數 = 2（不會每天歸零）",
    paper.performance(paper.load_trades(), [1_000_000], 1_000_000)["trades"] == 2,
)


# ==========================================================================
print()
print("--- 狀態檔壞掉要出聲，不能安靜地重開一個新帳戶 ---")
# ==========================================================================
# 曾經的行為：JSONDecodeError 就回傳 Account(cash=initial_cash)。
# 帳戶被重設成 100 萬，但成交與淨值紀錄還留著舊的，
# 於是之後每一個績效數字都是錯的，而畫面上完全看不出來。

check(
    "第一次存檔沒有上一版可備份",
    not paper.state_backup_path().exists(),
)

_acct.cash = 123_456.0
paper.save_state(_acct)          # 第二次存檔
check("第二次存檔會留下上一版的備份", paper.state_backup_path().exists())
check(
    "備份裡是「上一版」而不是這次寫進去的內容",
    json.loads(paper.state_backup_path().read_text(encoding="utf-8"))["cash"] == 500_000.0,
)
check(
    "正本是這次的內容",
    paper.load_state(1_000_000.0).cash == 123_456.0,
)

paper.STATE_FILE.write_text('{"cash": 500000, "positio', encoding="utf-8")  # 半個 JSON
try:
    paper.load_state(1_000_000.0)
    check("壞掉的狀態檔會丟出 StateCorrupted", False, "沒有丟出例外就回傳了")
except paper.StateCorrupted as _exc:
    check("壞掉的狀態檔會丟出 StateCorrupted", True)
    check(
        "而且訊息會提到還有備份可以還原",
        "備份" in str(_exc),
        str(_exc),
    )

paper.STATE_FILE.unlink()
check(
    "狀態檔不存在＝第一次跑，這種情況才可以開新帳戶",
    paper.load_state(1_000_000.0).cash == 1_000_000.0,
)


# ==========================================================================
print()
print("--- 追蹤池少一檔，那檔的出場判斷會整段被跳過 ---")
# ==========================================================================
# 從觀察清單移除一檔仍被模擬倉持有的股票時，它會從 universe 消失，
# 於是永遠不做出場判斷、待賣委託也永遠成交不了——一張賣不掉的殭屍持股。
# paperdaily 必須把模擬倉現有持股併回追蹤池，這兩項就是在守那件事。

_crash_bars = seq_bars([100.0] * 70 + [80.0])
_last = _crash_bars[-1].date
_params = StrategyParams(stop_loss_pct=8.0, max_hold_bars=999)
_acct_params = AccountParams(initial_cash=1_000_000.0)
_costs = Costs()


def _holding_account() -> Account:
    acct = Account(cash=800_000.0)
    acct.positions["9999"] = paper.PaperPosition(
        code="9999", shares=1000, entry_price=100.0,
        entry_date=_crash_bars[0].date, entry_fee=20.0,
        peak_close=100.0, bars_held=5,
    )
    return acct


_with = _holding_account()
_res_with = paper.run_day(
    _with, _last, {"9999": Series(code="9999", bars=_crash_bars)},
    _params, _acct_params, _costs,
)
check(
    "在追蹤池裡：收盤 80 跌破停損線 92 → 產生賣單",
    len(_res_with.sell_orders) == 1,
    f"得到 {len(_res_with.sell_orders)} 張賣單",
)

_without = _holding_account()
_res_without = paper.run_day(
    _without, _last, {}, _params, _acct_params, _costs,
)
check(
    "不在追蹤池裡：同一根 K 完全不產生賣單",
    _res_without.sell_orders == [],
    str(_res_without.sell_orders),
)
check("而且部位還留著，不會自己消失", "9999" in _without.positions)
check(
    "已抱根數也停止累加（時間出場永遠不會觸發）",
    _without.positions["9999"].bars_held == 5,
    str(_without.positions["9999"].bars_held),
)


# ==========================================================================
print()
print("--- 資料不足的那天不算數，不能把 last_date 記掉 ---")
# ==========================================================================
# 真實事故：2026-08-20 早上執行時 data/history/ 幾乎是空的，
# 掃描池 40 檔沒幾檔有 61 根 K，於是「今天沒有訊號」——但那是假的，
# 它只代表沒東西可看。系統照樣寫下 last_date="2026-08-20"，
# 當天稍晚補齊歷史後重跑，得到的是「今天已經跑過」，那一天就此永久消失。
#
# 事後用補齊的資料重算，2603 其實成立（突破前 20 日高點 242，收 246）。
# 下面守的就是這件事：資料不足時整段不算數，補完資料重跑同一天要能補回來。

import history as _history        # noqa: E402
import paperdaily                 # noqa: E402

_guard_tmp = Path(tempfile.mkdtemp(prefix="paper-guard-"))
paper.DATA_DIR = _guard_tmp
paper.TRADES_FILE = _guard_tmp / "paper_trades.jsonl"
paper.EQUITY_FILE = _guard_tmp / "paper_equity.jsonl"
paper.STATE_FILE = _guard_tmp / "paper_state.json"
paper.RUNS_FILE = _guard_tmp / "paper_runs.jsonl"

_GUARD_DAY = _crash_bars[-1].date
_rich = seq_bars([100.0 + i * 0.5 for i in range(80)])
_rich_day = _rich[-1].date

_orig_universe = _history.universe_from_config
_orig_load_bars = _history.load_bars
_orig_load_config = paperdaily.load_config

_fake_history: dict[str, list] = {}


def _install_fake_history(bars_by_code: dict, min_ready: int) -> None:
    _fake_history.clear()
    _fake_history.update(bars_by_code)
    _history.universe_from_config = lambda: list(_fake_history)
    _history.load_bars = lambda code, *a, **k: _fake_history.get(code, [])
    paperdaily.load_config = lambda: {
        "enabled": True,
        "account": {"initial_cash": 1_000_000, "position_pct": 20.0, "max_positions": 5},
        "strategy": {"max_hold_bars": 999},
        "data_guard": {"min_ready_codes": min_ready},
    }


# 情境一：掃描池有 3 檔，但全部只有 5 根 K，門檻要求 10 檔 → 今天不算數
_install_fake_history({f"900{i}": flat_bars(5) for i in range(3)}, min_ready=10)
_thin = paperdaily.run_daily(_GUARD_DAY, {}, dry_run=False)
check(
    "資料不足時回報「今天不算數」而不是「今天沒有訊號」",
    _thin.get("insufficient") is True,
    str(_thin)[:120],
)
check(
    "資料不足時不會寫出狀態檔（last_date 沒有被記掉）",
    not paper.STATE_FILE.exists(),
)
check(
    "資料不足時不會污染淨值曲線",
    not paper.EQUITY_FILE.exists(),
)
check(
    "訊息要講清楚補完資料可以重跑，不是叫人放棄",
    "重跑" in _thin.get("skipped", ""),
    _thin.get("skipped", "")[:120],
)

# 情境二：同一天，資料補齊了 → 這次要真的跑起來，並記下 last_date
_install_fake_history({f"900{i}": _rich for i in range(12)}, min_ready=10)
_ok = paperdaily.run_daily(_rich_day, {}, dry_run=False)
check(
    "資料補齊後同一個交易日可以補跑（沒有被永久鎖住）",
    "result" in _ok,
    str(_ok)[:120],
)
check(
    "補跑之後才寫入狀態檔",
    paper.STATE_FILE.exists()
    and paper.load_state(1_000_000.0).last_date == _rich_day,
)

# 情境三：門檻設 0 = 明確關掉保護，維持舊行為
_install_fake_history({"9001": flat_bars(5)}, min_ready=0)
_off = paperdaily.run_daily("2026-12-31", {}, dry_run=False)
check(
    "門檻設 0 就不擋（保護是可以關掉的，但要明確寫出來）",
    _off.get("insufficient") is None,
    str(_off)[:120],
)

# 稽核紀錄：每一次執行都要留下一行，包含「掃了幾檔、幾檔資料夠」
_runs = paper.load_runs()
check(
    "每次執行都留下稽核紀錄",
    len(_runs) == 3,
    f"得到 {len(_runs)} 行",
)
check(
    "稽核紀錄記下了資料不足的原因與當時的檔數",
    _runs and _runs[0]["status"] == "insufficient_data"
    and _runs[0]["ready"] == 0 and _runs[0]["universe"] == 3,
    str(_runs[0]) if _runs else "(空)",
)
check(
    "成功執行那次記下了訊號數與淨值",
    len(_runs) > 1 and _runs[1]["status"] == "ok" and "equity" in _runs[1],
    str(_runs[1]) if len(_runs) > 1 else "(空)",
)

# dry-run 不留任何痕跡，跟其他寫檔路徑一致
_before = len(paper.load_runs())
paperdaily.run_daily("2026-12-30", {}, dry_run=True)
check(
    "dry-run 不寫稽核紀錄",
    len(paper.load_runs()) == _before,
)

_history.universe_from_config = _orig_universe
_history.load_bars = _orig_load_bars
paperdaily.load_config = _orig_load_config


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
