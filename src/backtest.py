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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

# Windows 的主控台預設是 GBK/cp950，而這支程式的輸出全是中文，還帶著
# ⚠ 🔴 之類的符號——不改編碼的話，一遇到 GBK 放不進去的字元就直接
# UnicodeEncodeError 中斷，報告只印出前面半段。tests/ 底下每一支都做了
# 同樣的事（見 commit 00c43f2），但正式的進入點當時漏掉了。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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
        bars = history.load_bars_adjusted(code)
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


@dataclass
class RunResult:
    """一次回測的完整輸出。

    exposures 是每天「投在市場上的比例」＝(淨值 − 現金) ÷ 淨值。
    沒有它，最大回撤這個數字會騙人：策略如果七成時間空手，
    回撤當然小，但那不是風險控制得好，是根本沒承擔風險。
    """

    account: Account
    curve: list[float] = field(default_factory=list)
    curve_dates: list[str] = field(default_factory=list)
    exposures: list[float] = field(default_factory=list)

    @property
    def avg_exposure_pct(self) -> float:
        if not self.exposures:
            return 0.0
        return sum(self.exposures) / len(self.exposures) * 100

    @property
    def flat_days_pct(self) -> float:
        """完全空手的日子佔幾 %。"""
        if not self.exposures:
            return 0.0
        return sum(1 for e in self.exposures if e < 0.01) / len(self.exposures) * 100


def run(
    universe: dict[str, Series],
    account_params: AccountParams,
    costs: Costs,
    params: StrategyParams,
    start: str | None = None,
    end: str | None = None,
    verbose: bool = False,
) -> RunResult:
    """跑完整段歷史。"""
    account = Account(cash=account_params.initial_cash)
    out = RunResult(account=account)

    for day in trading_days(universe):
        if start and day < start:
            continue
        if end and day > end:
            continue

        result = paper.run_day(
            account, day, universe, params, account_params, costs
        )
        out.curve.append(result.equity)
        out.curve_dates.append(day)
        out.exposures.append(
            (result.equity - result.cash) / result.equity if result.equity else 0.0
        )

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

    return out


BENCHMARK_CODE = "0050"


def benchmark_return(
    code: str, start: str, end: str
) -> tuple[float | None, str, str]:
    """基準的買進持有報酬（%），以及實際涵蓋的起訖日。

    一定要用還原權值後的價格。0050 在 2025-06-18 做過 1 拆 4，
    用原始價格算出來的「買進持有」是 −44%，實際上是 +123%——
    拿那個數字當基準，任何策略看起來都會像天才。
    """
    bars = [b for b in history.load_bars_adjusted(code) if start <= b.date <= end]
    if len(bars) < 2 or bars[0].close <= 0:
        return None, "", ""
    return (bars[-1].close / bars[0].close - 1) * 100, bars[0].date, bars[-1].date


def print_report(
    result: RunResult,
    account_params: AccountParams,
    params: StrategyParams,
) -> None:
    account, curve, curve_dates = result.account, result.curve, result.curve_dates
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

    # --- 對照組 ---
    # 沒有對照組的報酬數字沒有意義：多頭裡隨便買都會賺。
    # strategy.yaml 的 objective 明文寫著要贏 0050 買進持有，
    # 那就該把那個數字印在旁邊，而不是讓人自己去算。
    bench = bench_start = bench_end = None
    if curve_dates:
        bench, bench_start, bench_end = benchmark_return(
            BENCHMARK_CODE, curve_dates[0], curve_dates[-1]
        )
    if bench is not None:
        gap = stats["total_return_pct"] - bench
        verdict = "✅ 勝過基準" if gap > 0 else "❌ 輸給基準"
        print("-" * 62)
        print(f"{BENCHMARK_CODE} 買進持有  {bench:>13.2f}%   "
              f"（{bench_start} ~ {bench_end}）")
        print(f"超額報酬      {gap:>13.2f}%   {verdict}")
    elif curve_dates:
        print("-" * 62)
        print(f"{BENCHMARK_CODE} 買進持有            —   "
              f"（沒有 {BENCHMARK_CODE} 的歷史資料，無法比較）")

    # --- 曝險 ---
    # 回撤要跟曝險一起看。七成時間空手的策略，回撤小是理所當然的，
    # 那不代表風險控制得好——代表資金大部分時間沒在承擔風險。
    print("-" * 62)
    print(f"平均曝險      {result.avg_exposure_pct:>13.1f}%   "
          f"（資金實際投在市場上的比例）")
    print(f"完全空手      {result.flat_days_pct:>13.1f}%   的交易日")
    if result.avg_exposure_pct > 0:
        scaled = stats["total_return_pct"] / (result.avg_exposure_pct / 100)
        print(f"曝險調整後    {scaled:>13.2f}%   "
              f"（滿倉的話大約會是這個數量級，僅供對照）")
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

    _print_objective_check(stats, bench, result, account)


