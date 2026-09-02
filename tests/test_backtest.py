"""回測引擎的端對端測試（合成資料，不連外網）。

跑法:
    python3 tests/test_backtest.py

這支測的不是「策略賺不賺錢」——合成資料證明不了那件事。
它測的是**引擎有沒有作弊**：
    現金會不會變負的、部位會不會超過上限、
    淨值等式對不對、成交價是不是真的來自隔日開盤。

回測結果只要有一條不變式被破壞，後面所有績效數字都不用看了。
"""

from __future__ import annotations

import random
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import backtest  # noqa: E402
import paper  # noqa: E402
from history import Bar  # noqa: E402
from paper import Account, AccountParams, Costs, Series  # noqa: E402
from strategy import StrategyParams  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def synth_series(code: str, days: int, seed: int, drift: float = 0.0006) -> Series:
    """帶漂移的隨機漫步。種子固定，所以每次跑結果完全一樣。"""
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = 100.0
    day = date(2024, 1, 1)
    while len(bars) < days:
        if day.weekday() < 5:
            ret = rng.gauss(drift, 0.018)
            close = max(price * (1 + ret), 1.0)
            open_ = price * (1 + rng.gauss(0, 0.004))
            high = max(open_, close) * (1 + abs(rng.gauss(0, 0.005)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, 0.005)))
            bars.append(
                Bar(
                    date=day.isoformat(),
                    open=round(open_, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    volume=rng.randint(800_000, 5_000_000),
                )
            )
            price = close
        day += timedelta(days=1)
    return Series(code=code, bars=bars)


universe = {
    f"T{i}": synth_series(f"T{i}", days=500, seed=1000 + i) for i in range(6)
}

account_params = AccountParams(initial_cash=1_000_000.0, position_pct=20.0,
                               max_positions=5)
costs = Costs()
params = StrategyParams()

# 一邊跑一邊檢查不變式
peak_positions = 0
negative_cash = False
equity_mismatch = 0.0

account = Account(cash=account_params.initial_cash)
curve: list[float] = []
for day in backtest.trading_days(universe):
    result = paper.run_day(account, day, universe, params, account_params, costs)
    curve.append(result.equity)

    peak_positions = max(peak_positions, len(account.positions))
    if account.cash < -1e-6:
        negative_cash = True

    closes = {
        c: bar.close
        for c, s in universe.items()
        if (bar := s.bar_on(day)) is not None
    }
    holdings = sum(
        p.shares * closes.get(c, p.entry_price) for c, p in account.positions.items()
    )
    equity_mismatch = max(
        equity_mismatch, abs(result.equity - (account.cash + holdings))
    )

stats = paper.performance(account.trades, curve, account_params.initial_cash)

print("--- 不變式 ---")
check("現金從未變成負數", not negative_cash)
check(
    f"同時持有檔數未超過上限 {account_params.max_positions}",
    peak_positions <= account_params.max_positions,
    f"最高到 {peak_positions}",
)
check(
    "淨值 = 現金 + 持股市值（每一天都成立）",
    equity_mismatch < 1e-6,
    f"最大誤差 {equity_mismatch}",
)
check(
    "出場日不早於進場日",
    all(t.exit_date >= t.entry_date for t in account.trades),
)
check(
    "每筆交易都有進出場理由（報告要說得出為什麼）",
    all(t.exit_reason for t in account.trades),
)
check(
    "每筆交易的成本都大於零（手續費沒有被跳過）",
    all(t.fees > 0 and t.tax >= 0 for t in account.trades),
)
check(
    "淨損益一律小於毛損益",
    all(t.net_pnl < t.gross_pnl for t in account.trades),
)

print()
print("--- 引擎有沒有作弊 ---")
check(
    "成交價都落在成交當日的高低價區間內（沒有用不存在的價格成交）",
    all(
        (bar := universe[t.code].bar_on(t.exit_date)) is not None
        and bar.low * 0.995 <= t.exit_price <= bar.high * 1.005
        for t in account.trades
    ),
)
check(
    f"平均持有天數落在波段區間（≤ {params.max_hold_bars} 根）",
    stats["trades"] == 0 or stats["avg_bars_held"] <= params.max_hold_bars,
    f"得到 {stats['avg_bars_held']}",
)
check(
    "有實際產生交易（引擎不是空轉）",
    stats["trades"] > 0,
    f"得到 {stats['trades']} 筆",
)

# 可重現性：同樣的輸入跑第二次要得到一模一樣的結果
account2 = Account(cash=account_params.initial_cash)
curve2: list[float] = []
for day in backtest.trading_days(universe):
    curve2.append(
        paper.run_day(account2, day, universe, params, account_params, costs).equity
    )
check("整段回測可完整重現（同輸入同輸出）", curve == curve2)

print()
print("--- 參數敏感度（引擎有在回應設定，不是寫死的） ---")
tight = StrategyParams(stop_loss_pct=2.0)
acct_tight = Account(cash=account_params.initial_cash)
for day in backtest.trading_days(universe):
    paper.run_day(acct_tight, day, universe, tight, account_params, costs)
tight_stats = paper.performance(acct_tight.trades, [1.0], 1.0)
check(
    "停損收緊到 2% → 停損出場的比例明顯上升",
    sum(1 for t in acct_tight.trades if t.exit_reason == "STOP_LOSS")
    > sum(1 for t in account.trades if t.exit_reason == "STOP_LOSS"),
    f"2%: {sum(1 for t in acct_tight.trades if t.exit_reason == 'STOP_LOSS')} 筆 / "
    f"8%: {sum(1 for t in account.trades if t.exit_reason == 'STOP_LOSS')} 筆",
)

solo = AccountParams(initial_cash=1_000_000.0, position_pct=20.0, max_positions=1)
acct_solo = Account(cash=solo.initial_cash)
for day in backtest.trading_days(universe):
    paper.run_day(acct_solo, day, universe, params, solo, costs)
check(
    "上限改成 1 檔 → 交易筆數下降",
    len(acct_solo.trades) < len(account.trades),
    f"1 檔: {len(acct_solo.trades)} 筆 / 5 檔: {len(account.trades)} 筆",
)

print()
print(f"（參考數字：合成資料上完成 {stats['trades']} 筆交易，"
      f"總報酬 {stats['total_return_pct']:+.2f}%，"
      f"最大回撤 {stats['max_drawdown_pct']:.2f}%，"
      f"手續費+稅 {stats['total_fees']:,.0f} 元）")
print("  ⚠️ 這是隨機漫步造出來的假資料，上面的報酬不代表任何事情。")

print()
print("--- ATR 模式端對端 ---")

atr_params = StrategyParams(stop_mode="atr", atr_stop_multiple=2.0,
                            atr_target_multiple=3.5)
acct_atr = Account(cash=account_params.initial_cash)
curve_atr: list[float] = []
for day in backtest.trading_days(universe):
    curve_atr.append(
        paper.run_day(
            acct_atr, day, universe, atr_params, account_params, costs
        ).equity
    )
atr_stats = paper.performance(
    acct_atr.trades, curve_atr, account_params.initial_cash
)

check(
    "ATR 模式跑得完整段回測並產生交易",
    atr_stats["trades"] > 0,
    f"得到 {atr_stats['trades']} 筆",
)
check(
    "淨值曲線與固定百分比模式不同（設定真的有生效）",
    curve_atr != curve,
)

# 每個進場的部位都要記下當下的 ATR，否則停損線無從還原
atr_recorded = True
acct_probe = Account(cash=account_params.initial_cash)
seen_atr: list[float] = []
for day in backtest.trading_days(universe):
    paper.run_day(acct_probe, day, universe, atr_params, account_params, costs)
    for pos in acct_probe.positions.values():
        seen_atr.append(pos.entry_atr)
        if pos.entry_atr <= 0:
            atr_recorded = False
check(
    "每個部位都記錄了進場當下的 ATR（停損線可事後還原）",
    atr_recorded and seen_atr,
    f"共觀察 {len(seen_atr)} 次持倉狀態",
)

check(
    "ATR 模式下現金一樣不會變負",
    acct_atr.cash >= -1e-6,
    str(acct_atr.cash),
)
check(
    "ATR 模式的成交價一樣落在當日高低區間內",
    all(
        (bar := universe[t.code].bar_on(t.exit_date)) is not None
        and bar.low * 0.995 <= t.exit_price <= bar.high * 1.005
        for t in acct_atr.trades
    ),
)
check(
    "停損出場的紀錄帶著 ATR 基準說明",
    all(
        "ATR" in t.exit_detail
        for t in acct_atr.trades
        if t.exit_reason == "STOP_LOSS"
    ) or not any(t.exit_reason == "STOP_LOSS" for t in acct_atr.trades),
)

# 倍數放大 → 停損更寬 → 因停損出場的比例應下降
loose = StrategyParams(stop_mode="atr", atr_stop_multiple=6.0,
                       atr_target_multiple=3.5)
acct_loose = Account(cash=account_params.initial_cash)
for day in backtest.trading_days(universe):
    paper.run_day(acct_loose, day, universe, loose, account_params, costs)
check(
    "ATR 倍數從 2 放寬到 6 → 停損出場變少",
    sum(1 for t in acct_loose.trades if t.exit_reason == "STOP_LOSS")
    < sum(1 for t in acct_atr.trades if t.exit_reason == "STOP_LOSS"),
    f"2×: {sum(1 for t in acct_atr.trades if t.exit_reason == 'STOP_LOSS')} 筆 / "
    f"6×: {sum(1 for t in acct_loose.trades if t.exit_reason == 'STOP_LOSS')} 筆",
)

print()
print(f"（參考：ATR 模式完成 {atr_stats['trades']} 筆，"
      f"總報酬 {atr_stats['total_return_pct']:+.2f}%，"
      f"最大回撤 {atr_stats['max_drawdown_pct']:.2f}%；"
      f"固定 % 模式為 {stats['trades']} 筆 / "
      f"{stats['total_return_pct']:+.2f}% / {stats['max_drawdown_pct']:.2f}%）")
print("  ⚠️ 兩者都是隨機漫步的假資料，不代表哪一種比較好。")

# ==========================================================================
print()
print("--- 同曝險回撤門檻 ---")
# ==========================================================================
# 這條門檻取代了原本的「最大回撤 ≤ 15%」絕對數字。
# 它存在的唯一理由是「絕對數字在低曝險下等於自動過關」，
# 所以最重要的測試是**它真的會判失敗**——一個永遠 ✅ 的檢查
# 跟沒有檢查是同一件事。


def _fake_result(exposure_pct: float) -> backtest.RunResult:
    r = backtest.RunResult(account=Account(cash=0.0))
    r.exposures = [exposure_pct / 100]
    return r


# 0050 同期回撤 27.48%。曝險 65.3% → 同曝險基準 17.94%。
_, passed, detail = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -15.76}, _fake_result(65.3), -27.48
)
check("回撤 15.76% 在 65.3% 曝險下通過（基準 17.94%）", passed is True, detail)

