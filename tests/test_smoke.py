"""煙霧測試：排程每天跑的進入點，以及使用者真的會點的每一頁。

這片是專案裡最大的無測試區域——main.py / report.py / notify.py /
webapp/app.py 加起來約 59 KB，而排程跑的、使用者點的，就是它們。

之前 webapp/app.py 的 history_report() 裡有一行**無條件**的
`import markdown`，讓 render_markdown() 承諾的「沒裝就退回純文字」
整個失效：沒裝 markdown 的人打開歷史報告會拿到一頁 500。
414 條斷言沒有任何一條會發現這件事，因為沒有人碰過那些路由。

這支不驗業務邏輯（那些在各自的 test_*.py 裡），只守兩件事：
    1. 這幾個模組 import 得起來
    2. 每一個 GET 頁面都不會 5xx

網路一律擋掉：CI 上沒有 data/quotes_cache.json，真的去抓會讓這支
測試變成「今天證交所在不在」的擲筊。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "webapp"))

failures: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL':4} {desc}{'  ' + extra if extra else ''}")
    if not ok:
        failures.append(desc)


# ==========================================================================
# 1. 排程真的會 import 到的模組
# ==========================================================================
import main    # noqa: E402
import notify  # noqa: E402
import report  # noqa: E402

check("main.py import 得起來", callable(getattr(main, "main", None)))
check("report.py import 得起來", callable(getattr(report, "build_report", None)))
check(
    "notify.py import 得起來",
    callable(getattr(notify, "notify_if_actionable", None)),
)

# 報告最後會被 webapp 用 |safe 渲染，而股票名稱是 TWSE/TPEx 給的，
# 不是本機產生的。上游回傳惡意字串時不能變成可執行的 HTML。
_hostile = report._safe_name("<img src=x onerror=alert(1)>")
check(
    "資料源給的股票名稱會被轉義",
    "<img" not in _hostile and "&lt;img" in _hostile,
    _hostile,
)
check(
    "一般的中文名稱不會被動到",
    report._safe_name("台積電") == "台積電",
)


# ==========================================================================
# 2. 儀表板的每一頁都不能 5xx
# ==========================================================================
import store  # noqa: E402

# 擋掉所有對外連線。回空 dict 而不是假報價：沒有行情是真的會發生的
# 狀況（收盤前、被證交所擋 IP），頁面本來就必須撐得住。
_STUB_AT = datetime.now()
store.get_quotes = lambda force=False: ({}, _STUB_AT, True)
store.cached_quotes_or_none = lambda: ({}, _STUB_AT)

import app as webapp_app  # noqa: E402

client = webapp_app.app.test_client()

PAGES = [
    ("/", "今日"),
    ("/settings", "規則設定"),
    ("/history", "歷史"),
    ("/paper", "模擬倉"),
    ("/help", "說明"),
    ("/research", "研究"),
    ("/history/2026-08-21", "單日報告全文"),
    ("/history/not-a-date", "壞掉的日期參數"),
]

for path, label in PAGES:
    try:
        resp = client.get(path)
        status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        status = -1
        check(f"{label}（{path}）沒有丟出例外", False, repr(exc)[:120])
        continue
    check(
        f"{label}（{path}）不是 5xx",
        status < 500,
        f"HTTP {status}",
    )


# ==========================================================================
# 3. 跨站送來的寫入請求要被擋下來
#
# 綁 127.0.0.1 不等於安全：使用者瀏覽的任何網站都能對 localhost 送
# 表單 POST。/quit 是 os._exit()，等於一個誰都按得到的關機鍵。
# ==========================================================================
_evil = client.post(
    "/settings/objective",
    data={"objective": "被別的網站改掉了"},
    headers={"Origin": "https://evil.example"},
)
check(
    "跨站送來的 POST 被擋下（403）",
    _evil.status_code == 403,
    f"HTTP {_evil.status_code}",
)

_evil_quit = client.post("/quit", headers={"Origin": "https://evil.example"})
check(
    "跨站送來的 /quit 被擋下（不能讓別人關掉你的儀表板）",
    _evil_quit.status_code == 403,
    f"HTTP {_evil_quit.status_code}",
)

# 反面：從儀表板自己的頁面送出來的，不能被擋
_same = client.post(
    "/watch/remove",
    data={"code": "0000"},
    headers={"Origin": "http://localhost"},
)
check(
    "同源的 POST 不會被誤擋",
    _same.status_code != 403,
    f"HTTP {_same.status_code}",
)


# ==========================================================================
# 4. 沒裝 markdown 時，歷史報告要退回純文字而不是 500
#
# requirements.txt 把 markdown 列為必裝，但 render_markdown() 寫了
# ModuleNotFoundError 的降級路徑，docstring 也明文承諾這件事。
# 兩者只要有一邊說謊就該被抓到——之前說謊的是程式。
# ==========================================================================
import builtins  # noqa: E402

_real_import = builtins.__import__


def _no_markdown(name, *args, **kwargs):
    if name == "markdown":
        raise ModuleNotFoundError("No module named 'markdown'")
    return _real_import(name, *args, **kwargs)


_saved_markdown = sys.modules.pop("markdown", None)
builtins.__import__ = _no_markdown
try:
    _resp = client.get("/history/2026-08-21")
    check(
        "沒裝 markdown 時歷史報告退回純文字，不是 500",
        _resp.status_code == 200,
        f"HTTP {_resp.status_code}",
    )
finally:
    builtins.__import__ = _real_import
    if _saved_markdown is not None:
        sys.modules["markdown"] = _saved_markdown


# ==========================================================================
print()
if failures:
    print(f"{len(failures)} 項失敗 ❌")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
