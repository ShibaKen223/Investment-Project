"""把模擬倉接進每日流程。

main.py 每天抓完收盤行情之後呼叫 run_daily()，它負責:
    1. 把今天的收盤行情補進 data/history/（歷史會自己一天一天長出來）
    2. 讀出模擬倉狀態
    3. 用「昨天決定的委託」以今天開盤價成交
    4. 用今天收盤產生「明天要下的委託」
    5. 存檔：狀態、成交紀錄、淨值曲線

跑的是 paper.run_day()，跟回測完全同一支函式。

模擬倉關掉（config/paper.yaml 的 enabled: false）或設定檔不存在時，
整段會安靜跳過，不影響原本的持股監控報告。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import history  # noqa: E402
import paper  # noqa: E402
from datasource import Quote  # noqa: E402
from history import Bar  # noqa: E402
from paper import AccountParams, Costs, Series  # noqa: E402
from strategy import StrategyParams  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER_CONFIG = ROOT / "config" / "paper.yaml"


def load_config() -> dict:
    if not PAPER_CONFIG.exists():
        return {}
    with PAPER_CONFIG.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def is_enabled(config: dict) -> bool:
    return bool(config.get("enabled", False))


def ingest_quotes(codes: list[str], quotes: dict[str, Quote]) -> list[str]:
    """把今天的行情寫進歷史日 K。回傳成功寫入的代號。

    開高低任一欄缺值就跳過——盤中無成交的標的，
    用收盤價補出來的假 K 棒會污染均線。
    """
    written: list[str] = []
    for code in codes:
        quote = quotes.get(code)
        if quote is None:
            continue
        if None in (quote.open, quote.high, quote.low):
            continue
        bar = Bar(
            date=quote.trade_date,
            open=float(quote.open),
            high=float(quote.high),
            low=float(quote.low),
            close=float(quote.close),
            volume=int(quote.volume or 0),
        )
        if bar.is_valid:
            history.save_bars(code, [bar])
            written.append(code)
    return written


def run_daily(
    trade_date: str,
    quotes: dict[str, Quote],
    dry_run: bool = False,
) -> dict | None:
    """跑一天的模擬倉。回傳給報告用的 dict；未啟用時回傳 None。"""
    config = load_config()
    if not is_enabled(config):
        return None

    account_params = AccountParams.from_dict(config.get("account"))
    costs = Costs.from_dict(config.get("costs"))
    params = StrategyParams.from_dict(config.get("strategy"))

    account = paper.load_state(account_params.initial_cash)

    # 追蹤池 = 設定裡的持股+觀察清單，「加上」模擬倉現在還抱著的代號。
    #
    # 少了後面那半會出事：從觀察清單移掉一檔股票時，如果模擬倉正持有它，
    # 它就會從 universe 消失——沒有開盤價可以成交待賣委託、bars_held 不再增加、
    # 出場判斷整段被跳過。那筆部位會變成一張永遠賣不掉的殭屍持股，
    # 而且淨值還會用進場價估給你看，畫面上完全看不出哪裡不對。
    codes = history.universe_from_config()
    orphaned = [c for c in account.positions if c not in codes]
    codes = codes + orphaned

    if not codes:
        return {"skipped": "目前沒有登記任何持股或觀察清單標的。"}

    if not dry_run:
        ingest_quotes(codes, quotes)

    universe: dict[str, Series] = {}
    warmup_short: list[str] = []
    for code in codes:
        bars = history.load_bars(code)
        if not bars:
            continue
        universe[code] = Series(code=code, bars=bars)
        if len(bars) < params.warmup_bars:
            warmup_short.append(f"{code}（{len(bars)}/{params.warmup_bars} 根）")

    if not universe:
        return {
            "skipped": "還沒有任何歷史日 K，資料會隨系統每天執行慢慢累積，"
            "累積到足夠天數後這裡就會開始有內容。"
        }

    # 同一個交易日重跑不應該重複成交一次。
    if account.last_date == trade_date:
        return {
            "skipped": f"模擬倉今天（{trade_date}）已經跑過，未重複執行。",
            "equity": account.equity(
                {c: s.bars[-1].close for c, s in universe.items()}
            ),
        }

    before_trades = len(account.trades)
    result = paper.run_day(
        account, trade_date, universe, params, account_params, costs
    )

    if not dry_run:
        for trade in account.trades[before_trades:]:
            paper.append_trade(trade)
        paper.append_equity(result)
        paper.save_state(account)

    # 績效一律以 append-only 的紀錄檔為準，不要用 account.trades——
    # load_state() 不還原歷史成交，account.trades 只有「這次執行剛平倉」的那幾筆，
    # 拿它算勝率會讓報告上的累計數字每天從零開始。
    #
    # 非 dry-run 時上面已經 append 過了，讀回來就含今天；
    # dry-run 沒寫檔，所以把今天的結果手動補上，數字才跟實跑一致。
    all_trades = paper.load_trades()
    curve = paper.load_equity_values()
    if dry_run:
        all_trades = all_trades + account.trades[before_trades:]
        curve = curve + [result.equity]

    stats = paper.performance(
        all_trades, curve or [result.equity], account_params.initial_cash
    )

    return {
        "result": result,
        "account": account,
        "stats": stats,
        "params": params,
        "account_params": account_params,
        "warmup_short": warmup_short,
        "orphaned": orphaned,
        "closes": {c: s.bars[-1].close for c, s in universe.items()},
    }
