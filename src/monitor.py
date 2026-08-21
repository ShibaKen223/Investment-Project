"""監控層的部位來源。

「監控層」指的是每天算損益、算停損停利距離、產生訊號的那一層
（portfolio.py 的 evaluate），它的輸出會變成日報、「今日」頁與 signals.jsonl。

它可以監控兩種部位，由 config/positions.yaml 的 `source` 決定:

    manual  你自己填在 positions.yaml 裡的持股（人工下單、人工登記）
    engine  程式交易引擎的部位帳本（data/paper_state.json）
    both    兩邊都監控，各自標示來源

換成 engine 之後，「今日」頁與日報上看到的就是引擎實際持有的部位，
不再需要每次成交都回頭手動編輯 YAML——手動登記漏一筆，
畫面上不會有任何地方看得出來，那是這個改動要解決的問題。

⚠️ 一個關鍵點：引擎部位的停損停利價**一律由引擎的規則算**
（strategy.exit_levels，ATR 模式時是「進場價 ± N×進場當下的 ATR」），
不套用 strategy.yaml 的 rules 百分比。兩套參數本來就是獨立的，
若讓監控層自己重算，畫面上的停損線會跟引擎明天真的會賣的價格不一樣，
而那比沒有停損線更危險，因為它看起來是對的。
rules 在引擎部位上只剩 near_threshold_pct 還有作用（多近才算「接近」）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import paper  # noqa: E402
import paperdaily  # noqa: E402
from datasource import Quote  # noqa: E402
from portfolio import (  # noqa: E402
    Evaluation,
    Position,
    Rules,
    evaluate,
    load_peaks,
    resolve_rules,
)
from strategy import StrategyParams, exit_levels  # noqa: E402

SOURCE_MANUAL = "manual"
SOURCE_ENGINE = "engine"
SOURCE_BOTH = "both"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_ENGINE, SOURCE_BOTH)

SOURCE_LABEL = {
    SOURCE_MANUAL: "手動登記的持股",
    SOURCE_ENGINE: "程式交易引擎的部位",
    SOURCE_BOTH: "手動持股 ＋ 程式交易引擎部位",
}


# --------------------------------------------------------------------------
# 引擎側的附加資訊
# --------------------------------------------------------------------------


@dataclass
class EngineInfo:
    """引擎部位在監控層裡需要、但 Position 裝不下的東西。"""

    available: bool = False              # 狀態檔讀得出來嗎
    enabled: bool = False                # config/paper.yaml 的 enabled
    levels: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    peaks: dict[str, float] = field(default_factory=dict)
    bars_held: dict[str, int] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)   # 明日開盤委託
    max_hold_bars: int = 0
    last_date: str = ""                  # 引擎最後跑到哪一個交易日
    cash: float = 0.0
    error: str = ""                      # 讀不出來時的原因

    def pending_for(self, code: str) -> list[dict]:
        return [o for o in self.pending if o["code"] == code]


@dataclass
class MonitorSet:
    """這一輪監控要看的部位，以及它們從哪來。"""

    positions: list[Position] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    watchlist: list[dict] = field(default_factory=list)
    source: str = SOURCE_MANUAL
    engine: EngineInfo = field(default_factory=EngineInfo)
    warnings: list[str] = field(default_factory=list)

    @property
    def read_only(self) -> bool:
        """畫面上的「持股異動」還能不能用。

        source=engine 時不能：手動改 positions.yaml 不會影響任何顯示，
        改了以為改了、其實沒有，是最糟的一種介面。
        """
        return self.source == SOURCE_ENGINE

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)


# --------------------------------------------------------------------------
# 來源解析
# --------------------------------------------------------------------------


def resolve_source(doc: dict) -> tuple[str, list[str]]:
    """讀出 positions.yaml 的 source。看不懂的值退回 manual 並講清楚。"""
    raw = str(doc.get("source") or SOURCE_MANUAL).strip().lower()
    if raw in VALID_SOURCES:
        return raw, []
    options = "／".join(VALID_SOURCES)
    return SOURCE_MANUAL, [
        f"positions.yaml 的 source 寫著「{raw}」，不是 {options} "
        "其中之一，這次先當成 manual 處理。"
    ]


def manual_positions(doc: dict) -> tuple[list[Position], list[Position]]:
    """positions.yaml 的持股，拆成「未出場」與「已出場」兩份。"""
    open_rows: list[Position] = []
    closed_rows: list[Position] = []
    for entry in doc.get("positions") or []:
        position = Position(
            code=str(entry["code"]).strip(),
            shares=int(entry["shares"]),
            cost=float(entry["cost"]),
            entry_date=str(entry["entry_date"]),
            thesis=str(entry.get("thesis") or ""),
            invalidate=str(entry.get("invalidate") or ""),
            core=bool(entry.get("core", False)),
            exit_date=entry.get("exit_date"),
            exit_price=entry.get("exit_price"),
            source=SOURCE_MANUAL,
        )
        (open_rows if position.is_open else closed_rows).append(position)
    return open_rows, closed_rows


def _invalidate_text(max_hold_bars: int) -> str:
    """引擎部位的「什麼情況我會認錯」——它是規則寫死的，直接講出來。

    這裡刻意不重複停損基準（幾倍 ATR、幾 %）:
    那句話有自己的欄位（Evaluation.basis_label），兩邊都印會讓報告
    同一件事講兩次，讀的人反而會開始懷疑是不是有兩套規則。
    """
    text = "觸及停損或停利價，由引擎在隔日開盤自動出場"
    if max_hold_bars > 0:
        text += f"；抱滿 {max_hold_bars} 根 K 線未觸發則時間出場"
    return text + "。"


def engine_positions(
    account: paper.Account | None = None,
) -> tuple[list[Position], EngineInfo]:
    """把程式交易引擎的部位帳本轉成監控層看得懂的 Position。

    account 給定時直接用它（main.py 當天剛跑完引擎，用那份最新的狀態，
    不要再從檔案讀一次舊的）；省略時自己讀 data/paper_state.json。
    """
    config = paperdaily.load_config()
    params = StrategyParams.from_dict(config.get("strategy"))
    info = EngineInfo(
        enabled=paperdaily.is_enabled(config),
        max_hold_bars=int(params.max_hold_bars),
    )

    if account is None:
        initial_cash = float(
            (config.get("account") or {}).get("initial_cash", 0.0) or 0.0
        )
        try:
            account = paper.load_state(initial_cash)
        except paper.StateCorrupted as exc:
            info.error = str(exc)
            return [], info

    info.available = True
    info.last_date = account.last_date
    info.cash = account.cash
    info.pending = [
        {
            "code": o.code,
            "side": o.side,
            "shares": o.shares,
            "decided_on": o.decided_on,
            "reason": o.reason,
            "detail": o.detail,
        }
        for o in account.pending
    ]

    positions: list[Position] = []
    for code, pos in account.positions.items():
        if pos.shares <= 0:
            continue
        stop, target, basis = exit_levels(
            pos.entry_price, params, pos.entry_atr or None
        )
        info.levels[code] = (stop, target, basis)
        info.bars_held[code] = pos.bars_held
        if pos.peak_close > 0:
            info.peaks[code] = pos.peak_close
        positions.append(
            Position(
                code=code,
                shares=pos.shares,
                # 每股成本含買進手續費，跟 positions.yaml 的 cost 同一個定義。
                # 注意停損停利用的是 entry_price（不含手續費）——
                # 那是引擎自己的基準，兩者刻意不混用。
                cost=pos.cost_basis / pos.shares,
                entry_date=pos.entry_date,
                thesis=pos.entry_reason or "程式交易引擎依進場規則買進。",
                invalidate=_invalidate_text(params.max_hold_bars),
                core=False,
                source=SOURCE_ENGINE,
            )
        )

    positions.sort(key=lambda p: p.code)
    return positions, info


def load(doc: dict, account: paper.Account | None = None) -> MonitorSet:
    """依 positions.yaml 的 source 決定這一輪要監控誰。"""
    source, warnings = resolve_source(doc)
    manual_open, manual_closed = manual_positions(doc)
    watchlist = list(doc.get("watchlist") or [])

    if source == SOURCE_MANUAL:
        return MonitorSet(
            positions=manual_open,
            closed=manual_closed,
            watchlist=watchlist,
            source=source,
            warnings=warnings,
        )

    engine_rows, info = engine_positions(account)

    if info.error:
        warnings.append(info.error)
    elif not info.enabled:
        warnings.append(
            "config/positions.yaml 的 source 設成監控程式交易引擎，"
            "但 config/paper.yaml 的 enabled 是 false——引擎不會再更新，"
            "以下顯示的是它停在最後一次執行時的部位。"
        )

    if source == SOURCE_ENGINE:
        if manual_open:
            warnings.append(
                f"positions.yaml 裡還有 {len(manual_open)} 筆手動持股，"
                "但 source=engine，這次不列入監控（資料沒有被刪掉）。"
            )
        return MonitorSet(
            positions=engine_rows,
            closed=[],
            watchlist=watchlist,
            source=source,
            engine=info,
            warnings=warnings,
        )

    # both：兩邊都看。同一檔在兩邊都有時會出現兩列，這是刻意的，
    # 但一定要講出來——不講的話那看起來就像重複計算的 bug。
    both_codes = {p.code for p in manual_open} & {p.code for p in engine_rows}
    if both_codes:
        warnings.append(
            f"{'、'.join(sorted(both_codes))} 手動持股與引擎部位都有，"
            "下面會各列一行，總成本與總市值是兩者相加。"
        )
    return MonitorSet(
        positions=manual_open + engine_rows,
        closed=manual_closed,
        watchlist=watchlist,
        source=source,
        engine=info,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 評估
# --------------------------------------------------------------------------


def _engine_notes(ev: Evaluation, info: EngineInfo) -> None:
    """把「引擎接下來要做什麼」寫進這一列的備註。

    監控層對引擎部位的意義跟對手動持股不一樣:
    手動持股是「你該考慮賣了」，引擎部位是「它明天開盤就會賣」。
    這兩件事不能長得一樣。
    """
    code = ev.position.code

    for order in info.pending_for(code):
        side = "買進" if order["side"] == "BUY" else "賣出"
        detail = order["detail"] or order["reason"] or "—"
        ev.notes.append(
            f"引擎已排定明日開盤{side} {order['shares']:,} 股：{detail}"
        )

    held = info.bars_held.get(code)
    if held is not None and info.max_hold_bars > 0:
        remaining = info.max_hold_bars - held
        if remaining <= 0:
            ev.notes.append(
                f"已抱滿 {held}/{info.max_hold_bars} 根 K 線，觸發時間出場。"
            )
        elif remaining <= 3:
            ev.notes.append(
                f"已抱 {held}/{info.max_hold_bars} 根 K 線，"
                f"再 {remaining} 根未觸發停損停利就會時間出場。"
            )


def evaluate_all(
    mset: MonitorSet,
    quotes: dict[str, Quote],
    base_rules: Rules,
    overrides: dict[str, dict],
    today: date | None = None,
) -> tuple[list[Evaluation], list[str]]:
    """對這一輪的所有部位算訊號。回傳（評估結果, 額外警告）。

    main.py 與 webapp 共用這一支。之前兩邊各有一份幾乎一樣的迴圈，
    改了一邊忘了另一邊，畫面與日報就會對不起來。
    """
    warnings: list[str] = []
    manual_rows = [p for p in mset.positions if not p.is_engine]
    peaks = load_peaks(manual_rows)

    evaluations: list[Evaluation] = []
    for position in mset.positions:
        quote = quotes.get(position.code)
        if quote is None:
            warnings.append(
                f"{position.code} 查無當日行情——請確認代號是否正確，"
                "或該檔是否停牌。"
            )

        if position.is_engine:
            stop, target, basis = mset.engine.levels.get(
                position.code, (None, None, "")
            )
            ev = evaluate(
                position,
                quote,
                # 引擎部位不套用個股例外規則：那些例外是為手動持股寫的，
                # 套上去會讓停損線又跟引擎兜不起來。
                base_rules,
                peak_price=mset.engine.peaks.get(position.code),
                today=today,
                stop_override=stop,
                target_override=target,
                basis_label=basis,
            )
            _engine_notes(ev, mset.engine)
        else:
            ev = evaluate(
                position,
                quote,
                resolve_rules(base_rules, overrides, position.code),
                peak_price=peaks.get(position.code),
                today=today,
            )
        evaluations.append(ev)

    return evaluations, warnings


def staleness_warning(info: EngineInfo, trade_date: str) -> str | None:
    """引擎的狀態停在更早的日子時提醒一句。

    引擎沒跑（排程沒裝、當天關機）而行情有更新時，畫面上的部位是舊的，
    但每一格數字都照樣算得出來——沒有這句話就完全看不出來。
    """
    if not info.available or not info.last_date or not trade_date:
        return None
    if info.last_date >= trade_date:
        return None
    return (
        f"程式交易引擎的部位停在 {info.last_date}，但目前行情是 {trade_date}。"
        "下面的部位與委託是引擎最後一次執行的結果，不是今天的判斷。"
    )
