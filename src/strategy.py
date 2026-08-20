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


def true_range(bars: list[Bar], index: int) -> float | None:
    """單根 K 的真實區間。

    取三者最大：今天的高低差、今天最高與昨收的差距、今天最低與昨收的差距。
    後兩項是為了把「跳空」算進去——只看高低差的話，
    一根開盤就跳空跌停的 K 會被當成波動很小，那顯然不對。
    """
    if index < 0 or index >= len(bars):
        return None
    bar = bars[index]
    if index == 0:
        return bar.high - bar.low   # 沒有昨收可比
    prev_close = bars[index - 1].close
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: list[Bar], period: int = 14, end: int | None = None) -> float | None:
    """平均真實區間（Wilder 平滑）。

    白話：這檔股票「一天通常會動多少錢」。單位是元，不是百分比。

    為什麼需要它：固定 8% 停損對每檔股票都一樣，但一檔日常波動 1% 的
    電信股和一檔波動 5% 的航運股，8% 的意義完全不同——前者要跌很久才會碰到，
    後者兩天的正常震盪就掃出場了。用 ATR 的倍數當停損，
    停損寬度會自動跟著各股的性格調整。

    採 Wilder 原始的平滑法（不是簡單平均），這是 ATR 的標準定義：
    先用前 period 根的平均當起始值，之後每根做遞迴平滑。
    """
    end = len(bars) if end is None else end
    if period <= 0 or end > len(bars) or end < period + 1:
        return None

    trs = [tr for i in range(1, end) if (tr := true_range(bars, i)) is not None]
    if len(trs) < period:
        return None

    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


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
    # stop_mode 決定停損停利怎麼算:
    #   "pct" = 固定百分比，每檔一視同仁
    #   "atr" = N 倍 ATR，停損寬度自動跟著各股的波動度調整
    stop_mode: str = "pct"
    stop_loss_pct: float = 8.0      # 自「進場成交價」起算（stop_mode=pct 時）
    take_profit_pct: float = 15.0
    atr_period: int = 14
    atr_stop_multiple: float = 2.0    # 停損 = 進場價 − N × 進場當下的 ATR
    atr_target_multiple: float = 3.5  # 停利 = 進場價 + N × 進場當下的 ATR
    max_hold_bars: int = 15         # 抱滿這麼多根 K 還沒觸發就時間出場
    trailing_stop_pct: float = 0.0  # >0 才啟用，自進場後最高收盤起算

    @property
    def uses_atr(self) -> bool:
        return str(self.stop_mode).lower() == "atr"

    @property
    def warmup_bars(self) -> int:
        """要有這麼多根 K 才能開始判斷，回測時前面這段必須跳過。"""
        needed = [
            self.trend_ma, self.momentum_ma, self.breakout_lookback, self.volume_ma
        ]
        if self.uses_atr:
            needed.append(self.atr_period + 1)   # ATR 第一根要有昨收可比
        return max(needed) + 1

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


def exit_levels(
    entry_price: float,
    params: StrategyParams,
    entry_atr: float | None = None,
) -> tuple[float, float, str]:
    """算出停損價、停利價，以及一句話說明它們是怎麼來的。

    stop_mode="atr" 但拿不到進場當下的 ATR 時（歷史不足），
    會自動退回固定百分比並在說明裡講清楚——
    安靜地換一套規則是最糟的做法，你之後覆盤會完全看不出來。
    """
    if params.uses_atr and entry_atr and entry_atr > 0:
        stop = entry_price - params.atr_stop_multiple * entry_atr
        target = entry_price + params.atr_target_multiple * entry_atr
        basis = (
            f"{params.atr_stop_multiple:g}×ATR({params.atr_period})"
            f"＝{params.atr_stop_multiple * entry_atr:.2f} 元"
        )
        return max(stop, 0.0), target, basis

    stop = entry_price * (1 - params.stop_loss_pct / 100)
    target = entry_price * (1 + params.take_profit_pct / 100)
    basis = f"固定 {params.stop_loss_pct:g}%"
    if params.uses_atr:
        basis += "（設定為 ATR 模式，但進場時歷史不足，退回百分比）"
    return stop, target, basis


def check_exit(
    entry_price: float,
    bars: list[Bar],
    index: int,
    bars_held: int,
    params: StrategyParams,
    peak_close: float | None = None,
    entry_atr: float | None = None,
) -> ExitSignal:
    """判斷第 index 根 K 收盤時是否該出場。

    優先序刻意是「停損 → 移動停損 → 停利 → 時間」:
    同一天多個條件都成立時，先認賠的那個優先，
    這樣模擬出來的績效不會因為挑對自己有利的順序而偏樂觀。

    bars_held 是「已經抱了幾根 K」，時間出場用。
    peak_close 是進場後的最高收盤，移動停損用；None 代表尚未追蹤。
    entry_atr 是進場當下的 ATR，stop_mode="atr" 時用它算停損停利。
    刻意用「進場當下」而不是「今天」的 ATR——
    停損線在進場後就該固定下來，會移動的停損線沒辦法事先算風險。
    """
    if index < 0 or index >= len(bars):
        return ExitSignal(False)

    close = bars[index].close
    change_pct = (close / entry_price - 1) * 100
    stop_price, target_price, basis = exit_levels(entry_price, params, entry_atr)

    if close <= stop_price:
        return ExitSignal(
            True,
            STOP_LOSS,
            f"收盤 {close:.2f} 跌破停損線 {stop_price:.2f}"
            f"（{change_pct:+.2f}%，停損基準 {basis}）",
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
