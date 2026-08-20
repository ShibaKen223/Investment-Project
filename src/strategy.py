"""波段策略：進場與出場訊號。

這個模組補上系統原本缺的那一半。
portfolio.py 只回答「手上的部位該不該出場」，
strategy.py 回答「什麼時候該進場」，以及波段特有的「抱太久就該走」。

⚠️ 這裡的預設參數是一組**保守的教科書型突破策略**，
    它的用途是把「訊號 → 成交 → 紀錄 → 報告」這條路跑通，
    不是一組已經被驗證會賺錢的參數。
    先看模擬倉的績效，再決定要不要調——順序不要反過來。

設計上最重要的一條規則:
    所有判斷都以「第 i 根 K 收盤時已知的資訊」為輸入,
    成交價則是第 i+1 根的開盤價。
    這是為了讓回測和每日實跑走完全相同的程式碼路徑,
    也避免用到當天收盤後才知道的價格去下當天的單（前視偏誤）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from history import Bar


# --------------------------------------------------------------------------
# 指標
# --------------------------------------------------------------------------
# 一律接受 end 參數（不含）表示「只看到這根為止」，
# 預設 None = 看到最後一根。回傳 None 代表資料不足，呼叫端必須處理。


def _window(bars: list[Bar], period: int, end: int | None) -> list[Bar] | None:
    end = len(bars) if end is None else end
    start = end - period
    if period <= 0 or start < 0 or end > len(bars):
        return None
    return bars[start:end]


def sma(bars: list[Bar], period: int, end: int | None = None) -> float | None:
    """簡單移動平均（收盤價）。"""
    window = _window(bars, period, end)
    if window is None:
        return None
    return sum(b.close for b in window) / period


def highest_high(bars: list[Bar], period: int, end: int | None = None) -> float | None:
    """區間最高價。"""
    window = _window(bars, period, end)
    if window is None:
        return None
    return max(b.high for b in window)


def lowest_low(bars: list[Bar], period: int, end: int | None = None) -> float | None:
    window = _window(bars, period, end)
    if window is None:
        return None
    return min(b.low for b in window)


def avg_volume(bars: list[Bar], period: int, end: int | None = None) -> float | None:
    window = _window(bars, period, end)
    if window is None:
        return None
    return sum(b.volume for b in window) / period


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


@dataclass
class StrategyParams:
    """波段參數。預設值對應「持股 5～15 個交易日」的節奏。"""

    # --- 進場 ---
    trend_ma: int = 60          # 長期趨勢過濾：收盤要在這條均線之上
    momentum_ma: int = 20       # 中期動能：收盤要在這條均線之上
    breakout_lookback: int = 20  # 突破前 N 日的最高價才算訊號
    min_avg_volume: float = 500_000.0   # 近 20 日均量下限（股數）＝ 500 張
    volume_ma: int = 20

    # --- 出場 ---
    stop_loss_pct: float = 8.0      # 自「進場成交價」起算
    take_profit_pct: float = 15.0
    max_hold_bars: int = 15         # 抱滿這麼多根 K 還沒觸發就時間出場
    trailing_stop_pct: float = 0.0  # >0 才啟用，自進場後最高收盤起算

    @property
    def warmup_bars(self) -> int:
        """要有這麼多根 K 才能開始判斷，回測時前面這段必須跳過。"""
        return max(
            self.trend_ma, self.momentum_ma, self.breakout_lookback, self.volume_ma
        ) + 1

    @classmethod
    def from_dict(cls, raw: dict | None) -> "StrategyParams":
        raw = raw or {}
        params = cls()
        for key, value in raw.items():
            if hasattr(params, key) and value is not None:
                field_type = type(getattr(params, key))
                setattr(params, key, field_type(value))
        return params


# --------------------------------------------------------------------------
# 進場
# --------------------------------------------------------------------------


@dataclass
class EntrySignal:
    code: str
    bar_index: int
    decision_date: str      # 做出判斷的那根 K 的日期（收盤後）
    triggered: bool
    reasons: list[str] = field(default_factory=list)   # 成立的條件
    blockers: list[str] = field(default_factory=list)  # 不成立的條件

    @property
    def explanation(self) -> str:
        if self.triggered:
            return "；".join(self.reasons)
        return "；".join(self.blockers) or "條件不足"


def check_entry(
    code: str, bars: list[Bar], index: int, params: StrategyParams
) -> EntrySignal:
    """判斷第 index 根 K 收盤時是否出現進場訊號。

    四個條件全部成立才進場，任何一條不成立都會記在 blockers 裡——
    報告要能回答「今天為什麼沒買」，不能只說沒訊號。
    """
    sig = EntrySignal(
        code=code,
        bar_index=index,
        decision_date=bars[index].date if 0 <= index < len(bars) else "",
        triggered=False,
    )

    if index < 0 or index >= len(bars):
        sig.blockers.append("索引超出資料範圍")
        return sig

    end = index + 1              # 含當根
    prior_end = index            # 不含當根（突破要跟「之前」的高點比）
    close = bars[index].close

    trend = sma(bars, params.trend_ma, end)
    momentum = sma(bars, params.momentum_ma, end)
    prior_high = highest_high(bars, params.breakout_lookback, prior_end)
    volume = avg_volume(bars, params.volume_ma, end)

    if None in (trend, momentum, prior_high, volume):
        sig.blockers.append(
            f"歷史資料不足（需要至少 {params.warmup_bars} 根，目前 {end} 根）"
        )
        return sig

    checks = [
        (
            close > trend,
            f"收盤 {close:.2f} 站上 {params.trend_ma} 日均線 {trend:.2f}",
            f"收盤 {close:.2f} 在 {params.trend_ma} 日均線 {trend:.2f} 之下（長期趨勢不利）",
        ),
        (
            close > momentum,
            f"收盤站上 {params.momentum_ma} 日均線 {momentum:.2f}",
            f"收盤在 {params.momentum_ma} 日均線 {momentum:.2f} 之下（中期動能不足）",
        ),
        (
            close > prior_high,
            f"突破前 {params.breakout_lookback} 日高點 {prior_high:.2f}",
            f"未突破前 {params.breakout_lookback} 日高點 {prior_high:.2f}",
        ),
        (
            volume >= params.min_avg_volume,
            f"近 {params.volume_ma} 日均量 {volume/1000:.0f} 張，流動性足夠",
            f"近 {params.volume_ma} 日均量僅 {volume/1000:.0f} 張，低於門檻 "
            f"{params.min_avg_volume/1000:.0f} 張",
        ),
    ]

    for passed, reason, blocker in checks:
        (sig.reasons if passed else sig.blockers).append(reason if passed else blocker)

    sig.triggered = not sig.blockers
    return sig


# --------------------------------------------------------------------------
# 出場
# --------------------------------------------------------------------------


class ExitReason(str):
    """出場原因。用字串子類別方便直接寫進 JSON。"""


STOP_LOSS = ExitReason("STOP_LOSS")
TAKE_PROFIT = ExitReason("TAKE_PROFIT")
TRAILING_STOP = ExitReason("TRAILING_STOP")
TIME_EXIT = ExitReason("TIME_EXIT")

EXIT_LABELS = {
    STOP_LOSS: "🔴 停損",
    TAKE_PROFIT: "🟢 停利",
    TRAILING_STOP: "🟠 移動停損",
    TIME_EXIT: "⏱ 時間出場",
}


@dataclass
class ExitSignal:
    triggered: bool
    reason: str | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        return EXIT_LABELS.get(self.reason, self.reason or "")


def check_exit(
    entry_price: float,
    bars: list[Bar],
    index: int,
    bars_held: int,
    params: StrategyParams,
    peak_close: float | None = None,
) -> ExitSignal:
    """判斷第 index 根 K 收盤時是否該出場。

    優先序刻意是「停損 → 移動停損 → 停利 → 時間」:
    同一天多個條件都成立時，先認賠的那個優先，
    這樣模擬出來的績效不會因為挑對自己有利的順序而偏樂觀。

    bars_held 是「已經抱了幾根 K」，時間出場用。
    peak_close 是進場後的最高收盤，移動停損用；None 代表尚未追蹤。
    """
    if index < 0 or index >= len(bars):
        return ExitSignal(False)

    close = bars[index].close
    change_pct = (close / entry_price - 1) * 100

    stop_price = entry_price * (1 - params.stop_loss_pct / 100)
    if close <= stop_price:
        return ExitSignal(
            True,
            STOP_LOSS,
            f"收盤 {close:.2f} 跌破停損線 {stop_price:.2f}（{change_pct:+.2f}%）",
        )

    if params.trailing_stop_pct > 0 and peak_close is not None:
        trail_price = peak_close * (1 - params.trailing_stop_pct / 100)
        if close <= trail_price and trail_price > stop_price:
            return ExitSignal(
                True,
                TRAILING_STOP,
                f"收盤 {close:.2f} 自波段高點 {peak_close:.2f} 回落至 "
                f"移動停損線 {trail_price:.2f}",
            )

    target_price = entry_price * (1 + params.take_profit_pct / 100)
    if close >= target_price:
        return ExitSignal(
            True,
            TAKE_PROFIT,
            f"收盤 {close:.2f} 觸及停利線 {target_price:.2f}（{change_pct:+.2f}%）",
        )

    if bars_held >= params.max_hold_bars:
        return ExitSignal(
            True,
            TIME_EXIT,
            f"已持有 {bars_held} 根 K 未觸發停損停利（{change_pct:+.2f}%），"
            f"依波段紀律出場",
        )

    return ExitSignal(False)