_, passed, detail = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -19.0}, _fake_result(65.3), -27.48
)
check("同樣曝險下回撤 19% 要判失敗", passed is False, detail)

# 這是整條門檻的重點：低曝險時它會收緊，不會自動過關。
# 回撤 12% 在絕對門檻（20%）下輕鬆過關，但曝險只有 30% 時
# 同曝險基準是 8.24%，該判失敗。
_, passed, detail = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -12.0}, _fake_result(30.0), -27.48
)
check(
    "低曝險不會自動過關：回撤 12% / 曝險 30% 要判失敗（絕對門檻卻會放行）",
    passed is False,
    detail,
)
check(
    "同一組數字在絕對門檻 20% 下確實會過關（證明兩條門檻不是重複的）",
    abs(-12.0) <= 20,
)

# 曝險升高時門檻自己放寬，所以不能靠「把錢投出去」來繞過——
# 也不能靠「縮手」來假裝風險低。
_, tight, _ = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -10.0}, _fake_result(30.0), -27.48
)
_, loose, _ = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -10.0}, _fake_result(90.0), -27.48
)
check(
    "同一個回撤，曝險越低越難通過（門檻會跟著曝險收緊）",
    tight is False and loose is True,
    f"曝險30%={tight} 曝險90%={loose}",
)

_, passed, detail = backtest._exposure_matched_drawdown_check(
    {"max_drawdown_pct": -15.0}, _fake_result(0.0), -27.48
)
check("完全空手時無法比較，回傳 None 而不是假裝通過", passed is None, detail)

print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
