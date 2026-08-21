"""每日投資報告入口。

    python src/main.py                # 產生今日報告
    python src/main.py --dry-run      # 只印到終端機，不寫檔
    python src/main.py --quiet        # 不印到終端機，只寫檔（給 cron 用）

流程:
    1. 讀 config/positions.yaml 與 config/strategy.yaml
    2. 抓全市場收盤行情（原始 JSON 存到 data/raw/）
    3. 算損益、停損停利距離、產生訊號
    4. 寫 Markdown 報告到 data/reports/YYYY-MM-DD.md
    5. append 一行結構化紀錄到 data/signals.jsonl（永不改寫，覆盤用）
    6. 跑模擬倉（config/paper.yaml 的 enabled: true 時）：
       用今日開盤成交昨日委託、用今日收盤產生明日委託，全程虛擬不下真單
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import datasource  # noqa: E402
import paperdaily  # noqa: E402
import report as report_mod  # noqa: E402
from portfolio import (  # noqa: E402
    SIGNAL_LOG,
    Evaluation,
    Position,
    Rules,
    evaluate,
    load_peaks,
    resolve_rules,
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


def load_positions(raw: dict) -> tuple[list[Position], list[dict]]:
    positions: list[Position] = []
    for entry in raw.get("positions") or []:
        positions.append(
            Position(
                code=str(entry["code"]).strip(),
                shares=int(entry["shares"]),
                cost=float(entry["cost"]),
                entry_date=str(entry["entry_date"]),
                thesis=str(entry.get("thesis", "")),
                invalidate=str(entry.get("invalidate", "")),
                core=bool(entry.get("core", False)),
                exit_date=entry.get("exit_date"),
                exit_price=entry.get("exit_price"),
            )
        )
    watchlist = list(raw.get("watchlist") or [])
    return positions, watchlist


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
) -> dict:
    return {
        "trade_date": trade_date,
        "generated_at": generated_at.isoformat(timespec="seconds"),
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
    args = parser.parse_args()

    generated_at = datetime.now()
    warnings: list[str] = []

    strategy = load_yaml(CONFIG_DIR / "strategy.yaml")
    positions_cfg = load_yaml(CONFIG_DIR / "positions.yaml")

    rules_cfg = strategy.get("rules") or {}
    base_rules = Rules(
        stop_loss_pct=float(rules_cfg.get("stop_loss_pct", 10.0)),
        take_profit_pct=float(rules_cfg.get("take_profit_pct", 22.0)),
        stop_basis=str(rules_cfg.get("stop_basis", "cost")),
        near_threshold_pct=float(rules_cfg.get("near_threshold_pct", 3.0)),
    )
    overrides = strategy.get("overrides") or {}
    overrides = {str(k): v for k, v in overrides.items() if v}
    objective = str(strategy.get("objective", "（尚未設定目標）"))

    all_positions, watchlist_cfg = load_positions(positions_cfg)
    positions = [p for p in all_positions if p.is_open]
    if not positions:
        warnings.append("目前沒有登記任何未出場的持股。")

    if not args.quiet:
        print("抓取全市場收盤行情…", file=sys.stderr)
    quotes = datasource.fetch_quotes(raw_dir=None if args.dry_run else RAW_DIR)
    trade_date = datasource.market_date(quotes) or date.today().isoformat()

    stale_days = datasource.is_stale(trade_date)
    if stale_days > 1:
        warnings.append(
            f"行情日期為 {trade_date}，距今 {stale_days} 天。"
            "可能是連假，或資料源尚未更新——判讀訊號前請先確認。"
        )

    peaks = load_peaks(positions)

    evaluations: list[Evaluation] = []
    for position in positions:
        rules = resolve_rules(base_rules, overrides, position.code)
        quote = quotes.get(position.code)
        if quote is None:
            warnings.append(f"{position.code} 查無當日行情，已跳過訊號判斷。")
        evaluations.append(
            evaluate(
                position,
                quote,
                rules,
                peak_price=peaks.get(position.code),
            )
        )

    summary = summarize(evaluations)

    watchlist: list[tuple[dict, datasource.Quote | None]] = [
        (entry, quotes.get(str(entry.get("code", "")).strip()))
        for entry in watchlist_cfg
    ]

    # 模擬倉：完全獨立於上面的持股監控，出錯也不該讓日報產不出來。
    paper_data = None
    if not args.no_paper:
        try:
            paper_data = paperdaily.run_daily(trade_date, quotes, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"模擬倉執行失敗（不影響持股監控）：{exc}")

    markdown = report_mod.build_report(
        trade_date=trade_date,
        generated_at=generated_at,
        objective=objective,
        evaluations=evaluations,
        summary=summary,
        watchlist=watchlist,
        warnings=warnings,
        paper_data=paper_data,
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
            trade_date, generated_at, base_rules, evaluations, summary
        )
    )

    if not args.quiet:
        print(f"\n報告已寫入：{report_path}", file=sys.stderr)
        print(f"訊號紀錄已附加：{SIGNAL_LOG}", file=sys.stderr)
        if paper_data and "result" in paper_data:
            print(f"模擬倉淨值：{paper_data['result'].equity:,.0f}", file=sys.stderr)

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
