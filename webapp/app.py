"""投資儀表板 —— 本機 Web 介面。

只綁定 127.0.0.1，資料完全留在你的電腦上，不對外開放。

啟動方式（一般使用者請雙擊桌面圖示，不需要跑這行）:
    python3 webapp/app.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flask import (  # noqa: E402
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import research as research_mod  # noqa: E402
import store  # noqa: E402
from portfolio import (  # noqa: E402
    Evaluation,
    Position,
    Rules,
    Signal,
    evaluate,
    resolve_rules,
    summarize,
)

app = Flask(__name__)
app.secret_key = "local-only-investment-dashboard"  # 僅供 flash 訊息，非安全用途

REPORT_DIR = ROOT / "data" / "reports"


# --------------------------------------------------------------------------
# 模板輔助
# --------------------------------------------------------------------------

@app.template_filter("money")
def fmt_money(value) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}NT${abs(value):,.0f}"


@app.template_filter("pct")
def fmt_pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


@app.template_filter("price")
def fmt_price(value) -> str:
    return "—" if value is None else f"{value:,.2f}"


SIGNAL_CLASS = {
    Signal.STOP_LOSS: "sig-stop",
    Signal.TAKE_PROFIT: "sig-profit",
    Signal.NEAR_STOP: "sig-near-stop",
    Signal.NEAR_TARGET: "sig-near-target",
    Signal.HOLD: "sig-hold",
    Signal.CORE: "sig-core",
    Signal.NO_DATA: "sig-nodata",
}


# --------------------------------------------------------------------------
# 資料組裝
# --------------------------------------------------------------------------

def build_view(force_refresh: bool = False) -> dict:
    warnings: list[str] = []

    strategy = store.load_strategy_doc()
    positions_doc = store.load_positions_doc()

    rules_cfg = strategy.get("rules") or {}
    base_rules = Rules(
        stop_loss_pct=float(rules_cfg.get("stop_loss_pct", 10.0)),
        take_profit_pct=float(rules_cfg.get("take_profit_pct", 22.0)),
        stop_basis=str(rules_cfg.get("stop_basis", "cost")),
        near_threshold_pct=float(rules_cfg.get("near_threshold_pct", 3.0)),
    )
    overrides = {
        str(k): v for k, v in (strategy.get("overrides") or {}).items() if v
    }
    objective = str(strategy.get("objective") or "").strip()

    # 行情（抓不到就降級用快取，不要讓整個畫面掛掉）
    fetched_at = None
    try:
        quotes, fetched_at, is_fresh = store.get_quotes(force=force_refresh)
    except Exception as exc:  # noqa: BLE001
        cached = store.cached_quotes_or_none()
        if cached is None:
            raise
        quotes, fetched_at = cached
        warnings.append(
            f"這次連線抓取失敗（{exc}），顯示的是 "
            f"{fetched_at.strftime('%m/%d %H:%M')} 的快取資料。"
        )

    import datasource

    trade_date = datasource.market_date(quotes) or date.today().isoformat()
    stale_days = datasource.is_stale(trade_date)
    if stale_days > 1:
        warnings.append(
            f"行情日期為 {trade_date}，距今 {stale_days} 天。"
            "可能是連假或資料源尚未更新——判讀訊號前請先確認。"
        )

    # 部位
    all_entries = positions_doc.get("positions") or []
    open_positions: list[Position] = []
    closed_rows: list[dict] = []

    for entry in all_entries:
        code = str(entry.get("code")).strip()
        position = Position(
            code=code,
            shares=int(entry["shares"]),
            cost=float(entry["cost"]),
            entry_date=str(entry["entry_date"]),
            thesis=str(entry.get("thesis") or ""),
            invalidate=str(entry.get("invalidate") or ""),
            core=bool(entry.get("core", False)),
            exit_date=entry.get("exit_date"),
            exit_price=entry.get("exit_price"),
        )
        if position.is_open:
            open_positions.append(position)
        else:
            exit_price = float(position.exit_price or 0)
            realized = (exit_price - position.cost) * position.shares
            closed_rows.append(
                {
                    "position": position,
                    "name": quotes[code].name if code in quotes else "—",
                    "exit_price": exit_price,
                    "realized": realized,
                    "realized_pct": (exit_price / position.cost - 1) * 100
                    if position.cost
                    else 0.0,
                }
            )

    peaks = _load_peaks({p.code for p in open_positions})

    rows: list[dict] = []
    evaluations: list[Evaluation] = []
    for position in open_positions:
        rules = resolve_rules(base_rules, overrides, position.code)
        quote = quotes.get(position.code)
        if quote is None:
            warnings.append(
                f"{position.code} 查無當日行情——請確認代號是否正確，或該檔是否停牌。"
            )
        ev = evaluate(position, quote, rules, peak_price=peaks.get(position.code))
        evaluations.append(ev)
        rows.append(
            {
                "ev": ev,
                "name": quote.name if quote else "—",
                "css": SIGNAL_CLASS[ev.signal],
            }
        )

    # 觸發訊號排最前面，其餘依損益排序
    order = {
        Signal.STOP_LOSS: 0,
        Signal.TAKE_PROFIT: 1,
        Signal.NEAR_STOP: 2,
        Signal.NEAR_TARGET: 3,
        Signal.HOLD: 4,
        Signal.CORE: 5,
        Signal.NO_DATA: 6,
    }
    rows.sort(key=lambda r: (order[r["ev"].signal], -(r["ev"].pnl_pct or 0)))

    summary = summarize(evaluations)
    realized_total = sum(r["realized"] for r in closed_rows)

    watch_rows = []
    for entry in positions_doc.get("watchlist") or []:
        code = str(entry.get("code") or "").strip()
        watch_rows.append(
            {
                "code": code,
                "note": str(entry.get("note") or ""),
                "quote": quotes.get(code),
            }
        )

    return {
        "objective": objective,
        "rules": base_rules,
        "trade_date": trade_date,
        "fetched_at": fetched_at,
        "warnings": warnings,
        "rows": rows,
        "closed_rows": closed_rows,
        "realized_total": realized_total,
        "summary": summary,
        "actionable": [r for r in rows if r["ev"].signal.is_actionable],
        "watch_rows": watch_rows,
        "journal": store.load_journal(trade_date),
        "today": date.today().isoformat(),
    }


def _load_peaks(codes: set[str]) -> dict[str, float]:
    import json

    log = ROOT / "data" / "signals.jsonl"
    peaks: dict[str, float] = {}
    if not log.exists():
        return peaks
    with log.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            for item in record.get("positions", []):
                code, close = item.get("code"), item.get("close")
                if code in codes and isinstance(close, (int, float)):
                    peaks[code] = max(peaks.get(code, 0.0), float(close))
    return peaks


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@app.route("/")
def dashboard():
    view = build_view(force_refresh=False)
    return render_template("dashboard.html", **view)


@app.post("/refresh")
def refresh():
    try:
        store.get_quotes(force=True)
        flash("已更新報價。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"更新失敗：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/position/add")
def position_add():
    form = request.form
    try:
        store.add_position(
            code=form["code"],
            shares=int(form["shares"]),
            cost=float(form["cost"]),
            entry_date=form["entry_date"] or date.today().isoformat(),
            thesis=form.get("thesis", ""),
            invalidate=form.get("invalidate", ""),
            core=form.get("core") == "on",
        )
        flash(f"已新增持股 {form['code']}。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"新增失敗：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/position/exit")
def position_exit():
    form = request.form
    try:
        store.exit_position(
            code=form["code"],
            exit_date=form.get("exit_date") or date.today().isoformat(),
            exit_price=float(form["exit_price"]),
        )
        store.add_journal(
            form.get("trade_date", date.today().isoformat()),
            f"{form['code']} 出場，成交價 {form['exit_price']}。"
            f"{form.get('reason', '').strip()}",
            tag="exit",
        )
        flash(f"{form['code']} 已標記出場。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"出場失敗：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/position/update")
def position_update():
    form = request.form
    try:
        store.update_position(
            form["code"],
            shares=form.get("shares"),
            cost=form.get("cost"),
            thesis=form.get("thesis"),
            invalidate=form.get("invalidate"),
        )
        flash(f"{form['code']} 已更新。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"更新失敗：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/watch/add")
def watch_add():
    try:
        store.add_watch(request.form["code"], request.form.get("note", ""))
        flash("已加入觀察清單。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"加入失敗：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/watch/remove")
def watch_remove():
    store.remove_watch(request.form["code"])
    flash("已從觀察清單移除。", "ok")
    return redirect(url_for("dashboard"))


@app.post("/journal/add")
def journal_add():
    text = request.form.get("text", "").strip()
    if text:
        store.add_journal(
            request.form.get("trade_date", date.today().isoformat()),
            text,
            tag=request.form.get("tag", "note"),
        )
        flash("已記錄。", "ok")
    return redirect(url_for("dashboard"))


@app.route("/settings")
def settings():
    strategy = store.load_strategy_doc()
    rules_cfg = strategy.get("rules") or {}
    return render_template(
        "settings.html",
        objective=str(strategy.get("objective") or "").strip(),
        rules=Rules(
            stop_loss_pct=float(rules_cfg.get("stop_loss_pct", 10.0)),
            take_profit_pct=float(rules_cfg.get("take_profit_pct", 22.0)),
            stop_basis=str(rules_cfg.get("stop_basis", "cost")),
            near_threshold_pct=float(rules_cfg.get("near_threshold_pct", 3.0)),
        ),
        changelog=list(strategy.get("changelog") or [])[::-1],
    )


@app.post("/settings/rules")
def settings_rules():
    form = request.form
    try:
        store.update_rules(
            stop_loss_pct=float(form["stop_loss_pct"]),
            take_profit_pct=float(form["take_profit_pct"]),
            stop_basis=form.get("stop_basis", "cost"),
            near_threshold_pct=float(form["near_threshold_pct"]),
            reason=form.get("reason", ""),
        )
        flash("規則已更新，並寫入 changelog。", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"更新失敗：{exc}", "error")
    return redirect(url_for("settings"))


@app.post("/settings/objective")
def settings_objective():
    store.update_objective(request.form.get("objective", ""))
    flash("目標已更新。", "ok")
    return redirect(url_for("settings"))


@app.post("/quit")
def quit_app():
    """關閉伺服器。讓使用者不必回終端機按 Ctrl+C。"""
    import os
    import threading

    threading.Timer(0.5, lambda: os._exit(0)).start()
    return render_template("quit.html")


@app.route("/history")
def history():
    reports = sorted(
        (p for p in REPORT_DIR.glob("*.md")), key=lambda p: p.stem, reverse=True
    )
    return render_template(
        "history.html",
        reports=[p.stem for p in reports],
        journal=store.load_journal()[::-1][:100],
    )


@app.route("/help")
def help_page():
    """使用說明。

    刻意做成畫面上的一頁，而不是只放在 README——
    會來翻 README 的人本來就看得懂，
    真正需要說明的人不會去打開專案資料夾裡的 .md 檔。
    """
    return render_template("help.html")


@app.route("/research")
def research():
    """研究筆記：價格算出來的事實 + 產業背景 + 名詞辭典。

    刻意跟「今日」分開：那頁是「今天要不要動作」，
    這頁是「我到底買了什麼、現在在發生什麼」——兩種完全不同的閱讀節奏。
    """
    import history as history_mod

    codes = history_mod.universe_from_config()
    view = research_mod.build_view(codes)

    glossary = research_mod.load_glossary()
    query = (request.args.get("q") or "").strip()
    term_hits = [
        {"key": k, **item} for k, item in research_mod.search_terms(query, glossary)
    ] if query else []

    categories: dict[str, list[dict]] = {}
    for key, item in glossary.items():
        categories.setdefault(item.get("category", "其他"), []).append(
            {"key": key, **item}
        )

    return render_template(
        "research.html",
        query=query,
        term_hits=term_hits,
        categories=categories,
        **view,
    )


def main() -> None:
    import webbrowser
    import threading

    port = 5173
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"投資儀表板已啟動：{url}\n關閉這個視窗即可結束。")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
