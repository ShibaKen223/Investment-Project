"""回測：用歷史日 K 把模擬倉從頭跑一遍。

    python3 src/backtest.py                    # 用持股 + 觀察清單，跑全部歷史
    python3 src/backtest.py --from 2025-01-01
    python3 src/backtest.py --codes 2330,2412 --verbose

為什麼要有回測，明明已經有模擬倉:
    模擬倉一天只前進一天，要三個月才看得出策略好壞。
    回測讓你在寫完策略的當天就知道「這組參數在過去兩年會做出什麼決定」。
    兩者跑的是同一個 paper.run_day()，所以回測看到的行為就是實跑的行為。

回測不能證明的事（請一起記住）:
    - 過去有效不代表未來有效。
    - 樣本內最佳化出來的參數，通常在樣本外會退化。
    - 這裡沒有模擬「買不到」的情況：假設你要的張數在開盤都成交得掉。
      對日均量 500 張以上、每次只買 20% 部位的規模來說還算合理，
      但資金放大之後這個假設會失真。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import history  # noqa: E402
import paper  # noqa: E402
from paper import Account, AccountParams, Costs, Series  # noqa: E402
from strategy import StrategyParams  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER_CONFIG = ROOT / "config" / "paper.yaml"


def load_config() -> tuple[AccountParams, Costs, StrategyParams, dict]:
    raw = {}
    if PAPER_CONFIG.exists():
        with PAPER_CONFIG.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    return (
        AccountParams.from_dict(raw.get("account")),
        Costs.from_dict(raw.get("costs")),
        StrategyParams.from_dict(raw.get("strategy")),
        raw,
    )


def load_universe(codes: list[str]) -> dict[str, Series]:
    """讀出每檔的歷史日 K。沒有資料的會被跳過並提醒。"""
    universe: dict[str, Series] = {}
    for code in codes:
        bars = history.load_bars(code)
        if not bars:
            print(f"⚠️  {code} 沒有歷史資料，已跳過。先跑 src/history.py 補。")
            continue
        universe[code] = Series(code=code, bars=bars)
    return universe


def trading_days(universe: dict[str, Series]) -> list[str]:
    """所有標的日期的聯集，由舊到新。"""
    days: set[str] = set()
    for series in universe.values():
        days.update(b.date for b in series.bars)
    return sorted(days)


def run(
    universe: dict[str, Series],
    account_params: AccountParams,
    costs: Costs,
    params: StrategyParams,
    start: str | None = None,
    end: str | None = None,
    verbose: bool = False,
) -> tuple[Account, list[float], list[str]]:
    """跑完整段歷史，回傳 (帳戶, 淨值曲線, 對應日期)。"""
    account = Account(cash=account_params.initial_cash)
    curve: list[float] = []
    curve_dates: list[str] = []

    for day in trading_days(universe):
        if start and day < start:
            continue
        if end and day > end:
            continue

        result = paper.run_day(
            account, day, universe, params, account_params, costs
        )
        curve.append(result.equity)
        curve_dates.append(day)

        if verbose:
            for fill in result.fills:
                if fill.get("status") == "FILLED":
                    extra = (
                        f"  損益 {fill['net_pnl']:+,.0f} ({fill['net_pnl_pct']:+.2f}%)"
                        if fill["side"] == "SELL"
                        else ""
                    )
                    print(
                        f"{day}  {fill['side']:4} {fill['code']:>6} "
                        f"{fill['shares']:>6} 股 @ {fill['price']:>9,.2f}{extra}"
                    )

    return account, curve, curve_dates


def print_report(
    account: Account,
    curve: list[float],
    curve_dates: list[str],
    account_params: AccountParams,
    params: StrategyParams,
) -> None:
    stats = paper.performance(account.trades, curve, account_params.initial_cash)

    print()
    print("=" * 62)
    print("模擬倉回測結果")
    print("=" * 62)
    if curve_dates:
        print(f"期間          {curve_dates[0]} ~ {curve_dates[-1]}"
              f"（{len(curve_dates)} 個交易日）")
    print(f"初始資金      {stats['initial_cash']:>14,.0f}")
    print(f"期末淨值      {stats['final_equity']:>14,.0f}")
    print(f"總報酬        {stats['total_return_pct']:>13.2f}%")
    print(f"最大回撤      {stats['max_drawdown_pct']:>13.2f}%")
    print("-" * 62)
    print(f"完成交易      {stats['trades']:>14} 筆")
    print(f"勝 / 負       {stats['wins']:>7} / {stats['losses']:<6}"
          f"  勝率 {stats['win_rate']:.1f}%")
    print(f"平均獲利      {stats['avg_win']:>14,.0f}")
    print(f"平均虧損      {stats['avg_loss']:>14,.0f}")
    pf = stats["profit_factor"]
    print(f"獲利因子      {pf if pf is not None else '—（沒有虧損交易）':>14}")
    print(f"平均持有      {stats['avg_bars_held']:>14} 根 K")
    print(f"手續費+稅     {stats['total_fees']:>14,.0f}")
    print("=" * 62)

    if stats["trades"] == 0:
        print()
        print("一筆交易都沒有。可能的原因：")
        print(f"  · 歷史資料不足 {params.warmup_bars} 根，暖身期就用完了")
        print("  · 條件太嚴（四個條件要同時成立）")
        print("  · 標的太少 —— 只掃 5 檔的話，好幾個月沒訊號很正常")
    elif stats["trades"] < 20:
        print()
        print(f"⚠️  只有 {stats['trades']} 筆交易，樣本太小，這組數字沒有統計意義。")
        print("    不要根據它調參數。至少要 30～50 筆才勉強能看出傾向。")

    if account.positions:
        print()
        print("回測結束時仍持有：")
        for code, pos in account.positions.items():
            print(
                f"  {code}  {pos.shares:>6} 股 @ {pos.entry_price:,.2f}"
                f"  已抱 {pos.bars_held} 根"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="模擬倉回測")
    parser.add_argument("--codes", help="逗號分隔代號；省略則用持股 + 觀察清單")
    parser.add_argument("--from", dest="start", help="起始日 YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="結束日 YYYY-MM-DD")
    parser.add_argument("--verbose", action="store_true", help="印出每一筆成交")
    args = parser.parse_args()

    account_params, costs, params, _ = load_config()
    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else history.universe_from_config()
    )
    universe = load_universe(codes)
    if not universe:
        raise SystemExit(
            "沒有任何標的有歷史資料。先跑:\n"
            "    python3 src/history.py --months 24"
        )

    account, curve, curve_dates = run(
        universe, account_params, costs, params,
        start=args.start, end=args.end, verbose=args.verbose,
    )
    print_report(account, curve, curve_dates, account_params, params)


if __name__ == "__main__":
    main()
