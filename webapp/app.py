"""投資儀表板 —— 本機 Web 介面。

只綁定 127.0.0.1，資料完全留在你的電腦上，不對外開放。

啟動方式（一般使用者請雙擊桌面圖示，不需要跑這行）:
    python3 webapp/app.py
"""

from __future__ import annotations

import json
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
    SIGNAL_LOG,
    Evaluation,
    Position,
    Rules,
    Signal,
    evaluate,
    load_peaks,
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


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
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


def _sparkline_svg(points: list[dict], width: int = 640, height: int = 90, pad: int = 6) -> str:
    """畫模擬倉淨值的簡易走勢線。用 --up/--down 是延續這個系統「紅漲綠跌」的配色。"""
    values = [p["equity"] for p in points]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)

    def x(i: int) -> float:
        return pad + (width - 2 * pad) * i / (n - 1)

    def y(v: float) -> float:
        return height - pad - (height - 2 * pad) * (v - lo) / span

    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    base_y = y(values[0])
    color = "var(--up)" if values[-1] >= values[0] else "var(--down)"
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="sparkline" role="img" aria-label="模擬倉淨值走勢">'
        f'<line x1="{pad}" y1="{base_y:.1f}" x2="{width - pad}" y2="{base_y:.1f}" '
        f'stroke="var(--border)" stroke-width="1" stroke-dasharray="3,3"/>'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


# 每日排程（launch/install_daily.sh 裝的那個）。
SCHEDULE_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "local.investment.daily.plist"
)
# 幾天沒跑就算不正常。抓 4 天是為了容忍「週五跑完 → 週一才開機」再加一天連假。
SCHEDULE_STALE_DAYS = 4


def _schedule_status() -> dict:
    """自動排程到底有沒有在跑。

    這件事一定要顯示在畫面上。說明頁寫著「系統每天自動抓收盤行情，
    你不用手動按什麼」——排程要是沒裝，那句話就是騙人的，
    而使用者沒有任何方法會發現：畫面照樣有數字、圖照樣畫，
    只是那些數字是上一次手動執行留下來的。

    「上次執行」取 signals.jsonl 的最後一筆，因為那份檔案只有
    main.py 真的跑完一輪才會 append，比檔案時間戳可信。
    """
    last_run: datetime | None = None
    for record in reversed(_read_jsonl(SIGNAL_LOG)):
        raw = record.get("generated_at")
        if not raw:
            continue
        try:
            last_run = datetime.fromisoformat(str(raw))
            break
        except ValueError:
            continue

    days_since = (date.today() - last_run.date()).days if last_run else None
    installed = SCHEDULE_PLIST.exists()
    return {
        "installed": installed,
        "last_run": last_run,
        "days_since": days_since,
        "stale": days_since is None or days_since > SCHEDULE_STALE_DAYS,
    }


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

    peaks = load_peaks(open_positions)

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
        "schedule": _schedule_status(),
    }


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


@app.route("/history/<trade_date>")
def history_report(trade_date: str):
    """打開單一天的報告全文（含模擬倉那一段）。"""
    import re

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
        return redirect(url_for("history"))
    path = REPORT_DIR / f"{trade_date}.md"
    if not path.exists():
        flash(f"找不到 {trade_date} 的報告。", "error")
        return redirect(url_for("history"))
    return render_template(
        "report_detail.html",
        trade_date=trade_date,
        content=path.read_text(encoding="utf-8"),
    )


@app.route("/paper")
def paper_page():
    """模擬倉現況。這頁完全是唯讀的——它照規則自己跑，沒有任何按鈕給你按。"""
    import paper
    import paperdaily
    import strategy as strategy_mod

    config = paperdaily.load_config()
    enabled = paperdaily.is_enabled(config)
    if not enabled:
        return render_template("paper.html", enabled=False)

    account_params = paper.AccountParams.from_dict(config.get("account"))
    params = strategy_mod.StrategyParams.from_dict(config.get("strategy"))
    try:
        account = paper.load_state(account_params.initial_cash)
    except paper.StateCorrupted as exc:
        # 顯示一則看得懂的訊息，而不是一頁 500，也不是假裝帳戶是空的。
        return render_template("paper.html", enabled=True, broken=str(exc))

    try:
        quotes, fetched_at, _ = store.get_quotes(force=False)
    except Exception:  # noqa: BLE001
        cached = store.cached_quotes_or_none()
        quotes, fetched_at = cached if cached else ({}, None)

    holdings = []
    for code, pos in account.positions.items():
        quote = quotes.get(code)
        current_price = quote.close if quote else pos.entry_price
        market_value = current_price * pos.shares
        unrealized = market_value - pos.cost_basis
        stop, target, basis = strategy_mod.exit_levels(
            pos.entry_price, params, pos.entry_atr or None
        )
        holdings.append(
            {
                "code": code,
                "name": quote.name if quote else "—",
                "pos": pos,
                "current_price": current_price,
                "unrealized": unrealized,
                "unrealized_pct": (unrealized / pos.cost_basis * 100)
                if pos.cost_basis
                else 0.0,
                "stop": stop,
                "target": target,
                "basis": basis,
            }
        )

    # 跟每日報告讀的是同一份紀錄、同一支函式——
    # 這兩邊各寫一次還原邏輯，遲早會算出兩個兜不起來的數字。
    trades = paper.load_trades()
    equity_curve = paper.load_equity_curve()
    equity_values = [round(e["equity"], 2) for e in equity_curve]
    current_prices = {h["code"]: h["current_price"] for h in holdings}
    equity_now = round(account.equity(current_prices), 2)

    # 把「用即時報價算出來的現在淨值」接在曲線尾巴上再算績效。
    # 不接的話 total_return_pct 會用曲線最後一筆（上次收盤存檔的數字），
    # 而畫面上「目前淨值」用的是即時報價——兩個數字並排卻不同步，
    # 使用者看到的是一組自己對不起來的統計。
    stats = paper.performance(
        trades, equity_values + [equity_now], account_params.initial_cash
    )

    latest_report = None
    if equity_curve:
        candidate = REPORT_DIR / f"{equity_curve[-1]['trade_date']}.md"
        if candidate.exists():
            latest_report = equity_curve[-1]["trade_date"]

    return render_template(
        "paper.html",
        enabled=True,
        account=account,
        account_params=account_params,
        params=params,
        holdings=holdings,
        pending=account.pending,
        trades=trades[::-1][:30],   # 最新的排最上面
        equity_curve=equity_curve[-60:],
        sparkline=_sparkline_svg(equity_curve[-60:]),
        stats=stats,
        equity_now=equity_now,
        latest_report=latest_report,
        fetched_at=fetched_at,
    )


@app.route("/help")
def help_page():
    """使用說明。

    刻意做成畫面上的一頁，而不是只放在 README——
    會來翻 README 的人本來就看得懂，
    真正需要說明的人不會去打開專案資料夾裡的 .md 檔。

    模擬倉的規則參數直接讀現在生效中的設定，不寫死數字——
    這樣哪天調了停損停利，這頁的說明會自動跟著對，不用回來手動改文字。
    """
    import history
    import paper
    import paperdaily
    import strategy as strategy_mod

    config = paperdaily.load_config()
    paper_enabled = paperdaily.is_enabled(config)
    params = strategy_mod.StrategyParams.from_dict(config.get("strategy"))
    account_params = paper.AccountParams.from_dict(config.get("account"))
    universe_codes = history.universe_from_config()

    return render_template(
        "help.html",
        paper_enabled=paper_enabled,
        params=params,
        account_params=account_params,
        universe_count=len(universe_codes),
    )


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
