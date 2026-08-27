"""把「程式交易的實際成交」接進監控層。

為什麼需要這支
--------------
原本的 config/positions.yaml 是手動維護的：你買了什麼就自己去登記一筆，
順便寫下「當初為什麼買」。那套設計預設你是人工選股。

如果實際下單的是程式，這個前提就不成立了——
每天手動抄成交明細不可能持久，而只要有一天沒抄，
停損提醒就是對著一份過期的持股清單在算，比沒有提醒更危險。

所以改成反過來：**程式的成交紀錄才是唯一事實來源**，
positions.yaml 由它產生。

這樣監控層的角色也跟著變了。它不再是「提醒你該賣了」——
程式自己會賣。它變成**稽核**：

    程式照它自己的規則，現在應該已經出場了嗎？實際上出場了嗎？

全自動交易最常見的壞法不是策略失效，是程式安靜地壞掉：
API 斷線、委託被退、部位卡住、成交價跟假設差很多。
這些在券商 App 上看不出來，要有一份每日對帳才會浮出來。

輸入格式
--------
`data/fills.csv`，欄位是任何券商匯出檔都會有的最小集合：

    date,code,side,shares,price,fee,tax,note
    2026-08-03,2330,BUY,1000,1150.0,164,0,突破訊號進場
    2026-08-14,2330,SELL,1000,1210.0,172,363,停利出場

    date   成交日 YYYY-MM-DD
    code   股票代號
    side   BUY / SELL（大小寫不拘，也吃「買」「賣」）
    shares 股數（正整數）
    price  每股成交價
    fee    手續費（可省略，預設 0）
    tax    證交稅（可省略，預設 0）
    note   備註（可省略）

欄位順序不重要，多餘的欄位會被忽略——直接把券商的匯出檔改個欄位名就能用。

用法
----
    python3 src/fills.py --check                 只檢查，印出會怎麼改
    python3 src/fills.py --sync                  真的寫進 positions.yaml
    python3 src/fills.py --file 我的成交.csv --sync
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FILLS_FILE = DATA_DIR / "fills.csv"
REAL_TRADES_FILE = DATA_DIR / "real_trades.jsonl"

BUY_WORDS = {"buy", "b", "買", "買進", "買入"}
SELL_WORDS = {"sell", "s", "賣", "賣出", "沖賣"}


class FillsError(RuntimeError):
    """成交檔有問題。刻意讓它炸出來——用壞掉的成交紀錄去同步持股，
    結果是一份看起來正常、實際上錯的持股清單，那比讀不出來危險得多。"""


@dataclass
class Fill:
    date: str
    code: str
    side: str        # "BUY" | "SELL"
    shares: int
    price: float
    fee: float = 0.0
    tax: float = 0.0
    note: str = ""

    @property
    def gross(self) -> float:
        return self.price * self.shares


@dataclass
class OpenPosition:
    code: str
    shares: int = 0
    cost_total: float = 0.0     # 含手續費的總成本
    entry_date: str = ""

    @property
    def avg_cost(self) -> float:
        return self.cost_total / self.shares if self.shares else 0.0


@dataclass
class RoundTrip:
    """一筆完成的來回。欄位刻意對齊 paper.Trade，
    這樣實倉績效可以走跟模擬倉完全同一支 performance()。"""

    code: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    fees: float
    tax: float

    @property
    def net_pnl(self) -> float:
        return (
            (self.exit_price - self.entry_price) * self.shares - self.fees - self.tax
        )

    @property
    def net_pnl_pct(self) -> float:
        base = self.entry_price * self.shares
        return (self.net_pnl / base * 100) if base else 0.0

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "shares": self.shares,
            "fees": round(self.fees, 2),
            "tax": round(self.tax, 2),
            "net_pnl": round(self.net_pnl, 2),
            "net_pnl_pct": round(self.net_pnl_pct, 4),
        }


@dataclass
class Replay:
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    round_trips: list[RoundTrip] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 讀取
# --------------------------------------------------------------------------


def _norm_side(raw: str) -> str:
    text = str(raw).strip().lower()
    if text in BUY_WORDS:
        return "BUY"
    if text in SELL_WORDS:
        return "SELL"
    raise FillsError(f"看不懂的買賣別: {raw!r}（要是 BUY 或 SELL）")


def _num(raw: object, label: str, default: float | None = None) -> float:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        if default is not None:
            return default
        raise FillsError(f"{label} 是空的")
    try:
        return float(text)
    except ValueError as exc:
        raise FillsError(f"{label} 不是數字: {raw!r}") from exc


def load_fills(path: Path | None = None) -> list[Fill]:
    """讀出成交明細，依日期排序。

    壞掉的一行**不會**被跳過——成交紀錄跟一般設定檔不一樣，
    少讀一行等於少算一筆部位，而那個錯誤會一路傳到停損判斷。
    寧可整個停下來叫人去修。
    """
    path = path or FILLS_FILE
    if not path.exists():
        raise FillsError(
            f"找不到成交檔 {path}。\n"
            "格式見 src/fills.py 開頭的說明，最少要有 "
            "date,code,side,shares,price 五欄。"
        )

    fills: list[Fill] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise FillsError(f"{path} 是空的，或者沒有標題列。")
        missing = {"date", "code", "side", "shares", "price"} - {
            (name or "").strip() for name in reader.fieldnames
        }
        if missing:
            raise FillsError(
                f"{path} 缺少必要欄位: {'、'.join(sorted(missing))}\n"
                f"目前的欄位是: {'、'.join(reader.fieldnames)}"
            )

        for lineno, row in enumerate(reader, start=2):
            row = {(k or "").strip(): v for k, v in row.items()}
            if not (row.get("code") or "").strip():
                continue        # 完全空白的行跳過
            try:
                shares = int(_num(row.get("shares"), "shares"))
                if shares <= 0:
                    raise FillsError(f"股數要是正整數，讀到 {shares}")
                fills.append(
                    Fill(
                        date=str(row["date"]).strip(),
                        code=str(row["code"]).strip(),
                        side=_norm_side(row["side"]),
                        shares=shares,
                        price=_num(row.get("price"), "price"),
                        fee=_num(row.get("fee"), "fee", 0.0),
                        tax=_num(row.get("tax"), "tax", 0.0),
                        note=str(row.get("note") or "").strip(),
                    )
                )
            except FillsError as exc:
                raise FillsError(f"{path} 第 {lineno} 行：{exc}") from exc

    fills.sort(key=lambda f: (f.date, f.code))
    return fills


# --------------------------------------------------------------------------
# 重播
# --------------------------------------------------------------------------


def replay(fills: list[Fill]) -> Replay:
    """依時間重播所有成交，算出「現在還持有什麼」與「完成了哪些來回」。

    成本用**加權平均含手續費**：分批進場時每一批的價格不同，
    停損線要以整體平均成本為基準，不是最後一批的價格。

    賣出時按比例結轉成本，剩下的部位維持同一個平均成本——
    這是最貼近券商對帳單的算法，也讓「剩餘部位的成本」跟你在
    App 上看到的數字對得起來。
    """
    out = Replay()

    for fill in fills:
        pos = out.open_positions.get(fill.code)

        if fill.side == "BUY":
            if pos is None:
                pos = OpenPosition(code=fill.code, entry_date=fill.date)
                out.open_positions[fill.code] = pos
            pos.shares += fill.shares
            pos.cost_total += fill.gross + fill.fee
            continue

        # SELL
        if pos is None or pos.shares <= 0:
            out.warnings.append(
                f"{fill.date} 賣出 {fill.code} {fill.shares} 股，"
                "但紀錄裡沒有這檔的部位——成交檔可能不完整（缺了更早的買進）。"
            )
            continue
        if fill.shares > pos.shares:
            out.warnings.append(
                f"{fill.date} 賣出 {fill.code} {fill.shares} 股，"
                f"但手上只有 {pos.shares} 股。以實際持有股數計算。"
            )
            fill = Fill(**{**fill.__dict__, "shares": pos.shares})

        entry_price = pos.avg_cost
        sold_cost = entry_price * fill.shares
        out.round_trips.append(
            RoundTrip(
                code=fill.code,
                entry_date=pos.entry_date,
                exit_date=fill.date,
                entry_price=entry_price,
                exit_price=fill.price,
                shares=fill.shares,
                # 買進的手續費已經含在 entry_price 裡，這裡只記賣出這側
                fees=fill.fee,
                tax=fill.tax,
            )
        )
        pos.shares -= fill.shares
        pos.cost_total -= sold_cost
        if pos.shares <= 0:
            out.open_positions.pop(fill.code, None)

    return out


# --------------------------------------------------------------------------
# 同步進 positions.yaml
# --------------------------------------------------------------------------


@dataclass
class SyncPlan:
    to_add: list[OpenPosition] = field(default_factory=list)
    to_exit: list[tuple[str, str, float]] = field(default_factory=list)
    to_update: list[tuple[str, int, float]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.to_add or self.to_exit or self.to_update)


def build_sync_plan(state: Replay) -> SyncPlan:
    """比對「成交紀錄算出來的持股」與「positions.yaml 目前寫的」。

    先算出計畫再執行，是為了讓 --check 能在不改任何東西的情況下
    把差異印出來。第一次接上去的時候，你會想先看一眼再按下去。
    """
    plan = SyncPlan()
    doc = store.load_positions_doc()
    current = {
        str(e.get("code")).strip(): e
        for e in (doc.get("positions") or [])
        if not e.get("exit_date")
    }

    for code, pos in state.open_positions.items():
        existing = current.get(code)
        if existing is None:
            plan.to_add.append(pos)
            continue
        same_shares = int(existing.get("shares", 0)) == pos.shares
        same_cost = abs(float(existing.get("cost", 0)) - pos.avg_cost) < 0.005
        if same_shares and same_cost:
            plan.unchanged.append(code)
        else:
            plan.to_update.append((code, pos.shares, pos.avg_cost))

    # positions.yaml 上還開著、但成交紀錄說已經賣光的 → 補上出場
    for code in current:
        if code in state.open_positions:
            continue
        closed = [t for t in state.round_trips if t.code == code]
        if closed:
            last = max(closed, key=lambda t: t.exit_date)
            plan.to_exit.append((code, last.exit_date, last.exit_price))

    return plan


def apply_sync_plan(plan: SyncPlan) -> None:
    """執行同步。store 每次寫入前都會自動備份 positions.yaml。"""
    for pos in plan.to_add:
        store.add_position(
            code=pos.code,
            shares=pos.shares,
            cost=round(pos.avg_cost, 4),
            entry_date=pos.entry_date,
            thesis="程式交易部位（由 data/fills.csv 同步）",
            invalidate="由程式的出場規則決定；此欄不適用於自動交易。",
        )
    for code, shares, cost in plan.to_update:
        store.update_position(code, shares=shares, cost=round(cost, 4))
    for code, exit_date, exit_price in plan.to_exit:
        store.exit_position(code, exit_date, exit_price)


def write_real_trades(round_trips: list[RoundTrip]) -> int:
    """把完成的來回寫成 data/real_trades.jsonl。

    整份覆寫而不是 append——這份檔案是 fills.csv 的衍生物，
    成交檔才是事實來源。改了成交檔就該重新產生，
    兩份 append-only 的紀錄各自長大只會兜不起來。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REAL_TRADES_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for trip in round_trips:
            fh.write(json.dumps(trip.to_dict(), ensure_ascii=False) + "\n")
    tmp.replace(REAL_TRADES_FILE)
    return len(round_trips)


