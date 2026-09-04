"""Email 通知。

⚠️ 憑證絕不寫在程式碼裡，也不進版控。
   設定放在 config/mail.yaml（已被 .gitignore 排除），由你自己建立填寫。

預設只在「有觸發停損／停利」時寄信，平常不打擾。
"""

from __future__ import annotations

import hashlib
import json
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAIL_CONFIG = ROOT / "config" / "mail.yaml"

# 上一封信的去重標記。排程一天會跑兩次（15:00 主跑 + 17:30 重試），
# 兩次算出同一批訊號就會寄兩封一樣的信——這個檔案記住「這個交易日、
# 這批內容已經寄過」。屬於機器狀態，不進版控（.gitignore 排除）。
LAST_EMAIL_MARKER = ROOT / "data" / "last_email.json"


class MailNotConfigured(Exception):
    """尚未設定 config/mail.yaml —— 屬於正常狀態，不是錯誤。"""


def load_mail_config() -> dict:
    if not MAIL_CONFIG.exists():
        raise MailNotConfigured(
            "找不到 config/mail.yaml，略過寄信。"
            "（複製 config/mail.example.yaml 並填入你的設定即可啟用）"
        )
    with MAIL_CONFIG.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not cfg.get("enabled", False):
        raise MailNotConfigured("config/mail.yaml 的 enabled 為 false，略過寄信。")
    for key in ("smtp_host", "smtp_port", "username", "app_password", "to"):
        if not cfg.get(key):
            raise MailNotConfigured(f"config/mail.yaml 缺少必要欄位: {key}")
    return cfg


def build_alert_email(
    *, trade_date: str, actionable: list, summary: dict
) -> tuple[str, str]:
    """回傳 (主旨, 純文字內容)。"""
    codes = "、".join(
        f"{e.position.code}"
        f"{'停損' if e.signal.value == 'STOP_LOSS' else '停利'}"
        for e in actionable
    )
    subject = f"[投資提醒] {trade_date} 有 {len(actionable)} 檔觸發訊號：{codes}"

    lines = [
        f"交易日：{trade_date}",
        "",
        "── 需要你決定 ─────────────────────",
        "",
    ]
    for ev in actionable:
        action = "停損出場" if ev.signal.value == "STOP_LOSS" else "停利出場"
        line_label = "停損線" if ev.signal.value == "STOP_LOSS" else "停利線"
        line_price = (
            ev.stop_price if ev.signal.value == "STOP_LOSS" else ev.target_price
        )
        lines += [
            f"● {ev.position.code}　{ev.signal.label}",
            f"   現價 {ev.quote.close:,.2f}，{line_label} {line_price:,.2f}",
            f"   未實現 {ev.pnl_pct:+.2f}%（NT${ev.pnl:,.0f}）",
            f"   規則判定：{action}",
        ]
        if ev.position.thesis:
            lines.append(f"   當初買進理由：{ev.position.thesis}")
        if ev.position.invalidate:
            lines.append(f"   當初認錯條件：{ev.position.invalidate}")
        lines.append("")

    lines += [
        "── 投組總覽 ───────────────────────",
        "",
        f"總成本　NT${summary['cost_basis']:,.0f}",
        f"總市值　NT${summary['market_value']:,.0f}",
        f"未實現　NT${summary['pnl']:,.0f}（{summary['pnl_pct']:+.2f}%）",
        "",
        "打開儀表板查看完整報告：http://127.0.0.1:5173/",
        "",
        "──────────────────────────────────",
        "系統只負責計算與提醒，下單與否由你決定。",
        "若決定不照訊號執行，請在儀表板的決策紀錄寫下理由。",
        "本信依你自己設定的規則產生，不構成投資建議。",
    ]
    return subject, "\n".join(lines)


