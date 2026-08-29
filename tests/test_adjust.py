"""還原權值的測試。

跑法:
    python3 tests/test_adjust.py

守的是一件很容易靜靜壞掉的事：價格序列裡的假斷崖。
0050 在 2025-06-18 從 188.65 掉到 47.57，那不是崩盤，是 1 拆 4。
沒有還原的話，均線、期間報酬、停損判斷全部跟著錯，而畫面上完全看不出來。

全部用合成資料，不連外網。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import adjust  # noqa: E402
from adjust import Action  # noqa: E402
from history import Bar  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def bar(date: str, close: float, volume: int = 1_000_000) -> Bar:
    return Bar(date=date, open=close, high=close, low=close, close=close, volume=volume)


# ==========================================================================
print("--- 往前調整：動歷史，不動現在 ---")
# ==========================================================================
# 方向很重要。現價、停損線、券商 App 上的數字必須是同一個，
# 不能為了讓歷史好看而去改今天的價格。

_split = Action(code="X", date="2026-03-02", factor=0.25, kind="split")
_bars = [
    bar("2026-02-26", 400.0, 1_000_000),
    bar("2026-02-27", 404.0, 1_000_000),
    bar("2026-03-02", 101.0, 4_000_000),   # 1 拆 4 之後
    bar("2026-03-03", 102.0, 4_000_000),
]
_adj = adjust.apply_actions(_bars, [_split])

check("分割日之前的價格被壓下來", abs(_adj[0].close - 100.0) < 1e-9, str(_adj[0].close))
check("分割日之前的第二根也一樣", abs(_adj[1].close - 101.0) < 1e-9, str(_adj[1].close))
check("分割日當天不動", abs(_adj[2].close - 101.0) < 1e-9, str(_adj[2].close))
check("分割日之後不動", abs(_adj[3].close - 102.0) < 1e-9, str(_adj[3].close))
check(
    "假斷崖消失（-75% 變成正常的 +0%）",
    abs(_adj[1].close / _adj[0].close - 1.01) < 1e-9,
    f"{(_adj[1].close / _adj[0].close - 1) * 100:+.2f}%",
)
check(
    "分割會放大歷史成交量（股數變多了）",
    _adj[0].volume == 4_000_000,
    str(_adj[0].volume),
)
check("開高低收一起調整", abs(_adj[0].open - 100.0) < 1e-9 and abs(_adj[0].high - 100.0) < 1e-9)

# 配息不改變股數，量不能動
_div = Action(code="X", date="2026-03-02", factor=0.9, kind="dividend")
_adj_div = adjust.apply_actions(_bars, [_div])
check(
    "配息只調價格，不調成交量",
    _adj_div[0].volume == 1_000_000 and abs(_adj_div[0].close - 360.0) < 1e-9,
    f"量 {_adj_div[0].volume} 價 {_adj_div[0].close}",
)


# ==========================================================================
print()
print("--- 多筆行為要累乘 ---")
# ==========================================================================
_two = [
    Action(code="X", date="2026-02-27", factor=0.5, kind="split"),
    Action(code="X", date="2026-03-03", factor=0.5, kind="split"),
]
_acc = adjust.apply_actions(_bars, _two)
check(
    "最早那根被兩次分割一起調整（400 × 0.5 × 0.5 = 100）",
    abs(_acc[0].close - 100.0) < 1e-9,
    str(_acc[0].close),
)
check(
    "中間那根只被後面那次調整（404 × 0.5 = 202）",
    abs(_acc[1].close - 202.0) < 1e-9,
    str(_acc[1].close),
)
check("最後一根永遠不動", abs(_acc[3].close - 102.0) < 1e-9, str(_acc[3].close))

check("ignore: true 的行為不套用", adjust.apply_actions(
    _bars, [Action(code="X", date="2026-03-02", factor=0.25, ignore=True)]
) == _bars)
check("沒有行為就原樣回傳", adjust.apply_actions(_bars, []) == _bars)
check("空資料不會炸", adjust.apply_actions([], [_split]) == [])


# ==========================================================================
print()
print("--- 偵測：只抓不可能是真跌的斷崖 ---")
# ==========================================================================
# 台股漲跌幅上限 10%，所以跌超過 11% 一定是公司行為。
# 跌停（-10%）不可以被誤判成除權息，否則真實的暴跌會被抹掉。

_limit_down = [bar("2026-03-02", 100.0), bar("2026-03-03", 90.0)]
check(
    "跌停 -10% 不算公司行為",
    adjust.detect_from_bars("X", _limit_down) == [],
    str(adjust.detect_from_bars("X", _limit_down)),
)

_crash = [bar("2026-03-02", 400.0), bar("2026-03-03", 100.0)]
_found = adjust.detect_from_bars("X", _crash)
check("1 拆 4 抓得到", len(_found) == 1, str(_found))
check(
    "而且因子吸附到乾淨的 1:4 比例",
    _found and abs(_found[0].factor - 0.25) < 1e-9,
    str(_found[0].factor) if _found else "",
)
check("分割會自動套用（不標 ignore）", _found and _found[0].ignore is False)

# 真實資料踩過的坑：歷史缺一個月時，「前一根」是一個月前，
# 那段期間的正常跌幅會被當成除權息。2408 就是這樣被誤判的。
_gapped = [bar("2026-06-30", 452.5), bar("2026-08-03", 396.5)]
check(
    "中間缺一個月的話不當成單日事件（2408 的誤判）",
    adjust.detect_from_bars("X", _gapped) == [],
    str(adjust.detect_from_bars("X", _gapped)),
)

# 除息的因子沒辦法從收盤價準確反推，所以一律標 ignore 等人確認
_exdiv = [
    bar("2026-03-02", 542.0),
    Bar(date="2026-03-03", open=494.0, high=505.0, low=467.5, close=467.5, volume=1000),
]
_dv = adjust.detect_from_bars("X", _exdiv)
check("疑似除息抓得到", len(_dv) == 1, str(_dv))
check(
    "但不自動套用（因子只是估計值）",
    _dv and _dv[0].ignore is True and _dv[0].kind == "dividend",
    str(_dv[0]) if _dv else "",
)
check(
    "估計值用開盤價而不是收盤價（收盤把當天的跌幅也算進去了）",
    _dv and abs(_dv[0].factor - 494.0 / 542.0) < 1e-9,
    str(_dv[0].factor) if _dv else "",
)


# ==========================================================================
print()
print("--- 從每日行情反推除權息 ---")
# ==========================================================================
# TWSE 在除權息當天，「漲跌」是相對除權息參考價算的，不是相對昨收。
# 所以 收盤 − 漲跌 ≠ 昨收 就是除權息的指紋。
# 這是唯一能在當天抓到一般現金股息的方法。

_normal = adjust.implied_action_from_quote("X", "2026-03-03", 95.0, -5.0, 100.0)
check("平常日：收盤−漲跌 = 昨收 → 沒有除權息", _normal is None, str(_normal))

_ex = adjust.implied_action_from_quote("X", "2026-03-03", 93.0, -2.0, 100.0)
check("除息日：收盤−漲跌 = 95 ≠ 昨收 100 → 抓到", _ex is not None)
check(
    "因子是參考價 ÷ 昨收 = 0.95",
    _ex and abs(_ex.factor - 0.95) < 1e-9,
    str(_ex.factor) if _ex else "",
)
check(
    "0.5% 以內的零頭當成四捨五入，不是除權息",
    adjust.implied_action_from_quote("X", "2026-03-03", 99.7, -0.2, 100.0) is None,
)


# ==========================================================================
print()
print("--- 設定檔讀寫 ---")
# ==========================================================================
_tmp = Path(tempfile.mkdtemp(prefix="adjust-test-")) / "corporate_actions.yaml"

adjust.save_actions(
    {
        "0050": [Action(code="0050", date="2025-06-18", factor=0.25, kind="split",
                        note="1 拆 4", source="detected")],
        "2603": [Action(code="2603", date="2026-06-17", factor=0.898,
                        kind="dividend", ignore=True)],
    },
    _tmp,
)
_back = adjust.load_actions(_tmp)
check("寫出去讀得回來", set(_back) == {"0050", "2603"}, str(list(_back)))
check("因子完整保留", abs(_back["0050"][0].factor - 0.25) < 1e-9)
check("ignore 旗標保留", _back["2603"][0].ignore is True)
check("kind 保留", _back["0050"][0].kind == "split")

# ref_price / prev_close 寫法：因子由系統推導，比較好稽核
_tmp.write_text(
    'actions:\n  "X":\n    - date: "2026-01-02"\n'
    "      prev_close: 200.0\n      ref_price: 190.0\n      kind: dividend\n",
    encoding="utf-8",
)
_derived = adjust.load_actions(_tmp)
check(
    "可以改填公告上的參考價與昨收，讓系統自己算因子",
    abs(_derived["X"][0].factor - 0.95) < 1e-9,
    str(_derived["X"][0].factor),
)

# 壞掉的一筆不該讓整份設定讀不出來
_tmp.write_text(
    'actions:\n  "X":\n    - date: "2026-01-02"\n      factor: 0.9\n'
    '    - date: "2026-02-02"\n      factor: "壞掉"\n'
    '    - date: "2026-03-02"\n      factor: 5.0\n',
    encoding="utf-8",
)
_partial = adjust.load_actions(_tmp)
check(
    "壞掉或不合理的行為被跳過，好的照樣讀出來",
    len(_partial.get("X", [])) == 1,
    str(_partial),
)

# 已經登記過的不該被自動偵測蓋掉（手動修正過的內容要保住）
_existing = {"X": [Action(code="X", date="2026-01-02", factor=0.95,
                          kind="dividend", source="manual", note="手動填的")]}
_merged, _added = adjust.merge_actions(
    _existing, [Action(code="X", date="2026-01-02", factor=0.80, source="detected")]
)
check("同一天已登記就不覆蓋", _added == [] and _merged["X"][0].factor == 0.95)
check("不同天才會新增", adjust.merge_actions(
    _existing, [Action(code="X", date="2026-05-05", factor=0.9)]
)[1] != [])


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
