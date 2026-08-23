"""儀表板的跨站請求防護測試。

跑法:
    python3 tests/test_webapp.py

守的是一件事：**綁 127.0.0.1 不等於安全。**

它擋得住「別台機器連進來」，擋不住「你自己的瀏覽器被別的網站指使」。
你開著儀表板去逛任何一個網頁，那個網頁都能放一張隱藏表單自動 POST
到 127.0.0.1:5173——關掉儀表板、塞一筆假持股、清空觀察清單、改掉停損規則。
它讀不到回應，但這些全都是「寫」，它不需要讀。

用 Flask 的 test client，不開真的伺服器、不連外網。
會寫入設定檔的那幾支（store.*）在這裡全部換成假的，
測的是「請求有沒有被擋下來」，不是 store 本身。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "webapp"))

import app as webapp  # noqa: E402
import store  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


# 任何一支真的動到設定檔的函式都換掉。這支測試只關心請求擋不擋得住，
# 讓它去寫 config/positions.yaml 是完全沒必要的副作用。
CALLED: list[str] = []
store.add_watch = lambda *a, **kw: CALLED.append("add_watch")   # type: ignore[assignment]
store.remove_watch = lambda *a, **kw: CALLED.append("remove_watch")  # type: ignore[assignment]

webapp.app.config["TESTING"] = True
client = webapp.app.test_client()


# ==========================================================================
# 1. 樣板真的有把驗證碼印出來
# ==========================================================================
# 用 /settings 而不是 /，因為首頁會去抓行情（要連外網）。
resp = client.get("/settings")
check("GET /settings 正常回應", resp.status_code == 200, str(resp.status_code))

body = resp.get_data(as_text=True)
tokens = re.findall(r'name="_csrf" value="([^"]+)"', body)
# 3 個：設定頁自己的兩張表單，加上 base.html 版面上的「結束儀表板」。
check("設定頁的表單都帶了驗證碼", len(tokens) == 3, f"找到 {len(tokens)} 個")
check("驗證碼不是空字串", bool(tokens and tokens[0].strip()), str(tokens[:1]))
check("同一頁上的驗證碼一致", len(set(tokens)) == 1, str(set(tokens)))

TOKEN = tokens[0] if tokens else ""


# ==========================================================================
# 2. 沒有驗證碼的 POST 一律擋下來
# ==========================================================================
# 這正是跨站表單的樣子：它送得出請求，但拿不到 token。
naked = webapp.app.test_client()   # 全新的 client＝沒有 session cookie

for path, data in [
    ("/watch/add", {"code": "2330"}),
    ("/watch/remove", {"code": "2330"}),
    ("/position/add", {"code": "2330", "shares": "1000", "cost": "100"}),
    ("/settings/rules", {"stop_loss_pct": "99"}),
    ("/quit", {}),
    ("/refresh", {}),
]:
    r = naked.post(path, data=data)
    check(f"沒有驗證碼：POST {path} 被擋下", r.status_code == 400, str(r.status_code))

check(
    "被擋下的請求完全沒有碰到 store",
    CALLED == [],
    str(CALLED),
)


# ==========================================================================
# 3. 驗證碼錯了也擋下來
# ==========================================================================
r = client.post("/watch/add", data={"code": "2330", "_csrf": "wrong-token"})
check("驗證碼不對：POST 被擋下", r.status_code == 400, str(r.status_code))
check("而且一樣沒碰到 store", CALLED == [], str(CALLED))


# ==========================================================================
# 4. 正常操作不能被擋到 —— 防護做過頭跟沒做一樣糟
# ==========================================================================
r = client.post("/watch/add", data={"code": "2330", "_csrf": TOKEN})
check(
    "帶著正確驗證碼：POST 正常通過（302 導回）",
    r.status_code == 302,
    str(r.status_code),
)
check("這次真的呼叫到了 store.add_watch", CALLED == ["add_watch"], str(CALLED))


# ==========================================================================
# 5. Host 不是本機就拒絕（DNS rebinding）
# ==========================================================================
r = client.get("/settings", headers={"Host": "evil.example.com"})
check("外部 Host 的 GET 被拒絕", r.status_code == 403, str(r.status_code))

r = client.post(
    "/quit", data={"_csrf": TOKEN}, headers={"Host": "evil.example.com"}
)
check("外部 Host 的 POST 被拒絕", r.status_code == 403, str(r.status_code))

for host in ("127.0.0.1:5173", "localhost:5173", "127.0.0.1"):
    r = client.get("/settings", headers={"Host": host})
    check(f"本機 Host 照常放行：{host}", r.status_code == 200, str(r.status_code))


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗 ❌")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過 ✅")