def send(subject: str, body: str, cfg: dict | None = None) -> None:
    cfg = cfg or load_mail_config()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["username"]
    recipients = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    context = ssl.create_default_context()
    port = int(cfg["smtp_port"])
    host = cfg["smtp_host"]

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(cfg["username"], cfg["app_password"])
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(cfg["username"], cfg["app_password"])
            server.send_message(message)


def _content_digest(trade_date: str, subject: str, body: str) -> str:
    """同一個交易日、同一份內容 → 同一個指紋。"""
    raw = f"{trade_date}\n{subject}\n{body}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _already_sent(digest: str) -> bool:
    """這份內容是不是已經寄過。標記檔壞掉或讀不到一律當「沒寄過」——
    寧可重寄一封，也不要因為去重機制故障而漏掉一封該寄的信。"""
    try:
        record = json.loads(LAST_EMAIL_MARKER.read_text(encoding="utf-8"))
        return record.get("digest") == digest
    except Exception:  # noqa: BLE001
        return False


def _mark_sent(digest: str, trade_date: str) -> None:
    """寄出後留下標記。寫入失敗不往外丟——信已經寄出去了，
    標記只是防重寄，它壞掉的代價（多收一封）比讓排程掛掉小得多。"""
    try:
        LAST_EMAIL_MARKER.write_text(
            json.dumps({"digest": digest, "trade_date": trade_date}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def notify_if_actionable(trade_date: str, actionable: list, summary: dict) -> str:
    """有觸發訊號才寄信。回傳一句給終端機看的狀態說明。

    同一個交易日、同一批內容只寄一次——排程一天跑兩次（15:00 + 17:30 重試），
    沒有這個檢查的話，主跑成功的日子重試會把同一封信再寄一遍。
    內容變了（例如重試補跑後多出訊號）仍會照寄。
    """
    if not actionable:
        return "無觸發訊號，未寄送 Email。"
    try:
        cfg = load_mail_config()
    except MailNotConfigured as exc:
        return str(exc)
    subject, body = build_alert_email(
        trade_date=trade_date, actionable=actionable, summary=summary
    )
    digest = _content_digest(trade_date, subject, body)
    if _already_sent(digest):
        return f"這份提醒（{trade_date}）今天已寄過，未重寄。"
    try:
        send(subject, body, cfg)
    except Exception as exc:  # noqa: BLE001 - 寄信失敗不該讓整個排程掛掉
        return f"寄信失敗：{exc}"
    _mark_sent(digest, trade_date)
    return f"已寄出提醒信到 {cfg['to']}。"


def _send_test() -> int:
    """寄一封測試信，驗證 config/mail.yaml 設定是否可用。

    給第一次設定的人跑的：`python src/notify.py --test`。
    正式的提醒信只在觸發停損／停利時才寄，沒有這個指令的話，
    設定錯了要等到第一次真的觸發那天才會發現——而那天正是最不能漏信的一天。
    """
    try:
        cfg = load_mail_config()
    except MailNotConfigured as exc:
        print(f"設定還沒完成：{exc}")
        return 1
    if "還沒填" in str(cfg.get("app_password", "")):
        print("config/mail.yaml 的 app_password 還沒填。")
        print("到 https://myaccount.google.com/apppasswords 產生 16 碼應用程式密碼，")
        print("貼進 config/mail.yaml 的 app_password 欄位後再跑一次。")
        return 1
    try:
        send(
            "[投資提醒] 測試信",
            "這是一封測試信。收到它代表 Email 通知設定完成，\n"
            "之後有部位觸發停損／停利時就會收到提醒。\n\n"
            "本信由 python src/notify.py --test 手動觸發。",
            cfg,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"寄信失敗：{exc}")
        print("最常見的原因：app_password 貼錯（要用應用程式密碼，不是登入密碼）。")
        return 1
    print(f"測試信已寄出到 {cfg['to']}，去信箱確認一下（也看看垃圾郵件夾）。")
    return 0


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        raise SystemExit(_send_test())
    print("用法：python src/notify.py --test  （寄一封測試信驗證設定）")
    raise SystemExit(2)
