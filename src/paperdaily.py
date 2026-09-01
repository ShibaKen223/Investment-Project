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

⚠️ 關於「今天不算數」
    這支模組唯一會造成永久損失的行為，是在**資料還沒到齊**的時候
    宣告自己跑完並寫下 last_date——那一天就此被鎖住，之後補了資料也回不去。
    實際發生過：2026-08-20 早上執行時 data/history/ 幾乎是空的，
    掃描池 40 檔沒幾檔有 61 根 K，於是「沒有訊號」；
    當天稍晚歷史補齊後重算，其實 2603 是成立的，但那一天已經記掉了。

    所以現在多了一道 data_guard：資料足夠的檔數低於門檻就整段不算數，
    不成交、不寫 last_date、不記淨值。補完資料重跑同一天就會補回來。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import history  # noqa: E402
import paper  # noqa: E402
from datasource import Quote  # noqa: E402
from history import Bar  # noqa: E402
from paper import AccountParams, Costs, Series  # noqa: E402
from strategy import StrategyParams, check_entry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER_CONFIG = ROOT / "config" / "paper.yaml"

# 掃描池裡至少要有這麼多檔「資料足夠」才算數。
# 可在 config/paper.yaml 的 data_guard.min_ready_codes 覆寫；設 0 = 關掉保護。
DEFAULT_MIN_READY_CODES = 10


def load_config() -> dict:
    if not PAPER_CONFIG.exists():
        return {}
    with PAPER_CONFIG.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def is_enabled(config: dict) -> bool:
    return bool(config.get("enabled", False))


def min_ready_codes(config: dict) -> int:
    guard = config.get("data_guard") or {}
    try:
        return max(int(guard.get("min_ready_codes", DEFAULT_MIN_READY_CODES)), 0)
    except (TypeError, ValueError):
        return DEFAULT_MIN_READY_CODES


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


def _log_run(record: dict, dry_run: bool) -> None:
    """把這次執行寫進稽核紀錄。dry-run 不留痕跡，跟其他寫檔一致。"""
    if dry_run:
        return
    try:
        paper.append_run({"ran_at": datetime.now().isoformat(timespec="seconds"), **record})
    except OSError:
        pass   # 稽核紀錄寫不進去，不該讓整份日報產不出來


def _missing_trading_days(
    universe: dict[str, Series], last_date: str, trade_date: str
) -> list[str]:
    """last_date 與 trade_date 之間、還沒被處理過的交易日。

    交易日直接從歷史資料本身推導（所有標的看得到的日期取聯集），
    不維護假日表——農曆年、颱風假、補班日自動處理，
    理由跟 history.py 推導「那個月應該有幾根 K」是同一個。
    """
    if not last_date or not trade_date or last_date >= trade_date:
        return []
    seen: set[str] = set()
    for series in universe.values():
        for bar in series.bars:
            if last_date < bar.date < trade_date:
                seen.add(bar.date)
    return sorted(seen)


