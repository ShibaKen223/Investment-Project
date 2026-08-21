"""模擬倉（paper trading）：把訊號變成有成本、有紀錄的虛擬成交。

這不是回測工具的一部分，它就是**下單引擎本身**，只是成交發生在記憶體裡。
之後要接真實券商，替換的只有 _fill() 這一層，其餘邏輯完全不動。

三個刻意的保守設定，全都是為了讓模擬績效不要比現實好看:

1. 成交價用「隔日開盤價」，不是當日收盤價。
   收盤價是你做決定當下才知道的數字，用它下單等於偷看未來。
2. 交易成本照台股實況計算：買賣各一次手續費 0.1425%（可設折扣、有最低 20 元），
   賣出再課 0.3% 證交稅。來回大約吃掉 0.47%。
3. 只能買整張（1000 股）。零股的流動性和成本結構不一樣，不混在一起模擬。

檔案:
    data/paper_state.json    當前現金、持股、待執行委託（會覆寫）
    data/paper_trades.jsonl  每一筆成交（append-only，永不改寫）
    data/paper_equity.jsonl  每日淨值（append-only，畫績效曲線用）
"""

from __future__ import annotations

import json
import math
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
            positions={
                code: PaperPosition(**data)
                for code, data in (raw.get("positions") or {}).items()
            },
            pending=[Order(**o) for o in (raw.get("pending") or [])],
        )


# --------------------------------------------------------------------------
# 成交
# --------------------------------------------------------------------------


def _lots_affordable(cash: float, price: float, target_value: float) -> int:
    """在「不超過目標金額」且「現金付得起」之下，最多能買幾股（整張）。"""
    if price <= 0:
        return 0
    budget = min(cash, target_value)
    lots = int(budget // (price * LOT_SIZE))
    return max(lots, 0) * LOT_SIZE


def execute_pending(
    account: Account,
    trade_date: str,
    opens: dict[str, float],
    costs: Costs,
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
            fills.append(_fill_buy(account, order, trade_date, open_price, costs))
        else:
            fills.append(_fill_sell(account, order, trade_date, open_price, costs))

    account.pending = []
    return [f for f in fills if f]


def _fill_buy(
    account: Account, order: Order, trade_date: str, open_price: float, costs: Costs
) -> dict:
    if order.code in account.positions:
        return {
            "code": order.code,
            "side": "BUY",
            "status": "SKIPPED",
            "detail": "已持有，不重複進場",
        }

    price = costs.buy_price(open_price)
    shares = min(order.shares, _lots_affordable(account.cash, price, account.cash))
    if shares < LOT_SIZE:
        return {
            "code": order.code,
            "side": "BUY",
            "status": "REJECTED",
            "detail": f"現金 {account.cash:,.0f} 不足以買進 1 張（每股 {price:.2f}）",
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
    result.fills = execute_pending(account, trade_date, opens, costs)

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
        est_price = costs.buy_price(bar.close)   # 用收盤價估算張數，實際以明開成交
        shares = _lots_affordable(projected_cash, est_price, target_value)
        if shares < LOT_SIZE:
            result.rejected.append(
                {
                    "code": code,
                    "reason": f"訊號成立但資金不足（每張約 "
                    f"{est_price * LOT_SIZE:,.0f} 元，可用 {projected_cash:,.0f} 元）",
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


def load_trades() -> list[Trade]:
    """讀回**所有**已完成的來回交易，最舊的在前面。

    這是累計績效的唯一來源。狀態檔只記「現在還持有什麼」，
    平倉紀錄全在這裡——就算狀態檔壞掉重建，這份也還在。

    to_dict() 會多寫 gross_pnl / net_pnl / net_pnl_pct 三個衍生欄位（給人看的），
    它們不是 Trade 的建構參數，所以這裡要濾掉再還原。
    """
    names = {f.name for f in dataclass_fields(Trade)}
    trades: list[Trade] = []
    for record in _read_jsonl(TRADES_FILE):
        try:
            trades.append(Trade(**{k: v for k, v in record.items() if k in names}))
        except TypeError:
            continue   # 舊格式缺欄位，跳過而不是整份炸掉
    return trades


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