def _print_objective_check(
    stats: dict, bench: float | None, result: RunResult, account: Account
) -> None:
    """把結果直接對照 strategy.yaml 裡自己訂的門檻。

    門檻寫在設定檔裡但從來沒有人去對，等於沒有門檻。
    這一段就是把「我說過要達到什麼」跟「實際上是多少」放在一起。
    """
    print()
    print("對照 strategy.yaml 的 objective：")

    checks: list[tuple[str, bool | None, str]] = [
        (
            "完成交易 ≥ 30 筆",
            stats["trades"] >= 30,
            f"{stats['trades']} 筆",
        ),
        (
            "勝率 ≥ 50%",
            stats["win_rate"] >= 50,
            f"{stats['win_rate']:.1f}%",
        ),
        (
            "獲利因子 ≥ 1.5",
            (stats["profit_factor"] or 0) >= 1.5,
            str(stats["profit_factor"]) if stats["profit_factor"] else "—",
        ),
        (
            "最大回撤 ≤ 15%",
            abs(stats["max_drawdown_pct"]) <= 15,
            f"{stats['max_drawdown_pct']:.2f}%",
        ),
        (
            f"總報酬勝過 {BENCHMARK_CODE} 買進持有",
            None if bench is None else stats["total_return_pct"] > bench,
            "無法比較" if bench is None
            else f"{stats['total_return_pct']:.2f}% vs {bench:.2f}%",
        ),
    ]

    for label, passed, actual in checks:
        mark = "—" if passed is None else ("✅" if passed else "❌")
        print(f"  {mark}  {label:<28} 實際 {actual}")

    # 樣本品質：43 筆聽起來夠了，但如果集中在少數幾檔、同一段行情，
    # 有效的獨立樣本遠少於 43。這件事不講，門檻就只是在自我安慰。
    traded_codes = {t.code for t in account.trades}
    if stats["trades"] >= 30 and len(traded_codes) < 15:
        print()
        print(
            f"  ⚠️  {stats['trades']} 筆交易只分布在 {len(traded_codes)} 檔標的上，"
            "集中度偏高。"
        )
        print("      同一段行情、少數幾檔貢獻大部分損益時，")
        print("      有效的獨立樣本會遠少於交易筆數——別把它當成 30 筆的證據。")

    if result.avg_exposure_pct < 40 and abs(stats["max_drawdown_pct"]) <= 15:
        print()
        print(
            f"  ⚠️  回撤只有 {stats['max_drawdown_pct']:.2f}%，"
            f"但平均曝險也只有 {result.avg_exposure_pct:.1f}%。"
        )
        print("      「回撤 ≤ 15%」這條門檻在這種曝險下等於自動過關，")
        print("      它現在沒有在管任何事情。")


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

    result = run(
        universe, account_params, costs, params,
        start=args.start, end=args.end, verbose=args.verbose,
    )
    print_report(result, account_params, params)


if __name__ == "__main__":
    main()
