"""每日模擬倉流程（src/paperdaily.py）的測試。

跑法:
    python3 tests/test_paperdaily.py

這支測試守的是一件事：**--dry-run 看到的東西，要跟實跑一模一樣。**

dry-run 的用途就是「今天真的送出去之前，先看看它會做什麼」。
它要是印出一份跟實跑不同的報告，那它不只沒用，而且比沒有更糟——
你會照著一份假的預覽做決定，而畫面上沒有任何地方看得出來它是假的。

全部用合成資料與暫存目錄，不連外網、不碰專案裡真正的 data/。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history  # noqa: E402
import paper  # noqa: E402
import paperdaily  # noqa: E402
from datasource import Quote  # noqa: E402
from history import Bar  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


# --------------------------------------------------------------------------
# 隔離：所有讀寫都導到暫存目錄，絕不碰專案的 data/
# --------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="paperdaily-test-"))
history.HISTORY_DIR = TMP / "history"
history.RAW_DIR = TMP / "raw"
paper.DATA_DIR = TMP
paper.STATE_FILE = TMP / "paper_state.json"
paper.TRADES_FILE = TMP / "paper_trades.jsonl"
paper.EQUITY_FILE = TMP / "paper_equity.jsonl"

CODE = "8888"
CONFIG = {
    "enabled": True,
    "account": {"initial_cash": 1_000_000, "position_pct": 20.0, "max_positions": 5},
    "costs": {"fee_discount": 0.6, "slippage_pct": 0.1},
    "strategy": {"stop_mode": "pct", "stop_loss_pct": 8.0, "take_profit_pct": 15.0},
    # 這支測試只用一檔標的，而 data_guard 預設要求掃描池裡至少 10 檔
    # 「資料足夠」才算數（那道保護是這支測試寫完之後才加的）。
    # 這裡明確關掉，才不會每一項都被判成「今天不算數」。
    # data_guard 本身由 tests/test_paper.py 負責驗。
    "data_guard": {"min_ready_codes": 0},
}

paperdaily.load_config = lambda: CONFIG                    # type: ignore[assignment]
history.universe_from_config = lambda: [CODE]              # type: ignore[assignment]


def _weekdays(count: int, start: date = date(2026, 5, 1)) -> list[str]:
    out: list[str] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


# 70 根平盤 K 當背景，最後一根是「昨天」。
# 根數要超過 warmup_bars（預設 61），否則進場判斷會整段被跳過，
# 那就測不到 dry-run 與實跑的差異了。
DATES = _weekdays(71)
YESTERDAY, TODAY = DATES[-2], DATES[-1]
HISTORY_BARS = [
    Bar(date=d, open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000)
    for d in DATES[:-1]
]

TODAY_QUOTE = {
    CODE: Quote(
        code=CODE, name="測試股", trade_date=TODAY,
        close=101.0, change=1.0, open=100.0, high=101.5, low=99.5,
        volume=1_200_000, market="上市",
    )
}


def reset_state() -> None:
    """把世界重設成「昨天收盤後、今天還沒跑」。

    歷史只到昨天，帳戶有一張昨天決定、今天開盤要成交的買單。
    """
    for path in (paper.STATE_FILE, paper.TRADES_FILE, paper.EQUITY_FILE):
        path.unlink(missing_ok=True)
    shutil.rmtree(history.HISTORY_DIR, ignore_errors=True)
    history.save_bars(CODE, HISTORY_BARS)

    account = paper.Account(cash=1_000_000.0, last_date=YESTERDAY)
    account.pending = [
        paper.Order(
            code=CODE, side="BUY", shares=1000, decided_on=YESTERDAY,
            reason="ENTRY", detail="突破前 20 日高點",
        )
    ]
    paper.save_state(account)


def fills_of(data: dict) -> list[tuple]:
    """把成交明細壓成可以直接比對的形狀。"""
    return [
        (f.get("code"), f.get("side"), f.get("status"), f.get("shares"), f.get("price"))
        for f in data["result"].fills
    ]


def orders_of(data: dict) -> list[tuple]:
    result = data["result"]
    return [
        (o.code, o.side, o.shares) for o in result.sell_orders + result.buy_orders
    ]


try:
    # ======================================================================
    # dry-run 與實跑必須做出相同的決策
    # ======================================================================
    reset_state()
    dry = paperdaily.run_daily(TODAY, TODAY_QUOTE, dry_run=True)

    reset_state()
    real = paperdaily.run_daily(TODAY, TODAY_QUOTE, dry_run=False)

    check(
        "dry-run 沒有被整段跳過",
        dry is not None and "skipped" not in dry,
        str(dry),
    )
    check(
        "實跑沒有被整段跳過",
        real is not None and "skipped" not in real,
        str(real),
    )

    check(
        "昨天的買單在實跑時以今天開盤價成交",
        fills_of(real) and fills_of(real)[0][2] == "FILLED",
        str(fills_of(real)),
    )
    check(
        "同一張買單在 dry-run 也成交，不會被當成「當日無開盤價」作廢",
        fills_of(dry) and fills_of(dry)[0][2] == "FILLED",
        str(fills_of(dry)),
    )
    check(
        "兩邊的成交明細完全一致",
        fills_of(dry) == fills_of(real),
        f"dry={fills_of(dry)}  real={fills_of(real)}",
    )
    check(
        "兩邊為明天產生的委託完全一致",
        orders_of(dry) == orders_of(real),
        f"dry={orders_of(dry)}  real={orders_of(real)}",
    )
    check(
        "兩邊的淨值一致",
        round(dry["result"].equity, 2) == round(real["result"].equity, 2),
        f"dry={dry['result'].equity}  real={real['result'].equity}",
    )

    # ======================================================================
    # 但 dry-run 仍然不准留下任何痕跡
    # ======================================================================
    reset_state()
    before_state = paper.STATE_FILE.read_text(encoding="utf-8")
    before_bars = len(history.load_bars(CODE))

    paperdaily.run_daily(TODAY, TODAY_QUOTE, dry_run=True)

    check(
        "dry-run 不寫狀態檔",
        paper.STATE_FILE.read_text(encoding="utf-8") == before_state,
    )
    check(
        "dry-run 不寫成交紀錄",
        not paper.TRADES_FILE.exists(),
    )
    check(
        "dry-run 不寫淨值曲線",
        not paper.EQUITY_FILE.exists(),
    )
    check(
        "dry-run 不把今天的 K 棒寫進歷史檔",
        len(history.load_bars(CODE)) == before_bars,
        f"{before_bars} → {len(history.load_bars(CODE))}",
    )

    # ======================================================================
    # 同一天重跑不該重複成交（既有行為，順便守住）
    # ======================================================================
    reset_state()
    paperdaily.run_daily(TODAY, TODAY_QUOTE, dry_run=False)
    again = paperdaily.run_daily(TODAY, TODAY_QUOTE, dry_run=False)
    check(
        "同一個交易日重跑會被擋下來",
        again is not None and "skipped" in again,
        str(again),
    )

finally:
    shutil.rmtree(TMP, ignore_errors=True)


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
