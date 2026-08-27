"""程式交易成交匯入的測試。

跑法:
    python3 tests/test_fills.py

守的是一件很貴的事：用錯的持股清單去算停損。
成交檔少讀一行、成本算錯一點，停損線就是錯的，
而畫面上看起來完全正常——這正是「安靜地錯」最典型的樣子。

全部用合成資料與暫存目錄，不會動到 config/positions.yaml。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fills  # noqa: E402
import store  # noqa: E402
from fills import Fill, FillsError  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def write_csv(path: Path, rows: str) -> Path:
    path.write_text(rows, encoding="utf-8")
    return path


TMP = Path(tempfile.mkdtemp(prefix="fills-test-"))


# ==========================================================================
print("--- 讀取：壞掉就要停下來，不能跳過 ---")
# ==========================================================================
# 一般設定檔壞一行可以跳過，成交紀錄不行。
# 少算一筆買進 = 少一個部位 = 那個部位沒有停損保護。

_ok = write_csv(
    TMP / "ok.csv",
    "date,code,side,shares,price,fee,tax,note\n"
    "2026-06-02,2330,BUY,1000,1150.0,164,0,進場\n"
    "2026-06-20,2330,BUY,1000,1210.0,172,0,加碼\n"
    "2026-07-15,2330,SELL,2000,1305.0,372,783,停利\n",
)
check("正常的檔案讀得出來", len(fills.load_fills(_ok)) == 3)

for label, body in [
    ("股數不是數字", "date,code,side,shares,price\n2026-01-02,2330,BUY,abc,1150\n"),
    ("股數是 0", "date,code,side,shares,price\n2026-01-02,2330,BUY,0,1150\n"),
    ("買賣別看不懂", "date,code,side,shares,price\n2026-01-02,2330,XX,1000,1150\n"),
    ("缺必要欄位", "date,code,shares,price\n2026-01-02,2330,1000,1150\n"),
]:
    try:
        fills.load_fills(write_csv(TMP / "bad.csv", body))
        check(f"{label} → 要丟出 FillsError", False, "沒有丟出例外")
    except FillsError:
        check(f"{label} → 要丟出 FillsError", True)

def _capture(path: Path) -> str:
    try:
        fills.load_fills(path)
    except FillsError as exc:
        return str(exc)
    return ""


check("找不到檔案時訊息要講得清楚", "找不到成交檔" in _capture(TMP / "nope.csv"))
check(
    "中文的買賣別也吃得下",
    fills.load_fills(write_csv(
        TMP / "zh.csv",
        "date,code,side,shares,price\n2026-01-02,2330,買,1000,1150\n"
        "2026-01-05,2330,賣出,1000,1200\n",
    ))[0].side == "BUY",
)
check(
    "多餘的欄位不會擋住讀取（券商匯出檔通常一堆用不到的欄）",
    len(fills.load_fills(write_csv(
        TMP / "extra.csv",
        "date,code,side,shares,price,券商,帳號\n2026-01-02,2330,BUY,1000,1150,元大,123\n",
    ))) == 1,
)


# ==========================================================================
print()
print("--- 重播：成本是加權平均含手續費 ---")
# ==========================================================================
# 分批進場時每批價格不同。停損線要以整體平均成本為基準，
# 用最後一批的價格算會把停損線放在錯的地方。

_state = fills.replay(fills.load_fills(_ok))
check("全部賣掉之後沒有留下部位", _state.open_positions == {}, str(_state.open_positions))
check("產生一筆完成的來回", len(_state.round_trips) == 1)

_trip = _state.round_trips[0]
# (1150*1000+164 + 1210*1000+172) / 2000 = 1180.168
check(
    "進場價 = 加權平均含買進手續費",
    abs(_trip.entry_price - 1180.168) < 1e-6,
    str(_trip.entry_price),
)
# (1305 - 1180.168) * 2000 - 372 - 783 = 248,509
check(
    "淨損益扣掉了賣出手續費與證交稅",
    abs(_trip.net_pnl - 248_509.0) < 0.01,
    str(_trip.net_pnl),
)
check("進場日是第一批的日期", _trip.entry_date == "2026-06-02", _trip.entry_date)

# 部分賣出：剩下的部位要維持同一個平均成本
_partial = fills.replay([
    Fill("2026-01-02", "X", "BUY", 2000, 100.0, 200.0),
    Fill("2026-02-02", "X", "SELL", 1000, 120.0, 100.0, 50.0),
])
check("部分賣出後部位還在", "X" in _partial.open_positions)
check(
    "剩下的股數正確",
    _partial.open_positions["X"].shares == 1000,
    str(_partial.open_positions["X"].shares),
)
check(
    "剩下的部位維持同一個平均成本（100.10）",
    abs(_partial.open_positions["X"].avg_cost - 100.1) < 1e-9,
    str(_partial.open_positions["X"].avg_cost),
)

# 成交檔不完整的情況要出聲，不能安靜地算出一個錯的部位
_orphan = fills.replay([Fill("2026-01-02", "X", "SELL", 1000, 100.0)])
check("賣了一檔沒有買過的股票 → 要留下警告", len(_orphan.warnings) == 1, str(_orphan.warnings))
check("而且不會產生假的來回紀錄", _orphan.round_trips == [])

_over = fills.replay([
    Fill("2026-01-02", "X", "BUY", 1000, 100.0),
    Fill("2026-02-02", "X", "SELL", 5000, 120.0),
])
check("賣超過持有股數 → 要留下警告", len(_over.warnings) == 1, str(_over.warnings))
check(
    "而且以實際持有股數計算，不會算出負部位",
    _over.round_trips and _over.round_trips[0].shares == 1000
    and "X" not in _over.open_positions,
    str(_over.round_trips),
)


# ==========================================================================
print()
print("--- 同步計畫：先看差異，再決定要不要寫 ---")
# ==========================================================================
# 第一次把程式接上來的時候，你會想先看一眼它打算怎麼改 positions.yaml。
# 所以「算計畫」跟「執行計畫」是分開的兩步。

_cfg = TMP / "positions.yaml"
_cfg.write_text(
    "positions: []\n\nwatchlist:\n  - code: \"2330\"\n    note: 測試\n",
    encoding="utf-8",
)
store.POSITIONS_FILE = _cfg
store.DATA_DIR = TMP
store.BACKUP_DIR = TMP / "backups"

_live = fills.replay(fills.load_fills(write_csv(
    TMP / "live.csv",
    "date,code,side,shares,price,fee\n"
    "2026-08-04,2317,BUY,2000,240.5,205\n",
)))
_plan = fills.build_sync_plan(_live)
check("新部位會出現在「要新增」裡", len(_plan.to_add) == 1 and _plan.to_add[0].code == "2317")
check("沒有要出場的", _plan.to_exit == [])
check("計畫不是空的", not _plan.is_empty)

fills.apply_sync_plan(_plan)
_after = store.load_positions_doc()
_open = [e for e in _after["positions"] if not e.get("exit_date")]
check("執行之後 positions.yaml 真的有這筆", len(_open) == 1, str(_open))
check(
    "成本寫的是含手續費的均價 240.6025",
    _open and abs(float(_open[0]["cost"]) - 240.6025) < 1e-6,
    str(_open[0]["cost"]) if _open else "",
)
check("代號保持字串（006208 不能變成數字 6208）", _open and isinstance(_open[0]["code"], str))

# 再跑一次同一份成交檔，不應該重複新增
check("重跑同一份成交檔不會重複新增", fills.build_sync_plan(_live).is_empty)

# 賣掉之後要自動補上 exit_date
_sold = fills.replay(fills.load_fills(write_csv(
    TMP / "sold.csv",
    "date,code,side,shares,price,fee,tax\n"
    "2026-08-04,2317,BUY,2000,240.5,205,0\n"
    "2026-08-20,2317,SELL,2000,260.0,222,1560\n",
)))
_plan2 = fills.build_sync_plan(_sold)
check("賣光之後會產生「要出場」", len(_plan2.to_exit) == 1, str(_plan2.to_exit))
fills.apply_sync_plan(_plan2)
_still_open = [
    e for e in store.load_positions_doc()["positions"] if not e.get("exit_date")
]
check("執行之後那筆部位被標成已出場", _still_open == [], str(_still_open))


# ==========================================================================
print()
print("--- 實倉績效跟模擬倉用同一套定義 ---")
# ==========================================================================
# 兩邊的勝率如果用不同定義算，三方對照（實倉 / 模擬倉 / 0050）就沒有意義。

_stats = fills.performance(_sold.round_trips)
check("完成交易筆數", _stats["trades"] == 1, str(_stats["trades"]))
check("勝率", _stats["win_rate"] == 100.0, str(_stats["win_rate"]))
check(
    "手續費與稅都算進去",
    abs(_stats["total_fees"] - 1782.0) < 0.01,
    str(_stats["total_fees"]),
)
check("沒有虧損交易時獲利因子是 None，不是 0", _stats["profit_factor"] is None)
check("空清單不會炸", fills.performance([])["trades"] == 0)

fills.REAL_TRADES_FILE = TMP / "real_trades.jsonl"
fills.DATA_DIR = TMP
check(
    "來回紀錄寫得出去",
    fills.write_real_trades(_sold.round_trips) == 1
    and fills.REAL_TRADES_FILE.exists(),
)
check(
    "重寫一次不會變成兩筆（這份檔案是衍生物，不是 append-only）",
    fills.write_real_trades(_sold.round_trips) == 1
    and len(fills.REAL_TRADES_FILE.read_text(encoding="utf-8").strip().split("\n")) == 1,
)


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
