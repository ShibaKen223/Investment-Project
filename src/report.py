"""每日報告產生（Markdown）。

報告一律 append-only 存檔到 data/reports/YYYY-MM-DD.md。
同一個交易日重跑會覆蓋當天的檔案，但歷史報告永遠不動——
這樣兩個月的模擬期結束後，你手上會有一份沒被事後修改過的紀錄。
"""

from __future__ import annotations

from datetime import datetime

from datasource import Quote
from portfolio import Evaluation, Signal


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}NT${abs(value):,.0f}"


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _price(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _quote_line(quote: Quote) -> str:
    return f"{_price(quote.close)} ({_pct(quote.change_pct)})"


def build_report(
    *,
    trade_date: str,
    generated_at: datetime,
    objective: str,
    evaluations: list[Evaluation],
    summary: dict,
    watchlist: list[tuple[dict, Quote | None]],
    warnings: list[str],
) -> str:
    lines: list[str] = []

    lines.append(f"# 投資日報 · {trade_date}")
    lines.append("")
    lines.append(f"> **目標**：{objective.strip()}")
    lines.append("")
    lines.append(
        f"*行情日期 {trade_date} ｜ 產生時間 "
        f"{generated_at.strftime('%Y-%m-%d %H:%M:%S')}*"
    )
    lines.append("")

    if warnings:
        lines.append("## ⚠️ 系統提醒")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    # --- 需要人工決策的訊號放最前面 ---
    actionable = summary["actionable"]
    lines.append("## 今日待辦")
    lines.append("")
    if actionable:
        for ev in actionable:
            name = ev.quote.name if ev.quote else ev.position.code
            action = "停損出場" if ev.signal is Signal.STOP_LOSS else "停利出場"
            trigger = (
                f"跌破停損線 {_price(ev.stop_price)}"
                if ev.signal is Signal.STOP_LOSS
                else f"突破停利線 {_price(ev.target_price)}"
            )
            lines.append(
                f"- **{ev.signal.label}｜{ev.position.code} {name}** — "
                f"現價 {_price(ev.quote.close if ev.quote else None)}，{trigger}，"
                f"未實現 {_pct(ev.pnl_pct)}（{_money(ev.pnl)}）。"
            )
            lines.append(f"  - 規則判定：**{action}**，等你人工確認。")
            if ev.position.invalidate:
                lines.append(f"  - 當初設定的認錯條件：{ev.position.invalidate}")
        lines.append("")
        lines.append(
            "> 系統只負責計算與提醒，下單與否由你決定。"
            "如果決定不照訊號執行，請在下方「決策紀錄」寫下理由。"
        )
    else:
        lines.append("- 無觸發停損或停利的部位，今日不需動作。")
    lines.append("")

    # --- 投組總覽 ---
    lines.append("## 投組總覽")
    lines.append("")
    lines.append("| 項目 | 數值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 持有檔數 | {summary['positions']} |")
    lines.append(f"| 總成本 | {_money(summary['cost_basis'])} |")
    lines.append(f"| 總市值 | {_money(summary['market_value'])} |")
    lines.append(f"| 未實現損益 | {_money(summary['pnl'])} |")
    lines.append(f"| 未實現報酬率 | {_pct(summary['pnl_pct'])} |")
    lines.append("")

    # --- 個股明細 ---
    lines.append("## 持股明細")
    lines.append("")
    lines.append(
        "| 代號 | 名稱 | 收盤 (漲跌) | 成本 | 未實現 | 停損線 | 距停損 | "
        "停利線 | 距停利 | 持有 | 狀態 |"
    )
    lines.append(
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
    )
    for ev in evaluations:
        pos = ev.position
        name = ev.quote.name if ev.quote else "—"
        close = _quote_line(ev.quote) if ev.quote else "—"
        if pos.core:
            stop_cell = target_cell = to_stop = to_target = "—"
        else:
            stop_cell = _price(ev.stop_price)
            target_cell = _price(ev.target_price)
            to_stop = _pct(ev.pct_to_stop)
            to_target = _pct(ev.pct_to_target)
        lines.append(
            f"| {pos.code} | {name} | {close} | {_price(pos.cost)} | "
            f"{_pct(ev.pnl_pct)} | {stop_cell} | {to_stop} | "
            f"{target_cell} | {to_target} | {pos.holding_days()}天 | "
            f"{ev.signal.label} |"
        )
    lines.append("")
    lines.append(
        "*「距停損」／「距停利」＝ 現價要再變動多少 % 才會碰到那條線。*  \n"
        "*距停損 −14% ＝ 還要再跌 14% 才停損（仍有緩衝）；轉為正數代表已經跌破。*  \n"
        "*距停利 +16% ＝ 還要再漲 16% 才停利；轉為負數代表已經突破。*"
    )
    lines.append("")

    # --- 當初的買進理由（覆盤用）---
    lines.append("## 當初的買進理由")
    lines.append("")
    lines.append(
        "> 每天看一次。如果理由已經不成立，就算沒到停損線也該考慮出場；"
        "如果理由還成立，就算帳面虧損也不必恐慌。"
    )
    lines.append("")
    for ev in evaluations:
        pos = ev.position
        name = ev.quote.name if ev.quote else pos.code
        lines.append(f"**{pos.code} {name}**（{pos.entry_date} 進場，{_pct(ev.pnl_pct)}）")
        lines.append(f"- 買進理由：{pos.thesis or '（未填寫）'}")
        lines.append(f"- 認錯條件：{pos.invalidate or '（未填寫）'}")
        for note in ev.notes:
            lines.append(f"- ⚠️ {note}")
        lines.append("")

    # --- 觀察清單 ---
    if watchlist:
        lines.append("## 觀察清單")
        lines.append("")
        lines.append("| 代號 | 名稱 | 收盤 (漲跌) | 備註 |")
        lines.append("| --- | --- | ---: | --- |")
        for entry, quote in watchlist:
            code = entry.get("code", "")
            name = quote.name if quote else "—"
            close = _quote_line(quote) if quote else "查無行情"
            lines.append(f"| {code} | {name} | {close} | {entry.get('note', '')} |")
        lines.append("")

    # --- 決策紀錄 ---
    lines.append("## 決策紀錄")
    lines.append("")
    lines.append("<!-- 今天做了什麼、為什麼。覆盤時這裡比任何數字都有用。 -->")
    lines.append("")
    lines.append("- [ ] 今日無動作")
    lines.append("- 若覆蓋了系統訊號，理由是：")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "*本報告由自動化管線產生，僅為依既定規則計算的結果，不構成投資建議。*"
    )
    lines.append("")

    return "\n".join(lines)