def performance(round_trips: list[RoundTrip]) -> dict:
    """實倉績效。指標定義跟模擬倉一致，兩邊才比得起來。"""
    closed = len(round_trips)
    wins = [t for t in round_trips if t.net_pnl > 0]
    losses = [t for t in round_trips if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    return {
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / closed * 100, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "net_pnl": round(sum(t.net_pnl for t in round_trips), 2),
        "total_fees": round(sum(t.fees + t.tax for t in round_trips), 2),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把程式交易的實際成交同步進持股監控。"
    )
    parser.add_argument("--file", help=f"成交檔路徑（預設 {FILLS_FILE}）")
    parser.add_argument(
        "--check", action="store_true", help="只比對並印出差異，不寫任何檔案"
    )
    parser.add_argument(
        "--sync", action="store_true", help="真的寫進 config/positions.yaml"
    )
    args = parser.parse_args()

    path = Path(args.file) if args.file else FILLS_FILE
    try:
        fills = load_fills(path)
    except FillsError as exc:
        print(f"❌ {exc}")
        return 1

    state = replay(fills)

    print(f"讀入 {len(fills)} 筆成交（{path}）")
    print(f"目前持有 {len(state.open_positions)} 檔、"
          f"完成 {len(state.round_trips)} 筆來回\n")

    for warning in state.warnings:
        print(f"⚠️  {warning}")
    if state.warnings:
        print()

    if state.open_positions:
        print("成交紀錄算出來的持股：")
        for code, pos in sorted(state.open_positions.items()):
            print(f"  {code:>6}  {pos.shares:>7,} 股  均價 {pos.avg_cost:>10,.2f}"
                  f"  自 {pos.entry_date}")
        print()

    if state.round_trips:
        stats = performance(state.round_trips)
        pf = stats["profit_factor"]
        print("實倉績效（跟模擬倉同一套指標定義）：")
        print(f"  完成交易 {stats['trades']} 筆　勝 {stats['wins']} / "
              f"負 {stats['losses']}　勝率 {stats['win_rate']:.1f}%")
        print(f"  獲利因子 {pf if pf is not None else '—'}　"
              f"淨損益 {stats['net_pnl']:+,.0f}　"
              f"手續費+稅 {stats['total_fees']:,.0f}")
        if stats["trades"] < 30:
            print(f"  ⚠️ 只有 {stats['trades']} 筆，樣本太小，還不具統計意義。")
        print()

    plan = build_sync_plan(state)
    if plan.is_empty:
        print("✅ positions.yaml 已經跟成交紀錄一致，沒有要改的。")
    else:
        print("positions.yaml 需要這些調整：")
        for pos in plan.to_add:
            print(f"  ＋ 新增 {pos.code} {pos.shares:,} 股 @ {pos.avg_cost:,.2f}")
        for code, shares, cost in plan.to_update:
            print(f"  ～ 更新 {code} → {shares:,} 股 @ {cost:,.2f}")
        for code, exit_date, exit_price in plan.to_exit:
            print(f"  － 出場 {code} 於 {exit_date} @ {exit_price:,.2f}")

    if not args.sync:
        print("\n這次沒有寫檔。加上 --sync 才會真的改 positions.yaml。")
        return 0

    apply_sync_plan(plan)
    count = write_real_trades(state.round_trips)
    print(f"\n✅ 已同步 positions.yaml")
    print(f"✅ 已寫入 {REAL_TRADES_FILE.name}（{count} 筆來回）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
