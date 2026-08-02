"""Email 通知。

⚠️ 憑證絕不寫在程式碼裡，也不進版控。
   設定放在 config/mail.yaml（已被 .gitignore 排除），由你自己建立填寫。

預設只在「有觸發停損／停利」時寄信，平常不打擾。
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAIL_CONFIG = ROOT / "config" / "mail.yaml"


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


def notify_if_actionable(trade_date: str, actionable: list, summary: dict) -> str:
    """有觸發訊號才寄信。回傳一句給終端機看的狀態說明。"""
    if not actionable:
        return "無觸發訊號，未寄送 Email。"
    try:
        cfg = load_mail_config()
    except MailNotConfigured as exc:
        return str(exc)
    subject, body = build_alert_email(
        trade_date=trade_date, actionable=actionable, summary=summary
    )
    try:
        send(subject, body, cfg)
    except Exception as exc:  # noqa: BLE001 - 寄信失敗不該讓整個排程掛掉
        return f"寄信失敗：{exc}"
    return f"已寄出提醒信到 {cfg['to']}。"
