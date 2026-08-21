"""每日報告產生（Markdown）。

報告一律 append-only 存檔到 data/reports/YYYY-MM-DD.md。
同一個交易日重跑會覆蓋當天的檔案，但歷史報告永遠不動——
這樣兩個月的模擬期結束後，你手上會有一份沒被事後修改過的紀錄。
"""

from __future__ import annotations

from datetime import datetime

from datasource import Quote
from portfolio import Evaluation, Signal
from strategy import exit_levels


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


def build_paper_section(paper_data: dict | None) -> list[str]:
    """模擬倉的決策報告。

    這一段要回答三個問題，缺一不可:
        今天成交了什麼？明天要下什麼單？為什麼其他標的沒買？
    第三個問題最容易被省略，但它才是你之後調參數的依據——
    只報告「做了什麼」而不報告「沒做什麼」，你會誤以為系統沒機會，
    實際上可能是名額滿了或資金不夠。
    """
    if paper_data is None:
        return []

    lines: list[str] = ["## 🧪 模擬倉", ""]
    lines.append(
        "> **這一段完全是虛擬的。** 沒有連接任何券商，不會送出任何真實委託。"
        "目的是累積「這套規則實際會做什麼決定」的證據。"
    )
    lines.append("")

    if "skipped" in paper_data:
        lines.append(f"- {paper_data['skipped']}")
        lines.append("")
        return lines

    result = paper_data["result"]
    stats = paper_data["stats"]
    account = paper_data["account"]
    params = paper_data["params"]

    # --- 今天成交了什麼 ---
    lines.append("### 今日成交（昨日委託，以今日開盤價）")
    lines.append("")
    filled = [f for f in result.fills if f.get("status") == "FILLED"]
    if filled:
        for fill in filled:
            side = "買進" if fill["side"] == "BUY" else "賣出"
            line = (
                f"- **{side} {fill['code']}** {fill['shares']:,} 股 @ "
                f"{fill['price']:,.2f}（手續費 {fill.get('fee', 0):,.0f}"
            )
            if fill["side"] == "SELL":
                line += f"、證交稅 {fill.get('tax', 0):,.0f}）"
                line += (
                    f" → 淨損益 {_money(fill.get('net_pnl'))} "
                    f"（{_pct(fill.get('net_pnl_pct'))}）"
                )
            else:
                line += "）"
            lines.append(line)
            if fill.get("detail"):
                lines.append(f"  - 理由：{fill['detail']}")
    else:
        lines.append("- 今日無成交。")

    unfilled = [f for f in result.fills if f.get("status") != "FILLED"]
    for fill in unfilled:
        lines.append(
            f"- ⚠️ {fill['code']} 委託未成交（{fill['status']}）：{fill['detail']}"
        )
    lines.append("")

    # --- 明天要下什麼單 ---
    lines.append("### 明日開盤委託")
    lines.append("")
    orders = result.sell_orders + result.buy_orders
    if orders:
        for order in orders:
            side = "買進" if order.side == "BUY" else "賣出"
            lines.append(
                f"- **{side} {order.code}** {order.shares:,} 股 — {order.detail}"
            )
    else:
        lines.append("- 明日無委託。")
    lines.append("")

    # --- 為什麼其他標的沒買 ---
    if result.rejected:
        lines.append("<details><summary>其他標的今天為什麼沒進場</summary>")
        lines.append("")
        for item in result.rejected:
            lines.append(f"- **{item['code']}**：{item['reason']}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # --- 模擬倉現況 ---
    lines.append("### 模擬倉現況")
    lines.append("")
    lines.append("| 項目 | 數值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 淨值 | {_money(result.equity)} |")
    lines.append(f"| 現金 | {_money(result.cash)} |")
    lines.append(f"| 持有檔數 | {result.holdings} / {paper_data['account_params'].max_positions} |")
    lines.append(f"| 累計報酬 | {_pct(stats['total_return_pct'])} |")
    lines.append(f"| 最大回撤 | {_pct(stats['max_drawdown_pct'])} |")
    lines.append(f"| 完成交易 | {stats['trades']} 筆（勝率 {stats['win_rate']:.0f}%）|")
    lines.append(f"| 累計手續費+稅 | {_money(stats['total_fees'])} |")
    lines.append("")

    if account.positions:
        lines.append("| 持股 | 股數 | 進場價 | 進場日 | 已抱 | 出場條件 | 停損基準 |")
        lines.append("| --- | ---: | ---: | --- | ---: | --- | --- |")
        for code, pos in account.positions.items():
            # 一定要走 exit_levels()，不要在這裡自己乘百分比——
            # stop_mode 設成 atr 時停損寬度是看進場當下的 ATR，
            # 報告如果自己算一套，印出來的線就會跟引擎實際在用的線不一樣。
            stop, target, basis = exit_levels(
                pos.entry_price, params, pos.entry_atr or None
            )
            lines.append(
                f"| {code} | {pos.shares:,} | {pos.entry_price:,.2f} | "
                f"{pos.entry_date} | {pos.bars_held}/{params.max_hold_bars} 根 | "
                f"停損 {stop:,.2f} ／ 停利 {target:,.2f} | {basis} |"
            )
        lines.append("")

    # --- 誠實提醒 ---
    if paper_data.get("orphaned"):
        lines.append(
            "> ⚠️ 模擬倉還持有 "
            f"{'、'.join(paper_data['orphaned'])}，但它們已經不在你的持股／觀察清單裡。"
            "系統仍會繼續追蹤這些部位到出場為止，"
            "不過「研究」頁不會再分析它們。"
        )
        lines.append("")
    if paper_data.get("warmup_short"):
        lines.append(
            f"> ⚠️ 這些標的歷史資料還不足 {params.warmup_bars} 根，"
            f"暫時不會產生進場訊號：{'、'.join(paper_data['warmup_short'])}"
        )
        lines.append("")
    if 0 < stats["trades"] < 30:
        lines.append(
            f"> ⚠️ 目前只有 {stats['trades']} 筆完成交易，樣本太小，"
            "上面的勝率與報酬還不具統計意義。不要依據它調整參數。"
        )
        lines.append("")

    return lines


def build_report(
    *,
    trade_date: str,
    generated_at: datetime,
    objective: str,
    evaluations: list[Evaluation],
    summary: dict,
    watchlist: list[tuple[dict, Quote | None]],
    warnings: list[str],
    paper_data: dict | None = None,
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

    # --- 模擬倉（若啟用） ---
    lines.extend(build_paper_section(paper_data))

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
