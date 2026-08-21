"""部位損益與停損 / 停利判斷。

這個模組是整個專案的核心：把 config/strategy.yaml 裡的規則
變成每天明確的訊號，而不是靠腦袋記。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from datasource import Quote

ROOT = Path(__file__).resolve().parent.parent
SIGNAL_LOG = ROOT / "data" / "signals.jsonl"


class Signal(str, Enum):
    STOP_LOSS = "STOP_LOSS"          # 已觸發停損
    TAKE_PROFIT = "TAKE_PROFIT"      # 已觸發停利
    NEAR_STOP = "NEAR_STOP"          # 接近停損線
    NEAR_TARGET = "NEAR_TARGET"      # 接近停利線
    HOLD = "HOLD"                    # 續抱
    CORE = "CORE"                    # 核心部位，不套用停損停利
    NO_DATA = "NO_DATA"              # 當日無行情

    @property
    def label(self) -> str:
        return {
            Signal.STOP_LOSS: "🔴 觸發停損",
            Signal.TAKE_PROFIT: "🟢 觸發停利",
            Signal.NEAR_STOP: "🟠 接近停損",
            Signal.NEAR_TARGET: "🔵 接近停利",
            Signal.HOLD: "⚪ 續抱",
            Signal.CORE: "⚓ 核心部位",
            Signal.NO_DATA: "❔ 無行情",
        }[self]

    @property
    def is_actionable(self) -> bool:
        return self in (Signal.STOP_LOSS, Signal.TAKE_PROFIT)


@dataclass
class Position:
    code: str
    shares: int
    cost: float
    entry_date: str
    thesis: str = ""
    invalidate: str = ""
    core: bool = False
    exit_date: str | None = None
    exit_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None

    @property
    def cost_basis(self) -> float:
        return self.cost * self.shares

    def holding_days(self, today: date | None = None) -> int:
        today = today or date.today()
        entered = datetime.strptime(self.entry_date, "%Y-%m-%d").date()
        return (today - entered).days


# strategy.yaml 出廠時 objective 裡帶的那句話。用它認出「還沒改過」。
DEFAULT_OBJECTIVE_MARKER = "這是預設範例"


def objective_is_unset(objective: str | None) -> bool:
    """目標還停在出廠預設值嗎。

    這一行每天印在報告和畫面的最上面，看起來就像已經設定好了。
    分辨得出來才有辦法提醒你去寫自己的——
    一個沒人真心寫過的目標比沒有目標更糟，它會讓你以為自己有紀律。
    """
    text = (objective or "").strip()
    return not text or DEFAULT_OBJECTIVE_MARKER in text


@dataclass
class Rules:
    stop_loss_pct: float = 10.0
    take_profit_pct: float = 22.0
    stop_basis: str = "cost"      # "cost" | "trailing"
    near_threshold_pct: float = 3.0


@dataclass
class Evaluation:
    position: Position
    quote: Quote | None
    rules: Rules
    signal: Signal
    peak_price: float | None = None      # 進場後最高價（trailing 用）
    notes: list[str] = field(default_factory=list)

    # --- 損益 ---
    @property
    def market_value(self) -> float | None:
        return None if self.quote is None else self.quote.close * self.position.shares

    @property
    def pnl(self) -> float | None:
        mv = self.market_value
        return None if mv is None else mv - self.position.cost_basis

    @property
    def pnl_pct(self) -> float | None:
        if self.quote is None:
            return None
        return (self.quote.close / self.position.cost - 1) * 100

    # --- 停損 / 停利價位 ---
    @property
    def stop_reference(self) -> float:
        """停損的計算基準價。"""
        if self.rules.stop_basis == "trailing" and self.peak_price is not None:
            return max(self.peak_price, self.position.cost)
        return self.position.cost

    @property
    def stop_price(self) -> float:
        return self.stop_reference * (1 - self.rules.stop_loss_pct / 100)

    @property
    def target_price(self) -> float:
        return self.position.cost * (1 + self.rules.take_profit_pct / 100)

    @property
    def pct_to_stop(self) -> float | None:
        """現價要再變動幾 % 才會碰到停損線。

        -14.29% = 還要再跌 14.29%（仍有緩衝）
        +5.88%  = 停損線在現價之上，代表已經跌破
        """
        if self.quote is None:
            return None
        return (self.stop_price / self.quote.close - 1) * 100

    @property
    def pct_to_target(self) -> float | None:
        """現價要再變動幾 % 才會碰到停利線。

        +16.19% = 還要再漲 16.19%（尚未到價）
        -18.67% = 停利線在現價之下，代表已經突破
        """
        if self.quote is None:
            return None
        return (self.target_price / self.quote.close - 1) * 100


def load_peaks(positions: list[Position]) -> dict[str, float]:
    """每個部位「進場之後」的最高收盤價，移動停損（stop_basis=trailing）用。

    一定要用 entry_date 切，不能整份掃過去取最大值：
    signals.jsonl 是 append-only 的全歷史，同一個代號裡面可能混著
    這次進場**之前**的價格，甚至上一輪早就出場的那個部位的價格。
    拿那種高點當移動停損的基準，停損線會被莫名其妙地往上拉，
    而畫面上只會看到一條「不知道為什麼這麼高」的停損線。

    ISO 日期字串直接比大小就是時間順序，不用轉 datetime。
    """
    entry_dates = {p.code: p.entry_date for p in positions}
    if not entry_dates or not SIGNAL_LOG.exists():
        return {}

    peaks: dict[str, float] = {}
    with SIGNAL_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            trade_date = str(record.get("trade_date") or "")
            for item in record.get("positions", []):
                code = item.get("code")
                entry_date = entry_dates.get(code)
                if entry_date is None or trade_date < entry_date:
                    continue
                close = item.get("close")
                if isinstance(close, (int, float)):
                    peaks[code] = max(peaks.get(code, 0.0), float(close))
    return peaks


def resolve_rules(base: Rules, overrides: dict[str, dict], code: str) -> Rules:
    """套用個股例外規則。未列出的個股回傳原本的 rules。"""
    override = overrides.get(code)
    if not override:
        return base
    return Rules(
        stop_loss_pct=float(override.get("stop_loss_pct", base.stop_loss_pct)),
        take_profit_pct=float(
            override.get("take_profit_pct", base.take_profit_pct)
        ),
        stop_basis=str(override.get("stop_basis", base.stop_basis)),
        near_threshold_pct=float(
            override.get("near_threshold_pct", base.near_threshold_pct)
        ),
    )


def evaluate(
    position: Position,
    quote: Quote | None,
    rules: Rules,
    peak_price: float | None = None,
    today: date | None = None,
) -> Evaluation:
    """對單一部位算出當日訊號。"""
    ev = Evaluation(
        position=position,
        quote=quote,
        rules=rules,
        signal=Signal.NO_DATA,
        peak_price=peak_price,
    )

    if quote is None:
        ev.notes.append("查無當日行情，請確認代號是否正確或該檔是否停牌。")
        return ev

    if position.core:
        ev.signal = Signal.CORE
        ev.notes.append("核心部位，依設定不套用停損停利訊號。")
        return ev

    near = rules.near_threshold_pct

    if quote.close <= ev.stop_price:
        ev.signal = Signal.STOP_LOSS
    elif quote.close >= ev.target_price:
        ev.signal = Signal.TAKE_PROFIT
    elif ev.pct_to_stop is not None and ev.pct_to_stop >= -near:
        ev.signal = Signal.NEAR_STOP
    elif ev.pct_to_target is not None and ev.pct_to_target <= near:
        ev.signal = Signal.NEAR_TARGET
    else:
        ev.signal = Signal.HOLD

    if rules.stop_basis == "trailing" and peak_price is None:
        ev.notes.append(
            "移動停損尚無歷史高點，暫以成本價計算；訊號記錄累積後會自動修正。"
        )

    if position.holding_days(today) < 0:
        ev.notes.append("進場日在未來，請確認登記的進場日期有沒有打錯。")

    return ev


def summarize(evaluations: list[Evaluation]) -> dict:
    """整體投組彙總。"""
    priced = [e for e in evaluations if e.quote is not None]
    cost_basis = sum(e.position.cost_basis for e in priced)
    market_value = sum(e.market_value or 0.0 for e in priced)
    return {
        "positions": len(evaluations),
        "priced": len(priced),
        "cost_basis": cost_basis,
        "market_value": market_value,
        "pnl": market_value - cost_basis,
        "pnl_pct": ((market_value / cost_basis - 1) * 100) if cost_basis else 0.0,
        "actionable": [e for e in evaluations if e.signal.is_actionable],
    }
