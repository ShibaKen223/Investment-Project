"""行情抓取的測試。

跑法:
    python3 tests/test_datasource.py

守的是一件實際發生過的事：上櫃端點抽風，整份日報就產不出來，
連上市持股的停損提醒都沒有——那天等於完全沒有監控。

全部用假的 HTTP 回應，不連外網。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import datasource  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


TWSE_ROWS = [
    {
        "Code": "2330", "Name": "台積電", "Date": "1150821",
        "ClosingPrice": "2410.00", "Change": "+35.00", "OpeningPrice": "2380.00",
        "HighestPrice": "2415.00", "LowestPrice": "2375.00", "TradeVolume": "30000",
    }
]
TPEX_ROWS = [
    {
        "SecuritiesCompanyCode": "3324", "CompanyName": "雙鴻", "Date": "1150821",
        "Close": "969.00", "Change": "-11.00", "Open": "975.00",
        "High": "980.00", "Low": "965.00", "TradingShares": "5000",
    }
]

_real_get_json = datasource._get_json


def install(twse_ok: bool = True, tpex_ok: bool = True) -> None:
    def fake(url: str):
        is_tpex = "tpex" in url
        if (is_tpex and not tpex_ok) or (not is_tpex and not twse_ok):
            raise RuntimeError(f"模擬失敗: {url}")
        return TPEX_ROWS if is_tpex else TWSE_ROWS

    datasource._get_json = fake


# ==========================================================================
print("--- 一個市場掛掉不該拖垮另一個 ---")
# ==========================================================================
# 原本的行為是任何一邊失敗就整支拋例外。上櫃端點不穩，
# 於是上市持股的監控跟著陪葬——而那才是絕大多數的部位。

install(twse_ok=True, tpex_ok=True)
_warn: list[str] = []
_both = datasource.fetch_quotes(raw_dir=None, warnings=_warn)
check("兩邊都正常時兩個市場的報價都在", {"2330", "3324"} <= set(_both), str(list(_both)))
check("兩邊都正常時不留警告", _warn == [], str(_warn))

install(twse_ok=True, tpex_ok=False)
_warn = []
_twse_only = datasource.fetch_quotes(raw_dir=None, warnings=_warn)
check("上櫃掛掉時上市報價照樣拿得到", "2330" in _twse_only)
check("而且不含上櫃的標的", "3324" not in _twse_only)
check("失敗要留下警告（不能讓人以為是市場沒資料）", len(_warn) == 1, str(_warn))
check("警告要講清楚是哪個市場", _warn and "上櫃" in _warn[0], str(_warn))

install(twse_ok=False, tpex_ok=True)
_warn = []
_tpex_only = datasource.fetch_quotes(raw_dir=None, warnings=_warn)
check("反過來也一樣：上市掛掉時上櫃照樣拿得到", "3324" in _tpex_only)
check("警告指出是上市", _warn and "上市" in _warn[0], str(_warn))

install(twse_ok=False, tpex_ok=False)
try:
    datasource.fetch_quotes(raw_dir=None, warnings=[])
    check("兩邊都掛才拋例外", False, "沒有拋出例外")
except RuntimeError as exc:
    check("兩邊都掛才拋例外", True)
    check("訊息要說明報告產不出來", "無法產生報告" in str(exc), str(exc))

check(
    "warnings 參數可以不給（舊呼叫方式不會壞）",
    "3324" in datasource.fetch_quotes(raw_dir=None)
    if (install(True, True) or True)
    else False,
)


# ==========================================================================
print()
print("--- 抓不到的市場不可以留下空的存檔 ---")
# ==========================================================================
# 存檔刻意不覆寫既有日期。把空清單存成當天的檔案，
# 之後 --from-raw 重建就會拿它當「那天真的沒資料」的證據，而且永遠改不掉。

import tempfile  # noqa: E402

_raw = Path(tempfile.mkdtemp(prefix="ds-test-"))
install(twse_ok=True, tpex_ok=False)
datasource.fetch_quotes(raw_dir=_raw, warnings=[])
check(
    "成功的那一邊有存檔",
    (_raw / "2026-08-21_twse.json").exists(),
    str(sorted(p.name for p in _raw.iterdir())),
)
check(
    "失敗的那一邊沒有留下空檔案",
    not (_raw / "2026-08-21_tpex.json").exists(),
    str(sorted(p.name for p in _raw.iterdir())),
)


datasource._get_json = _real_get_json

# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
