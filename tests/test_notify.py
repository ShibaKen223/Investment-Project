"""寄信去重的回歸測試。

排程一天跑兩次（15:00 主跑 + 17:30 重試），這裡守住的行為是：
同一個交易日、同一批內容只寄一次；內容變了照寄；
去重標記檔壞掉時寧可重寄也不能漏寄。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import notify  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL':4} {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def fake_event(code: str = "2330", pnl_pct: float = -8.5) -> SimpleNamespace:
    """最小可用的觸發事件，欄位對齊 build_alert_email 實際會讀的。"""
    return SimpleNamespace(
        position=SimpleNamespace(code=code, thesis="測試理由", invalidate="測試認錯"),
        signal=SimpleNamespace(value="STOP_LOSS", label="跌破停損線"),
        quote=SimpleNamespace(close=100.0),
        stop_price=101.0,
        target_price=120.0,
        pnl_pct=pnl_pct,
        pnl=-8500.0,
    )


SUMMARY = {"cost_basis": 100000.0, "market_value": 91500.0, "pnl": -8500.0, "pnl_pct": -8.5}


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    sent: list[str] = []

    # 不碰真的 SMTP 與真的設定檔：send 記下主旨就好，設定回傳假值。
    notify.send = lambda subject, body, cfg=None: sent.append(subject)
    notify.load_mail_config = lambda: {
        "smtp_host": "x", "smtp_port": 465, "username": "u",
        "app_password": "p", "to": "me@example.com",
    }
    notify.LAST_EMAIL_MARKER = tmp / "last_email.json"

    try:
        # 1. 第一次寄：要真的寄，而且留下標記
        msg1 = notify.notify_if_actionable("2026-09-04", [fake_event()], SUMMARY)
        check("第一次呼叫有寄信", len(sent) == 1, msg1)
        check("寄出後留下去重標記", notify.LAST_EMAIL_MARKER.exists())

        # 2. 同日同內容再呼叫（= 17:30 重試）：不重寄
        msg2 = notify.notify_if_actionable("2026-09-04", [fake_event()], SUMMARY)
        check("同日同內容不重寄", len(sent) == 1, msg2)
        check("回傳訊息說明已寄過", "已寄過" in msg2, msg2)

        # 3. 同日但內容不同（重試補跑後訊號變了）：照寄
        msg3 = notify.notify_if_actionable(
            "2026-09-04", [fake_event(), fake_event(code="2881", pnl_pct=15.2)], SUMMARY
        )
        check("同日不同內容照寄", len(sent) == 2, msg3)

        # 4. 標記檔壞掉：當作沒寄過，寧可重寄不可漏寄
        notify.LAST_EMAIL_MARKER.write_text("這不是 JSON{{{", encoding="utf-8")
        msg4 = notify.notify_if_actionable(
            "2026-09-04", [fake_event(), fake_event(code="2881", pnl_pct=15.2)], SUMMARY
        )
        check("標記檔壞掉時照寄不擋信", len(sent) == 3, msg4)

        # 5. 換一個交易日：照寄
        notify.notify_if_actionable("2026-09-05", [fake_event()], SUMMARY)
        check("不同交易日照寄", len(sent) == 4)

        # 6. 無訊號：不寄也不動標記
        marker_before = notify.LAST_EMAIL_MARKER.read_text(encoding="utf-8")
        msg6 = notify.notify_if_actionable("2026-09-05", [], SUMMARY)
        check("無訊號不寄信", len(sent) == 4 and "未寄送" in msg6, msg6)
        check(
            "無訊號不動標記",
            notify.LAST_EMAIL_MARKER.read_text(encoding="utf-8") == marker_before,
        )

        # 7. 標記內容確實是 JSON 且記著 trade_date（給人事後查）
        record = json.loads(notify.LAST_EMAIL_MARKER.read_text(encoding="utf-8"))
        check("標記記著交易日", record.get("trade_date") == "2026-09-05", str(record))

        print()
        if failures:
            print(f"失敗 {len(failures)} 項 ❌  {failures}")
            return 1
        print("全部通過 ✅")
        return 0
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
