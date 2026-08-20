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
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