def run_daily(
    trade_date: str,
    quotes: dict[str, Quote],
    dry_run: bool = False,
    catch_up: bool = False,
) -> dict | None:
    """跑一天的模擬倉。回傳給報告用的 dict；未啟用時回傳 None。

    catch_up=True 時，會把 last_date 到 trade_date 之間漏掉的交易日
    依序補跑完再跑今天；預設 False，遇到缺口直接拒絕執行（見下面說明）。
    """
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
        message = "目前沒有登記任何持股或觀察清單標的。"
        _log_run(
            {"trade_date": trade_date, "status": "no_codes", "universe": 0},
            dry_run,
        )
        return {"skipped": message}

    if not dry_run:
        ingest_quotes(codes, quotes)

    universe: dict[str, Series] = {}
    warmup_short: list[str] = []
    for code in codes:
        # 還原權值後的價格：除權息與分割的假斷崖會誤觸發停損，
        # 也會讓均線在事件後 60 天內整段失真。
        bars = history.load_bars_adjusted(code)
        if not bars:
            continue
        universe[code] = Series(code=code, bars=bars)
        if len(bars) < params.warmup_bars:
            warmup_short.append(f"{code}（{len(bars)}/{params.warmup_bars} 根）")

    ready = [
        c for c, s in universe.items() if len(s.bars) >= params.warmup_bars
    ]
    base_log = {
        "trade_date": trade_date,
        "universe": len(codes),
        "with_bars": len(universe),
        "ready": len(ready),
        "short": len(warmup_short),
    }

    if not universe:
        message = (
            "還沒有任何歷史日 K，資料會隨系統每天執行慢慢累積，"
            "累積到足夠天數後這裡就會開始有內容。"
        )
        _log_run({**base_log, "status": "no_history"}, dry_run)
        return {"skipped": message}

    # 同一個交易日重跑不應該重複成交一次。
    if account.last_date == trade_date:
        _log_run({**base_log, "status": "already_ran"}, dry_run)
        return {
            "skipped": f"模擬倉今天（{trade_date}）已經跑過，未重複執行。",
            "equity": account.equity(
                {c: s.bars[-1].close for c, s in universe.items()}
            ),
        }

    # --- 資料充足度保護 ---
    # 資料還沒到齊就跑，得到的「今天沒有訊號」是假的——它只代表沒東西可看。
    # 這種情況下**絕對不能寫 last_date**，否則這一天會被永久鎖住，
    # 之後補了歷史也重跑不了。直接整段不算數，等資料補齊再跑同一天。
    threshold = min_ready_codes(config)
    if len(ready) < threshold:
        message = (
            f"⚠️ **今天不算數**：掃描池 {len(codes)} 檔裡只有 {len(ready)} 檔"
            f"歷史資料足夠（需要 {params.warmup_bars} 根 K，門檻至少 {threshold} 檔），"
            "資料量不足以做出可信的判斷。\n"
            "> \n"
            f"> 這一天**沒有被記錄下來**，補完歷史後重跑 `python3 src/main.py` "
            "就會補上，不會像過去那樣被永久跳過。\n"
            "> \n"
            "> 補資料：`python3 src/history.py --months 24` 與 "
            "`python3 src/history.py --fill-gaps`"
        )
        _log_run(
            {**base_log, "status": "insufficient_data", "threshold": threshold},
            dry_run,
        )
        return {"skipped": message, "insufficient": True, **base_log}

    # --- 排程斷線保護 ---
    # run_day() 只認「你叫它跑哪一天」，不會發現自己漏了幾天。
    # 排程斷線超過一天再恢復時，資料源給的是最新那天，
    # 於是中間的交易日整段憑空消失：不報錯、數字照樣算得出來，
    # 但那幾天的訊號沒被記錄，而且昨天決定的委託會用錯誤的開盤價成交。
    # 實際發生過：2026-08-22~08-30 共 9 個交易日被跳過，
    # 2881 那筆 8/21 決定的委託用 8/31（而非 8/24）的開盤價成交。
    #
    # monitor.staleness_warning() 抓得到這件事，但它在 main.py 裡是
    # 「引擎跑完之後」才檢查的——那時 last_date 已經被推到今天，
    # 警告永遠不會觸發。所以這道保護一定要在成交之前。
    missing = _missing_trading_days(universe, account.last_date, trade_date)
    if missing and not catch_up:
        shown = "、".join(missing[:5]) + ("… 等" if len(missing) > 5 else "")
        message = (
            f"⚠️ **今天不算數**：模擬倉停在 {account.last_date}，"
            f"但現在要跑的是 {trade_date}，中間有 {len(missing)} 個交易日沒跑過"
            f"（{shown}）。\n"
            "> \n"
            "> 直接跑今天會讓那幾天的訊號永久消失，"
            "昨天決定的委託也會用錯誤的開盤價成交，所以**這一天沒有被記錄下來**。\n"
            "> \n"
            "> 補跑：先確認歷史沒有缺口"
            "（`python3 src/history.py --months 2` 與 `--fill-gaps`），"
            "再跑 `python3 src/main.py --catch-up` 逐日推進。"
        )
        _log_run(
            {**base_log, "status": "gap_detected", "missing_days": len(missing),
             "last_date": account.last_date},
            dry_run,
        )
        return {"skipped": message, "gap": True, "missing_days": missing, **base_log}

    # 訊號數只是給稽核紀錄用的，run_day 內部會自己重算一次。
    entry_signals = 0
    for code in ready:
        series = universe[code]
        idx = series.index_of(trade_date)
        if idx is None or code in account.positions:
            continue
        if check_entry(code, series.bars, idx, params).triggered:
            entry_signals += 1

    before_trades = len(account.trades)

    # 補跑時一天一天推進，不是直接跳到今天——每一天都要各自成交、
    # 各自記淨值，中間那些天的委託才會用它們自己的開盤價。
    dates_to_run = (missing if catch_up else []) + [trade_date]
    result = None
    for run_date in dates_to_run:
        day_before = len(account.trades)
        result = paper.run_day(
            account, run_date, universe, params, account_params, costs
        )
        if not dry_run:
            for trade in account.trades[day_before:]:
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

    _log_run(
        {
            **base_log,
            "status": "ok",
            "entry_signals": entry_signals,
            "fills": len([f for f in result.fills if f.get("status") == "FILLED"]),
            "orders": len(result.sell_orders) + len(result.buy_orders),
            "holdings": result.holdings,
            "equity": round(result.equity, 2),
            "cash": round(result.cash, 2),
        },
        dry_run,
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
