"""每日投資報告入口。

    python src/main.py                # 產生今日報告
    python src/main.py --dry-run      # 只印到終端機，不寫檔
    python src/main.py --quiet        # 不印到終端機，只寫檔（給 cron 用）

流程:
    1. 讀 config/positions.yaml 與 config/strategy.yaml
    2. 抓全市場收盤行情（原始 JSON 存到 data/raw/）
    3. 跑程式交易引擎（config/paper.yaml 的 enabled: true 時）：
       用今日開盤成交昨日委託、用今日收盤產生明日委託，全程虛擬不下真單
    4. 決定監控哪一份部位（positions.yaml 的 source：manual / engine / both），
       算損益、停損停利距離、產生訊號
    5. 寫 Markdown 報告到 data/reports/YYYY-MM-DD.md
    6. append 一行結構化紀錄到 data/signals.jsonl（永不改寫，覆盤用）

引擎跑在監控之前，順序是有意義的:
source=engine 時監控的是引擎的部位帳本，先評估再跑引擎的話，
今天早上剛成交的那幾筆不會出現在今天的報告裡。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

# Windows 的主控台預設是 GBK/cp950，而這支程式的輸出全是中文，還帶著
# ⚠ 🔴 之類的符號——不改編碼的話，一遇到 GBK 放不進去的字元就直接
# UnicodeEncodeError 中斷，報告只印出前面半段。tests/ 底下每一支都做了
# 同樣的事（見 commit 00c43f2），但正式的進入點當時漏掉了。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


import datasource  # noqa: E402
import monitor  # noqa: E402
import paperdaily  # noqa: E402
import report as report_mod  # noqa: E402
from portfolio import (  # noqa: E402
    SIGNAL_LOG,
    Evaluation,
    Rules,
    summarize,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = DATA_DIR / "reports"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到設定檔: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def append_signal_log(record: dict) -> None:
    """append-only。這份檔案是模擬期覆盤的證據，不要回頭編輯。"""
    SIGNAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SIGNAL_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_signal_record(
    trade_date: str,
    generated_at: datetime,
    rules: Rules,
    evaluations: list[Evaluation],
    summary: dict,
    source: str = "manual",
) -> dict:
    return {
        "trade_date": trade_date,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "source": source,
        "rules": {
            "stop_loss_pct": rules.stop_loss_pct,
            "take_profit_pct": rules.take_profit_pct,
            "stop_basis": rules.stop_basis,
            "near_threshold_pct": rules.near_threshold_pct,
        },
        "summary": {
            "cost_basis": round(summary["cost_basis"], 2),
            "market_value": round(summary["market_value"], 2),
            "pnl": round(summary["pnl"], 2),
            "pnl_pct": round(summary["pnl_pct"], 4),
        },
        "positions": [
            {
                "code": ev.position.code,
                "shares": ev.position.shares,
                "cost": ev.position.cost,
                "close": ev.quote.close if ev.quote else None,
                "pnl_pct": round(ev.pnl_pct, 4) if ev.pnl_pct is not None else None,
                "stop_price": round(ev.stop_price, 4),
                "target_price": round(ev.target_price, 4),
                "stop_basis": ev.basis_label or ev.rules.stop_basis,
                "source": ev.position.source,
                "signal": ev.signal.value,
            }
            for ev in evaluations
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="台股每日投資報告")
    parser.add_argument("--dry-run", action="store_true", help="只印出，不寫檔")
    parser.add_argument("--quiet", action="store_true", help="不印到終端機")
    parser.add_argument("--no-email", action="store_true", help="不寄送通知信")
    parser.add_argument(
        "--no-paper", action="store_true", help="跳過模擬倉（只做持股監控）"
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="排程斷線後補跑：把漏掉的交易日逐日推進，而不是直接跳到今天",
    )
    parser.add_argument(
        "--claim-owner",
        action="store_true",
        help="把模擬倉帳本的擁有權轉到這台機器（決策機換人時才用，先 git pull）",
    )
    args = parser.parse_args()

    generated_at = datetime.now()
    warnings: list[str] = []

    strategy = load_yaml(CONFIG_DIR / "strategy.yaml")
    positions_cfg = load_yaml(CONFIG_DIR / "positions.yaml")

    rules_cfg = strategy.get("rules") or {}
    base_rules = Rules.from_config(rules_cfg)
    overrides = strategy.get("overrides") or {}
    overrides = {str(k): v for k, v in overrides.items() if v}
    objective = str(strategy.get("objective", "（尚未設定目標）"))

    if not args.quiet:
        print("抓取全市場收盤行情…", file=sys.stderr)
    quotes = datasource.fetch_quotes(
        raw_dir=None if args.dry_run else RAW_DIR, warnings=warnings
    )
    trade_date = datasource.market_date(quotes) or date.today().isoformat()

    stale_days = datasource.is_stale(trade_date)
    if stale_days > 1:
        warnings.append(
            f"行情日期為 {trade_date}，距今 {stale_days} 天。"
            "可能是連假，或資料源尚未更新——判讀訊號前請先確認。"
        )

    # 程式交易引擎先跑。它出錯不該讓日報產不出來，但 source=engine 時
    # 監控的就是它的部位帳本，所以順序不能反過來（見檔案開頭的說明）。
    paper_data = None
    if not args.no_paper:
        try:
            paper_data = paperdaily.run_daily(
                trade_date,
                quotes,
                args.dry_run,
                catch_up=args.catch_up,
                claim_owner=args.claim_owner,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"程式交易引擎執行失敗：{exc}")

    # 監控哪一份部位由 positions.yaml 的 source 決定。
    # 引擎剛跑完的話直接用那份帳戶狀態，不要再從檔案讀一次舊的。
    mset = monitor.load(positions_cfg, (paper_data or {}).get("account"))
    warnings.extend(mset.warnings)

    if mset.source != monitor.SOURCE_MANUAL:
        if args.no_paper:
            warnings.append(
                "這次帶了 --no-paper，引擎沒有執行——"
                "下面監控的是狀態檔裡上一輪的部位，不是今天的。"
            )
        stale = monitor.staleness_warning(mset.engine, trade_date)
        if stale:
            warnings.append(stale)

    if not mset.positions:
        warnings.append(
            "程式交易引擎目前沒有持有任何部位。"
            if mset.source == monitor.SOURCE_ENGINE
            else "目前沒有登記任何未出場的持股。"
        )

    # --- 除權息偵測 ---
    # 除息當天股價會真的跌下去，但那不是虧損——你拿到了現金。
    # 不講清楚的話，一檔配息 8% 的股票會在除息當天直接觸發 10% 停損，
    # 而報告上會寫著「跌破停損線，規則判定該出場」。那是最貴的一種假訊號。
    if mset.positions or mset.watchlist:
        try:
            import adjust

            watched = [str(e.get("code", "")).strip() for e in mset.watchlist]
            held_codes = [p.code for p in mset.positions]
            detected = adjust.scan_quotes(quotes, held_codes + watched)
            if not args.dry_run:
                adjust.record_detected(detected)
            for action in detected:
                held = " ← 你有這檔部位，今天的停損訊號不可信"
                warnings.append(
                    f"🔔 {action.code} 今天疑似除權息"
                    f"（參考價較昨收 {(action.factor - 1) * 100:+.1f}%）。"
                    "當天的價格下跌是配息造成的，不是虧損。"
                    f"{held if action.code in held_codes else ''}"
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"除權息偵測失敗（不影響其他判斷）：{exc}")

    evaluations, eval_warnings = monitor.evaluate_all(
        mset, quotes, base_rules, overrides
    )
    warnings.extend(eval_warnings)

    summary = summarize(evaluations)

    watchlist: list[tuple[dict, datasource.Quote | None]] = [
        (entry, quotes.get(str(entry.get("code", "")).strip()))
        for entry in mset.watchlist
    ]

    markdown = report_mod.build_report(
        trade_date=trade_date,
        generated_at=generated_at,
        objective=objective,
        evaluations=evaluations,
        summary=summary,
        watchlist=watchlist,
        warnings=warnings,
        paper_data=paper_data,
        mset=mset,
    )

    if not args.quiet:
        print(markdown)

    if args.dry_run:
        print("\n[dry-run] 未寫入任何檔案。", file=sys.stderr)
        return 0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{trade_date}.md"
    report_path.write_text(markdown, encoding="utf-8")

    append_signal_log(
        build_signal_record(
            trade_date, generated_at, base_rules, evaluations, summary,
            source=mset.source,
        )
    )

    if not args.quiet:
        print(f"\n報告已寫入：{report_path}", file=sys.stderr)
        print(f"訊號紀錄已附加：{SIGNAL_LOG}", file=sys.stderr)
        print(f"監控來源：{mset.source_label}", file=sys.stderr)
        if paper_data and "result" in paper_data:
            print(f"引擎淨值：{paper_data['result'].equity:,.0f}", file=sys.stderr)

    # 有觸發訊號才寄信；未設定 config/mail.yaml 就安靜略過
    if not args.no_email:
        import notify

        status = notify.notify_if_actionable(
            trade_date, summary["actionable"], summary
        )
        print(status, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
