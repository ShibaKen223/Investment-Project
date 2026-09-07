"""模擬倉（paper trading）：把訊號變成有成本、有紀錄的虛擬成交。

這不是回測工具的一部分，它就是**下單引擎本身**，只是成交發生在記憶體裡。
之後要接真實券商，替換的只有 _fill() 這一層，其餘邏輯完全不動。

三個刻意的保守設定，全都是為了讓模擬績效不要比現實好看:

1. 成交價用「隔日開盤價」，不是當日收盤價。
   收盤價是你做決定當下才知道的數字，用它下單等於偷看未來。
2. 交易成本照台股實況計算：買賣各一次手續費 0.1425%（可設折扣、有最低 20 元），
   賣出再課 0.3% 證交稅。來回大約吃掉 0.47%。
3. 交易單位由 account.trade_unit 決定（預設 1000 股＝整張，設 1 開放零股）。

   ⚠️ 零股不是「放寬限制」，是**修掉一個讓回測失真的缺陷**。
   本金 100 萬、每檔 20%＝預算 20 萬，但只能買整張時，觀察清單 54 檔裡
   有 30 檔一張就超過 20 萬（2330 一張 244 萬、6669 一張 780 萬），
   訊號成立也永遠買不到。實際成交的只剩金控、航運、塑化、電信——
   選股論述寫的是 AI 供應鏈，回測測的根本是另一個標的池。

   而且它會隨股價自己惡化：65 萬淨利裡有 41.6 萬（64%）來自
   1303、2408、3711、2317、6213、3037 這幾檔，它們都是**買進時買得起、
   現在已經買不起**的。只留今天買得起的 24 檔重跑，報酬從 65.51%
   掉到 14.74%、報酬/回撤 1.70 輸給基準。贏家漲到買不起了。

   零股讓「20% 部位」這個設定真的能被執行：7580 元的股票買 26 股
   ≈ 19.7 萬，正好是本來就打算投的金額。部位大小沒有變大或變小，
   只是從「0 或 758 萬」變成「拿得到 20 萬」。

檔案:
    data/paper_state.json    當前現金、持股、待執行委託（會覆寫）
    data/paper_trades.jsonl  每一筆成交（append-only，永不改寫；
                             作廢是再 append 一筆註銷紀錄，見 append_void）
    data/paper_equity.jsonl  每日淨值（append-only，畫績效曲線用）
    data/paper_runs.jsonl    每次執行的稽核紀錄（append-only，見 append_run）
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
from pathlib import Path

from history import Bar
from strategy import (
    EntrySignal,
    ExitSignal,
    StrategyParams,
    atr,
    check_entry,
    check_exit,
    exit_levels,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "paper_state.json"
TRADES_FILE = DATA_DIR / "paper_trades.jsonl"
EQUITY_FILE = DATA_DIR / "paper_equity.jsonl"
RUNS_FILE = DATA_DIR / "paper_runs.jsonl"

LOT_SIZE = 1000  # 台股一張 = 1000 股


# --------------------------------------------------------------------------
# 交易成本
# --------------------------------------------------------------------------


@dataclass
class Costs:
    """台股交易成本。預設值是「網路下單 6 折手續費、一般股票」。"""

    fee_rate: float = 0.001425     # 券商手續費費率（法定上限）
    fee_discount: float = 0.6      # 網路下單折扣，多數券商 5～6 折
    fee_minimum: float = 20.0      # 每筆最低手續費
    tax_rate: float = 0.003        # 證交稅，賣出才收（ETF 為 0.001）
    slippage_pct: float = 0.1      # 滑價：實際成交常比開盤價差一點

    def fee(self, amount: float) -> float:
        """單邊手續費，無條件捨去到元，並套用最低收費。"""
        raw = amount * self.fee_rate * self.fee_discount
        return max(float(math.floor(raw)), self.fee_minimum)

    def tax(self, amount: float) -> float:
        return float(math.floor(amount * self.tax_rate))

    def buy_price(self, open_price: float) -> float:
        """買進的實際成交價：往上滑。"""
        return open_price * (1 + self.slippage_pct / 100)

    def sell_price(self, open_price: float) -> float:
        """賣出的實際成交價：往下滑。"""
        return open_price * (1 - self.slippage_pct / 100)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Costs":
        raw = raw or {}
        costs = cls()
        for key, value in raw.items():
            if hasattr(costs, key) and value is not None:
                setattr(costs, key, float(value))
        return costs


@dataclass
class AccountParams:
    """資金與部位管理。"""

    initial_cash: float = 1_000_000.0
    position_pct: float = 20.0   # 每檔投入淨值的百分比
    max_positions: int = 5       # 同時最多幾檔

    # 最小交易單位（股）。1000 = 只能買整張；1 = 開放零股。
    # 預設留 1000 是為了讓既有測試與舊設定檔的行為不變，
    # 真正在跑的設定見 config/paper.yaml。
    trade_unit: int = 1000

    # 低於這個金額就不開新部位（0 = 不檢查）。
    # 零股讓「買 1 股」變成可能，而手續費有 20 元的最低收費：
    # 2000 元的部位來回要付 20+20+6 = 46 元 ＝ 2.3%，
    # 對一套停損 8%、停利 15% 的策略來說，那筆交易從第一天就輸了。
    # 這道門檻擋的是「現金快用完時硬擠出一筆零頭部位」。
    min_position_value: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "AccountParams":
        raw = raw or {}
        params = cls()
        for key, value in raw.items():
            if hasattr(params, key) and value is not None:
                field_type = type(getattr(params, key))
                setattr(params, key, field_type(value))
        return params


# --------------------------------------------------------------------------
# 部位、委託、成交
# --------------------------------------------------------------------------


@dataclass
class PaperPosition:
    code: str
    shares: int
    entry_price: float        # 已含滑價的成交價
    entry_date: str
    entry_fee: float
    entry_reason: str = ""
    peak_close: float = 0.0   # 進場後最高收盤（移動停損用）
    bars_held: int = 0
    entry_atr: float = 0.0    # 進場當下的 ATR，stop_mode="atr" 時決定停損寬度

    @property
    def cost_basis(self) -> float:
        """含手續費的總投入。"""
        return self.entry_price * self.shares + self.entry_fee


@dataclass
class Order:
    """待執行委託。今天收盤決定，明天開盤成交。"""

    code: str
    side: str            # "BUY" | "SELL"
    shares: int
    decided_on: str      # 做出判斷的交易日
    reason: str = ""
    detail: str = ""
    atr: float = 0.0     # 下單當下的 ATR，成交時一併寫進部位


@dataclass
class Trade:
    """一筆完成的來回交易。"""

    code: str
    shares: int
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    entry_reason: str
    exit_reason: str
    exit_detail: str
    fees: float          # 買 + 賣手續費
    tax: float
    bars_held: int

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.tax

    @property
    def net_pnl_pct(self) -> float:
        basis = self.entry_price * self.shares
        return (self.net_pnl / basis * 100) if basis else 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            gross_pnl=round(self.gross_pnl, 2),
            net_pnl=round(self.net_pnl, 2),
            net_pnl_pct=round(self.net_pnl_pct, 4),
        )
        return data


# --------------------------------------------------------------------------
# 帳戶
# --------------------------------------------------------------------------


@dataclass
class Account:
    cash: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    pending: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    last_date: str = ""

    # 上一次寫這本帳的機器。狀態檔在版控裡，兩台機器各自跑排程時
    # 會產生兩本互相矛盾的帳，而 git 只會把它當文字合併或直接覆蓋——
    # 實際發生過：origin/main 的 2881 是「8/21 的訊號、8/28 的成交價」，
    # 本機同一筆卻是 8/24 @ 135.135。兩邊都不報錯，畫面都很正常。
    # 有了這個欄位，paperdaily 才能在成交之前發現「這本帳不是我的」。
    owner: str = ""

    def equity(self, prices: dict[str, float]) -> float:
        """淨值 = 現金 + 持股市值。查不到報價的持股用進場價估。"""
        holdings = sum(
            pos.shares * prices.get(code, pos.entry_price)
            for code, pos in self.positions.items()
        )
        return self.cash + holdings

    def to_dict(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "last_date": self.last_date,
            "owner": self.owner,
            "positions": {c: asdict(p) for c, p in self.positions.items()},
            "pending": [asdict(o) for o in self.pending],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Account":
        """從狀態檔還原。

        ⚠️ `trades` 刻意不還原，狀態檔裡也沒存它——
        已平倉的交易一律以 append-only 的 paper_trades.jsonl 為準（見 load_trades）。
        所以 load_state() 回來的 account.trades 一定是空的，
        它只會裝「這一次執行新平倉」的那幾筆。要算累計績效請用 load_trades()。
        """
        return cls(
            cash=float(raw.get("cash", 0.0)),
            last_date=str(raw.get("last_date", "")),
            owner=str(raw.get("owner", "")),
            positions={
                code: PaperPosition(**data)
                for code, data in (raw.get("positions") or {}).items()
            },
            pending=[Order(**o) for o in (raw.get("pending") or [])],
        )


# --------------------------------------------------------------------------
# 成交
# --------------------------------------------------------------------------


def _shares_affordable(
    cash: float, price: float, target_value: float, trade_unit: int = LOT_SIZE
) -> int:
    """在「不超過目標金額」且「現金付得起」之下，最多能買幾股。

    trade_unit 是最小交易單位：1000 = 只能買整張，1 = 零股。
    無條件捨去到 trade_unit 的整數倍。
    """
    if price <= 0 or trade_unit < 1:
        return 0
    budget = min(cash, target_value)
    units = int(budget // (price * trade_unit))
    return max(units, 0) * trade_unit


def _why_unaffordable(
    price: float, cash: float, target_value: float, trade_unit: int,
    min_position_value: float,
) -> str:
    """買不到時，說出**真正**卡住的是哪一個限制。

    舊版本這裡一律印「資金不足（可用 X 元）」，而 X 印的是現金。
    但 _shares_affordable 取的是 min(現金, 部位上限)，所以最常見的情況
    其實是「現金很多，但一個單位就超過 20% 的部位上限」——
    畫面照樣說「資金不足，可用 858,739 元」，看的人會以為要再等錢進來，
    實際上再多的錢也買不到，該調的是 position_pct 或 trade_unit。
    """
    unit_cost = price * trade_unit
    unit_label = f"{trade_unit:,} 股" if trade_unit != LOT_SIZE else "1 張"
    if unit_cost > target_value and unit_cost > cash:
        return (
            f"最小交易單位（{unit_label}）約 {unit_cost:,.0f} 元，"
            f"同時超過部位上限 {target_value:,.0f} 元與可用現金 {cash:,.0f} 元"
        )
    if unit_cost > target_value:
        return (
            f"最小交易單位（{unit_label}）約 {unit_cost:,.0f} 元，"
            f"超過每檔部位上限 {target_value:,.0f} 元"
            f"（現金有 {cash:,.0f} 元，卡住的不是現金）"
        )
    if unit_cost > cash:
        return (
            f"現金只剩 {cash:,.0f} 元，不足以買進最小交易單位"
            f"（{unit_label}，約 {unit_cost:,.0f} 元）"
        )
    return (
        f"買得到的金額低於最小部位門檻 {min_position_value:,.0f} 元，"
        f"這種零頭部位光手續費就吃掉報酬"
    )


def execute_pending(
    account: Account,
    trade_date: str,
    opens: dict[str, float],
    costs: Costs,
    trade_unit: int = LOT_SIZE,
) -> list[dict]:
    """用今天的開盤價執行昨天決定的委託。回傳成交明細（給報告用）。

    查不到開盤價的委託直接作廢，不順延——順延會讓模擬倉在停牌後
    用一個完全不同的價格成交，那是現實中不會發生的事。
    """
    fills: list[dict] = []

    for order in account.pending:
        open_price = opens.get(order.code)
        if open_price is None or open_price <= 0:
            fills.append(
                {
                    "code": order.code,
                    "side": order.side,
                    "status": "CANCELLED",
                    "detail": "當日無開盤價（停牌或無成交），委託作廢",
                }
            )
            continue

        if order.side == "BUY":
            fills.append(
                _fill_buy(account, order, trade_date, open_price, costs, trade_unit)
            )
        else:
            fills.append(_fill_sell(account, order, trade_date, open_price, costs))

    account.pending = []
    return [f for f in fills if f]


def _fill_buy(
    account: Account, order: Order, trade_date: str, open_price: float, costs: Costs,
    trade_unit: int = LOT_SIZE,
) -> dict:
    if order.code in account.positions:
        return {
            "code": order.code,
            "side": "BUY",
            "status": "SKIPPED",
            "detail": "已持有，不重複進場",
        }

    price = costs.buy_price(open_price)
    shares = min(
        order.shares,
        _shares_affordable(account.cash, price, account.cash, trade_unit),
    )
    if shares < trade_unit:
        unit_label = f"{trade_unit:,} 股" if trade_unit != LOT_SIZE else "1 張"
        return {
            "code": order.code,
            "side": "BUY",
            "status": "REJECTED",
            "detail": (
                f"現金 {account.cash:,.0f} 不足以買進最小交易單位"
                f"（{unit_label}，每股 {price:.2f}）"
            ),
        }

    amount = price * shares
    fee = costs.fee(amount)
    account.cash -= amount + fee
    account.positions[order.code] = PaperPosition(
        code=order.code,
        shares=shares,
        entry_price=price,
        entry_date=trade_date,
        entry_fee=fee,
        entry_reason=order.detail,
        peak_close=price,
        bars_held=0,
        entry_atr=order.atr,
    )
    return {
        "code": order.code,
        "side": "BUY",
        "status": "FILLED",
        "shares": shares,
        "price": round(price, 2),
        "amount": round(amount, 2),
        "fee": fee,
        "detail": order.detail,
    }


def _fill_sell(
    account: Account, order: Order, trade_date: str, open_price: float, costs: Costs
) -> dict:
    pos = account.positions.get(order.code)
    if pos is None:
        return {
            "code": order.code,
            "side": "SELL",
            "status": "SKIPPED",
            "detail": "已無此部位",
        }

    price = costs.sell_price(open_price)
    amount = price * pos.shares
    fee = costs.fee(amount)
    tax = costs.tax(amount)
    account.cash += amount - fee - tax

    trade = Trade(
        code=pos.code,
        shares=pos.shares,
        entry_date=pos.entry_date,
        entry_price=round(pos.entry_price, 2),
        exit_date=trade_date,
        exit_price=round(price, 2),
        entry_reason=pos.entry_reason,
        exit_reason=order.reason,
        exit_detail=order.detail,
        fees=round(pos.entry_fee + fee, 2),
        tax=tax,
        bars_held=pos.bars_held,
    )
    account.trades.append(trade)
    del account.positions[order.code]

    return {
        "code": order.code,
        "side": "SELL",
        "status": "FILLED",
        "shares": trade.shares,
        "price": round(price, 2),
        "amount": round(amount, 2),
        "fee": fee,
        "tax": tax,
        "net_pnl": round(trade.net_pnl, 2),
        "net_pnl_pct": round(trade.net_pnl_pct, 2),
        "detail": order.detail,
    }


# --------------------------------------------------------------------------
# 每日流程
# --------------------------------------------------------------------------
# 回測與每天實跑呼叫的是同一個 run_day()，差別只在餵進去的是歷史還是今天。
# 這一點是刻意的：兩條路徑一旦分家，回測就不再能證明任何事情。


@dataclass
class Series:
    """單一標的的日 K 序列，附日期索引。"""

    code: str
    bars: list[Bar]
    _index: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._index = {b.date: i for i, b in enumerate(self.bars)}

    def index_of(self, trade_date: str) -> int | None:
        return self._index.get(trade_date)

    def bar_on(self, trade_date: str) -> Bar | None:
        idx = self.index_of(trade_date)
        return None if idx is None else self.bars[idx]


@dataclass
class DayResult:
    """一天的完整決策紀錄。報告直接讀這個。"""

    trade_date: str
    fills: list[dict] = field(default_factory=list)
    sell_orders: list[Order] = field(default_factory=list)
    buy_orders: list[Order] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    equity: float = 0.0
    cash: float = 0.0
    holdings: int = 0

    def to_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "holdings": self.holdings,
            "fills": self.fills,
            "orders_for_next_open": [
                {**asdict(o)} for o in (self.sell_orders + self.buy_orders)
            ],
            "rejected": self.rejected,
        }


def _rank_key(series: Series, index: int, params: StrategyParams) -> float:
    """候選太多時的排序依據：離 20 日均線越遠代表動能越強。

    需要一個**決定性**的排序，否則同一份資料跑兩次會得到不同結果，
    模擬倉就失去了「可重現」這個唯一的優點。
    """
    from strategy import sma

    ma = sma(series.bars, params.momentum_ma, index + 1)
    if not ma:
        return 0.0
    return series.bars[index].close / ma - 1


def run_day(
    account: Account,
    trade_date: str,
    universe: dict[str, Series],
    params: StrategyParams,
    account_params: AccountParams,
    costs: Costs,
) -> DayResult:
    """跑完一個交易日：先成交昨天的委託，再產生明天的委託。"""
    result = DayResult(trade_date=trade_date)

    # --- 1. 用今天開盤價成交昨天的委託 ---
    opens = {
        code: bar.open
        for code, series in universe.items()
        if (bar := series.bar_on(trade_date)) is not None
    }
    result.fills = execute_pending(
        account, trade_date, opens, costs, account_params.trade_unit
    )

    # --- 2. 更新持股狀態（今天有交易的才算一根） ---
    closes: dict[str, float] = {}
    for code, pos in account.positions.items():
        series = universe.get(code)
        bar = series.bar_on(trade_date) if series else None
        if bar is None:
            continue
        closes[code] = bar.close
        if pos.entry_date != trade_date:
            pos.bars_held += 1
        pos.peak_close = max(pos.peak_close, bar.close)

    result.equity = account.equity(closes)
    result.cash = account.cash
    result.holdings = len(account.positions)

    # --- 3. 出場判斷（今天收盤決定，明天開盤賣） ---
    for code, pos in account.positions.items():
        series = universe.get(code)
        idx = series.index_of(trade_date) if series else None
        if idx is None:
            continue
        exit_sig: ExitSignal = check_exit(
            entry_price=pos.entry_price,
            bars=series.bars,
            index=idx,
            bars_held=pos.bars_held,
            params=params,
            peak_close=pos.peak_close,
            entry_atr=pos.entry_atr,
        )
        if exit_sig.triggered:
            result.sell_orders.append(
                Order(
                    code=code,
                    side="SELL",
                    shares=pos.shares,
                    decided_on=trade_date,
                    reason=exit_sig.reason or "",
                    detail=f"{exit_sig.label}：{exit_sig.detail}",
                )
            )

    # --- 4. 進場判斷 ---
    # 今天決定要賣的部位，它的名額明天才會釋出，所以這裡不算進可用名額。
    occupied = len(account.positions) - len(result.sell_orders)
    slots = max(account_params.max_positions - occupied, 0)

    candidates: list[tuple[float, str, EntrySignal]] = []
    for code, series in universe.items():
        if code in account.positions:
            continue
        idx = series.index_of(trade_date)
        if idx is None:
            continue
        sig = check_entry(code, series.bars, idx, params)
        if sig.triggered:
            candidates.append((_rank_key(series, idx, params), code, sig))
        elif sig.blockers:
            result.rejected.append(
                {"code": code, "reason": sig.explanation}
            )

    # 動能最強的排前面；同分時用代號排序，確保結果可重現。
    candidates.sort(key=lambda item: (-item[0], item[1]))

    # 訊號成立卻因名額不足而落選的，也要留下紀錄。
    # 「今天有 8 檔符合條件，我只買得下 5 檔」是你調整 max_positions 的依據，
    # 靜靜丟掉的話報告就永遠不會告訴你這件事。
    for _, code, _sig in candidates[slots:]:
        result.rejected.append(
            {
                "code": code,
                "reason": f"訊號成立，但同時持有上限為 "
                f"{account_params.max_positions} 檔，本次未入選",
            }
        )

    target_value = result.equity * account_params.position_pct / 100
    projected_cash = account.cash
    for _, code, sig in candidates[:slots]:
        series = universe[code]
        bar = series.bar_on(trade_date)
        if bar is None:
            continue
        est_price = costs.buy_price(bar.close)   # 用收盤價估算股數，實際以明開成交
        unit = account_params.trade_unit
        shares = _shares_affordable(projected_cash, est_price, target_value, unit)

        # 零頭部位擋在這裡，不是擋在成交那一層——現在拒絕還能把名額
        # 留給下一檔，等到明天開盤才發現就只是白白空一天。
        if shares >= unit and est_price * shares < account_params.min_position_value:
            shares = 0

        if shares < unit:
            result.rejected.append(
                {
                    "code": code,
                    "reason": "訊號成立但買不到："
                    + _why_unaffordable(
                        est_price, projected_cash, target_value, unit,
                        account_params.min_position_value,
                    ),
                }
            )
            continue
        projected_cash -= est_price * shares

        # 停損寬度在下單當下就固定下來。用「今天」的 ATR 而不是之後每天重算，
        # 是因為會移動的停損線沒辦法在進場前算出這筆交易最多會賠多少。
        entry_atr = (
            atr(series.bars, params.atr_period, series.index_of(trade_date) + 1)
            if params.uses_atr
            else None
        ) or 0.0

        detail = sig.explanation
        if params.uses_atr:
            _, _, basis = exit_levels(est_price, params, entry_atr)
            detail += f"｜停損基準 {basis}"

        result.buy_orders.append(
            Order(
                code=code,
                side="BUY",
                shares=shares,
                decided_on=trade_date,
                reason="ENTRY",
                detail=detail,
                atr=entry_atr,
            )
        )

    account.pending = result.sell_orders + result.buy_orders
    account.last_date = trade_date
    return result


# --------------------------------------------------------------------------
# 存檔
# --------------------------------------------------------------------------


class StateCorrupted(RuntimeError):
    """狀態檔讀不出來。

    刻意讓它炸出來，而不是安靜地開一個新帳戶——
    後者會把持股和現金重設成初始資金，但 paper_trades.jsonl 和
    paper_equity.jsonl 還留著舊紀錄，於是之後每一個績效數字都是錯的，
    而畫面上完全看不出來。壞掉就該停下來講清楚。
    """


def state_backup_path() -> Path:
    return STATE_FILE.with_name(STATE_FILE.name + ".bak")


def load_state(initial_cash: float) -> Account:
    """讀出模擬倉狀態。檔案不存在＝第一次跑，用初始資金開一個新帳戶。"""
    if not STATE_FILE.exists():
        return Account(cash=initial_cash)
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return Account.from_dict(raw)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        hint = (
            "上一次的狀態備份還在，可以請 Claude 幫你還原。"
            if state_backup_path().exists()
            else "沒有可用的備份。"
        )
        raise StateCorrupted(
            f"模擬倉的狀態檔讀不出來（{exc}）。為了避免算出錯的績效，"
            f"這次不執行模擬倉。{hint}"
        ) from exc


def save_state(account: Account) -> None:
    """原子性寫入，並保留上一版。

    跟 store.save_doc() 同樣的規矩：直接 write_text 的話，
    寫到一半被中斷就會留下半個 JSON——而那正是 load_state 唯一
    救不回來的情況。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(account.to_dict(), ensure_ascii=False, indent=2)

    if STATE_FILE.exists():
        try:
            state_backup_path().write_text(
                STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:
            pass   # 備份失敗不該擋住這次存檔

    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(STATE_FILE)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_trade(trade: Trade) -> None:
    _append_jsonl(TRADES_FILE, trade.to_dict())


# 作廢一筆已平倉交易，用「再 append 一筆註銷紀錄」而不是改寫原本那一行。
#
# 為什麼不直接改掉：這份檔案的價值就在「寫下去就不會再變」——
# 一本可以改寫的帳沒辦法證明自己沒被改過，而模擬倉的全部意義是當證據。
#
# 但作廢的需求是真的：設定被修掉一個**正確性錯誤**之後（例如 38743e2
# 開放零股之前，只能買整張讓 57 檔的掃描池實際只剩 26 檔、金融是唯一
# 全數可買的產業），舊設定產生的交易不該再算進勝率與獲利因子——
# 那是拿壞掉的儀器量出來的數字。
#
# 兩者的交集就是註銷紀錄：原始那一行原封不動留著，旁邊多一行說
# 「這筆不計入，理由是 X，時間是 Y」。稽核時兩行都在，看得出發生過什麼；
# 算績效時 load_trades() 預設把它濾掉。
#
# ⚠️ 這是給「設定錯誤」用的，不是給「這筆賠錢我不想算」用的。
#    判準寫在 config/paper.yaml 的 changelog 裡，理由欄位是必填的。
VOID_RECORD_TYPE = "void"


def append_void(
    code: str,
    entry_date: str,
    exit_date: str,
    reason: str,
    voided_at: str = "",
) -> None:
    """作廢一筆已平倉交易（見 VOID_RECORD_TYPE 上方的說明）。

    用 (code, entry_date, exit_date) 認一筆交易——同一檔在同一段趨勢裡
    可能反覆進出，只用代號會一次註銷掉全部。
    """
    if not str(reason).strip():
        raise ValueError("作廢一定要寫理由——沒有理由的作廢跟竄改沒有分別。")
    _append_jsonl(
        TRADES_FILE,
        {
            "record_type": VOID_RECORD_TYPE,
            "code": str(code),
            "entry_date": str(entry_date),
            "exit_date": str(exit_date),
            "reason": str(reason).strip(),
            "voided_at": voided_at or datetime.now().isoformat(timespec="seconds"),
        },
    )


def _read_jsonl(path: Path) -> list[dict]:
    """讀 append-only 紀錄檔。壞掉的行跳過，不要讓一行爛資料擋住整份歷史。"""
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_trades(include_void: bool = False) -> list[Trade]:
    """讀回已完成的來回交易，最舊的在前面。

    這是累計績效的唯一來源。狀態檔只記「現在還持有什麼」，
    平倉紀錄全在這裡——就算狀態檔壞掉重建，這份也還在。

    to_dict() 會多寫 gross_pnl / net_pnl / net_pnl_pct 三個衍生欄位（給人看的），
    它們不是 Trade 的建構參數，所以這裡要濾掉再還原。

    **被註銷的交易預設不會回傳**（見 append_void）。預設就排除是刻意的：
    這個專案已經吃過一次「規則只寫在散文裡，該用到的那天沒人記得」的虧
    （認錯條件原本只是 YAML 註解，沒有任何程式在檢查）。
    要是預設含進來、由每個呼叫端自己記得過濾，那就是同一個坑——
    而且漏掉時不會報錯，只會安靜地把作廢的交易算進勝率。
    稽核、要看完整原始紀錄時才傳 include_void=True。
    """
    names = {f.name for f in dataclass_fields(Trade)}
    trades: list[Trade] = []
    voided: set[tuple[str, str, str]] = set()

    for record in _read_jsonl(TRADES_FILE):
        # 舊紀錄沒有 record_type 這個 key，get() 回 None，會走成交那一支。
        if record.get("record_type") == VOID_RECORD_TYPE:
            voided.add((
                str(record.get("code", "")),
                str(record.get("entry_date", "")),
                str(record.get("exit_date", "")),
            ))
            continue
        try:
            trades.append(Trade(**{k: v for k, v in record.items() if k in names}))
        except TypeError:
            continue   # 舊格式缺欄位，跳過而不是整份炸掉

    if include_void or not voided:
        return trades
    return [
        t for t in trades
        if (t.code, t.entry_date, t.exit_date) not in voided
    ]


def load_equity_curve() -> list[dict]:
    """每日淨值紀錄，最舊的在前面。"""
    return _read_jsonl(EQUITY_FILE)


def load_equity_values() -> list[float]:
    """只要淨值數字，算報酬與最大回撤用。"""
    values: list[float] = []
    for record in load_equity_curve():
        try:
            values.append(float(record["equity"]))
        except (KeyError, TypeError, ValueError):
            continue
    return values


def append_run(record: dict) -> None:
    """記下「這次執行到底看到什麼」。append-only。

    沒有這份紀錄，模擬倉安靜地空轉是查不出來的：畫面上「淨值 100 萬、
    無委託」跟「掃描池整個是空的、根本沒東西可判斷」長得一模一樣。
    淨值曲線只記結果，這裡記的是過程——掃了幾檔、幾檔資料夠、
    幾檔出訊號、最後下了幾筆單。

    出問題時第一個該看的就是這個檔案。
    """
    _append_jsonl(RUNS_FILE, record)


def load_runs() -> list[dict]:
    """讀回所有執行紀錄，最舊的在前面。"""
    return _read_jsonl(RUNS_FILE)


def append_equity(result: DayResult) -> None:
    _append_jsonl(
        EQUITY_FILE,
        {
            "trade_date": result.trade_date,
            "equity": round(result.equity, 2),
            "cash": round(result.cash, 2),
            "holdings": result.holdings,
        },
    )


# --------------------------------------------------------------------------
# 績效
# --------------------------------------------------------------------------


def max_drawdown(equity_curve: list[float]) -> float:
    """最大回撤（%）。這是 strategy.yaml 裡 objective 真正在管的那個數字。"""
    peak = float("-inf")
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1) * 100)
    return worst


def performance(
    trades: list[Trade], equity_curve: list[float], initial_cash: float
) -> dict:
    """模擬倉績效彙總。

    刻意同時報「勝率」和「獲利因子」：
    勝率高但獲利因子低，代表你在賺小錢賠大錢——只看勝率會看不出來。
    """
    closed = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    final_equity = equity_curve[-1] if equity_curve else initial_cash

    return {
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / closed * 100, 2) if closed else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "total_fees": round(sum(t.fees + t.tax for t in trades), 2),
        "net_pnl": round(sum(t.net_pnl for t in trades), 2),
        "avg_bars_held": (
            round(sum(t.bars_held for t in trades) / closed, 1) if closed else 0.0
        ),
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": (
            round((final_equity / initial_cash - 1) * 100, 2) if initial_cash else 0.0
        ),
        "max_drawdown_pct": round(max_drawdown(equity_curve), 2),
    }
