"""模擬倉重置：把一段「產生它的設定已經改掉」的紀錄整批歸檔，重新開帳。

跑法:
    python3 src/paper_reset.py --dry-run --reason "..."   # 先看它會做什麼
    python3 src/paper_reset.py --reason "..."             # 真的執行
    （Windows 用 py 取代 python3）

為什麼需要這支程式
------------------
模擬倉的績效只有在「產生它的設定 == 現在的設定」時才說得上話。
實際發生過一次不是這樣：2026-09-02（commit 38743e2）之前 trade_unit 是
1000，只能買整張。本金 100 萬、每檔 20% ＝ 預算 20 萬，於是觀察清單
57 檔裡有 31 檔買不起——金融是唯一 5 檔全部買得起的產業，PCB、
電子零組件、網通、被動元件則是一檔都買不到。選股論述寫的是 AI 供應鏈，
帳上實際在跑的是另一個標的池。

那段紀錄不是「不好看」，是**用一台壞掉的儀器量出來的**。

它跟「把持股賣掉重來」的差別
----------------------------
賣掉會在 paper_trades.jsonl 留下 N 筆完成交易，而那個檔案是 append-only、
刻意設計成「狀態檔重建也還在」。那 N 筆的進場來自壞設定、出場是人為的
（不是停損、不是停利、也不是 max_hold_bars），但 performance() 照樣把它們
算進勝率、獲利因子與 objective 的樣本數。等於為了清掉污染而製造污染。

重置則是連同淨值曲線一起歸檔，新帳從 initial_cash 重新起算，
不會有任何一筆假交易混進樣本。

它做什麼
--------
1. 把 data/paper_*.json(l) 整批**複製**到 data/archive/paper-reset-<時間戳>/，
   逐檔記 SHA-256，寫一份 MANIFEST.json（重置理由、當下帳況、設定快照）
2. 驗完複本的雜湊之後，才移除原始檔並開一本新帳
3. 把這次重置寫進 config/paper.yaml 的 changelog

刻意不做的事
------------
- **不刪除，只歸檔。** 重置本身必須可稽核——真的刪掉的話，它在事後
  就跟「藏起難看的結果」無法區分。--reason 是必填欄位，同一個道理。
- **不動策略設定。** 要改 paper.yaml 的參數請自己改。同一個動作裡
  「改設定 ＋ 清紀錄」會讓人分不出哪個是因、哪個是果。
- **不作廢個別交易。** 那是 paper.append_void() 的事，它 append 一筆
  註銷紀錄而不改寫原行。這支程式處理的是「整段作廢」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import paper  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):   # Windows 主控台預設 cp950，中文會炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PAPER_CONFIG = ROOT / "config" / "paper.yaml"
MANIFEST_NAME = "MANIFEST.json"

# 理由太短等於沒寫。這道門檻擋的是 "test"、"reset"、"重來" 這種
# 填了跟沒填一樣的字串——理由是這次重置唯一能跟「湮滅紀錄」區分開來的東西。
MIN_REASON_CHARS = 12

# 互動確認要打的字。刻意不是 y/n：這個動作會讓一整段紀錄離開日常流程，
# 值得多花三秒鐘確認你知道自己在做什麼。
CONFIRM_WORD = "RESET"


class ChangelogNotAppendable(RuntimeError):
    """changelog 不在檔案最後面，純文字 append 不安全。"""


# --------------------------------------------------------------------------
# 路徑
# --------------------------------------------------------------------------


def archive_root() -> Path:
    """歸檔根目錄。"""
    return paper.DATA_DIR / "archive"


def live_files() -> dict[str, Path]:
    """會被這次重置帶走的檔案。

    路徑一律在呼叫當下才從 paper 模組取，不要在 import 時算好存起來：
    paper.py 是在 import 時就從 DATA_DIR 推出這幾個常數的，
    測試換掉的是模組屬性，先快取下來就會抓到真正的 data/
    （tests/test_paper.py 裡「五個路徑要一起改」那段註解講的就是這件事）。
    """
    return {
        "state": paper.STATE_FILE,
        "state_backup": paper.state_backup_path(),
        "trades": paper.TRADES_FILE,
        "equity": paper.EQUITY_FILE,
        "runs": paper.RUNS_FILE,
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def machine_name() -> str:
    try:
        return socket.gethostname().strip() or "unknown"
    except OSError:
        return "unknown"


def _rel(path: Path) -> str:
    """給人看的路徑。測試會把 DATA_DIR 指到暫存目錄，那時候算不出相對路徑。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _wrap(text: str, width: int = 34) -> list[str]:
    """把一段中英混排的文字切成好幾行，供 YAML 折疊區塊用。

    中文沒有空白可以斷，textwrap 幫不上忙，所以改成「湊到夠長就在標點後斷」。
    刻意只在標點後斷：YAML 的折疊區塊會把換行變成一個空白，
    斷在標點後那個空白讀起來像排版，斷在詞中間就是錯字。
    """
    lines: list[str] = []
    buf = ""
    for char in text:
        buf += char
        if len(buf) >= width and char in "。！？：；，、)）」』.,;:!?":
            lines.append(buf)
            buf = ""
        elif len(buf) >= width * 2:   # 一路沒有標點，硬斷也比爆一長行好
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)
    return lines or [text]


def _money(value) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


# --------------------------------------------------------------------------
# 設定與現況
# --------------------------------------------------------------------------


def load_paper_config() -> dict:
    if not PAPER_CONFIG.exists():
        return {}
    with PAPER_CONFIG.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def config_snapshot(config: dict) -> dict:
    """重置後這本新帳會用的設定。

    存進 MANIFEST 是為了讓「這段紀錄是哪組設定產生的」以後不必靠 git log
    去推——設定檔會一直改，而歸檔的紀錄不會跟著改。
    """
    return {
        key: config.get(key) or {}
        for key in ("account", "costs", "strategy", "data_guard")
    }


def incomplete_archives() -> list[Path]:
    """上一次歸檔做到一半就中斷的目錄（有資料夾、沒有 MANIFEST）。

    有這種東西存在時直接拒絕再跑：它代表上一次重置的狀態不明，
    這時候再蓋一次會讓兩次的殘骸混在一起，之後誰也說不清哪個檔案屬於哪一次。
    """
    root = archive_root()
    if not root.is_dir():
        return []
    return sorted(
        d
        for d in root.iterdir()
        if d.is_dir()
        and d.name.startswith("paper-reset-")
        and not (d / MANIFEST_NAME).exists()
    )


def summarise() -> dict:
    """重置當下的帳況。

    狀態檔壞掉也要能報得出來——那正是會想重置的場合之一，
    這時候丟例外等於把唯一的救生艇鎖起來。
    """
    curve = paper.load_equity_curve()
    summary: dict = {
        "equity_rows": len(curve),
        "run_rows": len(paper.load_runs()),
        "closed_trades": len(paper.load_trades(include_void=True)),
        "first_date": curve[0].get("trade_date") if curve else None,
        "last_date": curve[-1].get("trade_date") if curve else None,
        "final_equity": curve[-1].get("equity") if curve else None,
    }
    try:
        account = paper.load_state(0.0)
    except paper.StateCorrupted as exc:
        summary.update(
            state=f"讀不出來（{exc}）",
            open_positions=None,
            cash=None,
            owner=None,
            pending=None,
        )
    else:
        summary.update(
            state="ok",
            open_positions=sorted(account.positions),
            cash=round(account.cash, 2),
            owner=account.owner,
            pending=[order.code for order in account.pending],
        )
    return summary


def build_plan(reason: str) -> dict:
    reason = " ".join(str(reason).split())
    if len(reason) < MIN_REASON_CHARS:
        raise ValueError(
            f"--reason 至少要 {MIN_REASON_CHARS} 個字，你給了 {len(reason)} 個。\n"
            "理由是這次重置唯一能跟「湮滅紀錄」區分開來的東西：\n"
            "寫清楚是哪個設定變了、為什麼那讓舊紀錄失效。"
        )

    stale = incomplete_archives()
    if stale:
        raise RuntimeError(
            "有沒歸檔完的目錄，這次不執行：\n"
            + "\n".join(f"  {_rel(d)}" for d in stale)
            + "\n先確認那次重置的狀態（缺少 MANIFEST.json ＝ 中途失敗），"
            "確認完再把目錄改名或移走。"
        )

    present = {label: p for label, p in live_files().items() if p.exists()}
    if not present:
        raise RuntimeError("找不到任何模擬倉紀錄——這本帳還是空的，不需要重置。")

    stamp = datetime.now()
    config = load_paper_config()
    account_cfg = config.get("account") or {}
    return {
        "reset_at": stamp.isoformat(timespec="seconds"),
        "date": stamp.strftime("%Y-%m-%d"),
        "reason": reason,
        "machine": machine_name(),
        "archive_dir": archive_root() / f"paper-reset-{stamp:%Y%m%d-%H%M%S}",
        "files": present,
        "summary": summarise(),
        "initial_cash": float(
            account_cfg.get("initial_cash", paper.AccountParams.initial_cash)
        ),
        "config_after_reset": config_snapshot(config),
    }


# --------------------------------------------------------------------------
# changelog
# --------------------------------------------------------------------------


def changelog_note_lines(plan: dict) -> list[str]:
    """changelog 那則 note 的內容。空字串 = 段落分隔（折疊區塊裡會變成換行）。"""
    summary = plan["summary"]
    account = plan["config_after_reset"].get("account") or {}
    window = (
        f"{summary['first_date']} ～ {summary['last_date']}"
        if summary.get("first_date") and summary.get("last_date")
        else "（淨值曲線是空的）"
    )
    codes = summary.get("open_positions") or []
    held = f"{len(codes)} 檔（{'、'.join(codes)}）" if codes else "無"

    return [
        f"**模擬倉重置**：作廢 {window} 的紀錄。原始檔沒有刪除，",
        f"完整歸檔在 {_rel(plan['archive_dir'])}/，",
        "含逐檔 SHA-256 與 MANIFEST.json（重置理由、當下帳況、設定快照）。",
        "",
        *_wrap(f"理由：{plan['reason']}"),
        "",
        f"重置當下：完成交易 {summary['closed_trades']} 筆、未平倉 {held}，",
        f"淨值 {_money(summary.get('final_equity'))}、"
        f"淨值曲線 {summary['equity_rows']} 天。",
        f"重置後：initial_cash={_money(plan['initial_cash'])}、",
        f"trade_unit={account.get('trade_unit')}、",
        f"position_pct={account.get('position_pct')}%、",
        f"max_positions={account.get('max_positions')}。",
        "",
        "**判準（下次遇到同樣情況照這條走）：** 因**正確性錯誤**而變更設定時，",
        "該設定下產生的模擬倉紀錄作廢重起；因偏好、門檻或風險忍受度而變更，",
        "則不作廢。差別在於前者是「量錯了」，後者是「標準改了」——",
        "後者作廢紀錄就是搬門柱。",
        "",
        "這條判準要成立，唯一的證據是它必須先於結果。",
        "留意這次作廢掉的是一份**正報酬**的紀錄：如果只有難看的紀錄會被重置，",
        "那這個機制就只是一台把壞結果洗掉的機器，之後誰也不必再相信它。",
    ]


def render_changelog_entry(plan: dict, newline: str) -> str:
    lines = [f'  - date: "{plan["date"]}"', "    note: >-"]
    lines += [("      " + line if line else "") for line in changelog_note_lines(plan)]
    return newline.join(lines) + newline


def _read_config_text() -> tuple[str, str]:
    text = PAPER_CONFIG.read_bytes().decode("utf-8")
    return text, ("\r\n" if "\r\n" in text else "\n")


def check_changelog_appendable() -> None:
    """先確認 changelog 貼得上去，再去動紀錄檔。

    這個檢查刻意放在搬檔案**之前**：changelog 貼不上去是可以預先知道的事，
    等到檔案都歸檔完才發現，就只能留下一個「紀錄清了、帳沒記」的半套狀態。
    """
    if not PAPER_CONFIG.exists():
        raise ChangelogNotAppendable(f"找不到 {_rel(PAPER_CONFIG)}")

    text, _ = _read_config_text()
    keys = re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*):", text)
    if not keys or keys[-1] != "changelog":
        raise ChangelogNotAppendable(
            f"changelog 不是 {_rel(PAPER_CONFIG)} 的最後一個頂層 key"
            f"（目前最後一個是 {keys[-1] if keys else '無'}）。\n"
            "這支程式用純文字 append 的方式寫 changelog，因為 yaml.safe_load\n"
            "＋ dump 整份回去會把設定檔裡那幾百行中文註解全部吃掉。\n"
            "代價就是它只在 changelog 排最後時才安全，所以這裡不猜、不硬寫。"
        )
    if yaml.safe_load(text) is None:
        raise ChangelogNotAppendable(f"{_rel(PAPER_CONFIG)} 解析出來是空的")


def append_changelog(plan: dict) -> None:
    """把這次重置寫進 config/paper.yaml 的 changelog（純文字 append）。

    寫完會重新解析一次，確認除了 changelog 多一則之外，其他區塊一個字都沒變
    ——設定檔壞掉的話，每日排程隔天就整個停擺。
    """
    check_changelog_appendable()
    text, newline = _read_config_text()

    before = yaml.safe_load(text) or {}
    body = text if text.endswith(newline) else text + newline
    candidate = body + render_changelog_entry(plan, newline)

    try:
        after = yaml.safe_load(candidate)
    except yaml.YAMLError as exc:
        raise ChangelogNotAppendable(
            f"寫進去之後 YAML 解析不過，沒有動檔案：{exc}"
        ) from exc

    old_log = before.get("changelog") or []
    new_log = (after or {}).get("changelog") or []
    if len(new_log) != len(old_log) + 1:
        raise ChangelogNotAppendable(
            f"changelog 應該從 {len(old_log)} 則變成 {len(old_log) + 1} 則，"
            f"實際變成 {len(new_log)} 則。沒有動檔案。"
        )
    for key in ("enabled", "account", "costs", "strategy", "data_guard"):
        if (after or {}).get(key) != before.get(key):
            raise ChangelogNotAppendable(
                f"append 之後 {key} 區塊變了，這不該發生。沒有動檔案。"
            )

    tmp = PAPER_CONFIG.with_name(PAPER_CONFIG.name + ".tmp")
    tmp.write_bytes(candidate.encode("utf-8"))
    tmp.replace(PAPER_CONFIG)


# --------------------------------------------------------------------------
# 執行
# --------------------------------------------------------------------------


def render_plan(plan: dict) -> str:
    summary = plan["summary"]
    account = plan["config_after_reset"].get("account") or {}
    window = (
        f"（{summary['first_date']} ～ {summary['last_date']}）"
        if summary.get("first_date")
        else ""
    )
    out = [
        "=" * 62,
        "模擬倉重置",
        "=" * 62,
        f"理由      {plan['reason']}",
        f"機器      {plan['machine']}",
        f"歸檔到    {_rel(plan['archive_dir'])}/",
        "",
        "會被歸檔並移除的檔案:",
    ]
    for label, path in plan["files"].items():
        out.append(
            f"  {label:<14}{_rel(path):<34}{path.stat().st_size:>9,} bytes"
        )

    out += [
        "",
        "重置當下的帳況:",
        f"  狀態檔        {summary['state']}",
        f"  淨值曲線      {summary['equity_rows']} 天{window}",
        f"  執行紀錄      {summary['run_rows']} 筆",
        f"  完成交易      {summary['closed_trades']} 筆   ← 這些會離開績效統計",
        f"  未平倉        {'、'.join(summary.get('open_positions') or []) or '無'}",
        f"  待成交委託    {'、'.join(summary.get('pending') or []) or '無'}",
        f"  淨值          {_money(summary.get('final_equity'))}",
        "",
        "重置後的新帳:",
        f"  現金          {_money(plan['initial_cash'])}（來自 config/paper.yaml）",
        "  持股          無",
        "  owner         空白（下一次執行的機器自動認領）",
        f"  設定          trade_unit={account.get('trade_unit')}、"
        f"position_pct={account.get('position_pct')}%、"
        f"max_positions={account.get('max_positions')}",
        "=" * 62,
    ]
    return "\n".join(out)


def execute_plan(plan: dict) -> Path:
    """複製 → 驗雜湊 → 寫 MANIFEST → 移除原檔 → 開新帳。

    順序是刻意的：先確認複本真的落在硬碟上而且內容一致，才動原始檔。
    反過來（先搬再驗）只要中間掛掉，就會變成兩邊都沒有。
    """
    archive_dir: Path = plan["archive_dir"]
    if archive_dir.exists():
        raise FileExistsError(f"歸檔目錄已經存在，不覆蓋：{_rel(archive_dir)}")
    archive_dir.mkdir(parents=True)

    records = []
    for label, path in plan["files"].items():
        target = archive_dir / path.name
        shutil.copy2(path, target)
        source_hash, copy_hash = sha256_of(path), sha256_of(target)
        if source_hash != copy_hash:
            raise OSError(
                f"歸檔複本跟原檔對不起來，中止（原檔沒有被動過）：{path.name}"
            )
        records.append(
            {
                "label": label,
                "name": path.name,
                "bytes": target.stat().st_size,
                "sha256": copy_hash,
            }
        )

    manifest = {
        "reset_at": plan["reset_at"],
        "reason": plan["reason"],
        "machine": plan["machine"],
        "at_reset": plan["summary"],
        "initial_cash_after_reset": plan["initial_cash"],
        "config_after_reset": plan["config_after_reset"],
        "files": records,
    }
    (archive_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # MANIFEST 寫完才移除原檔：incomplete_archives() 就是靠「有目錄、沒 MANIFEST」
    # 認出中途失敗的，所以這兩步的先後也是那道保護的一部分。
    for path in plan["files"].values():
        path.unlink()

    paper.save_state(paper.Account(cash=plan["initial_cash"]))
    return archive_dir


def confirm() -> bool:
    print(
        f"\n確定要重置就輸入 {CONFIRM_WORD}（其他任何輸入都會取消）: ",
        end="",
        flush=True,
    )
    try:
        return input().strip() == CONFIRM_WORD
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="模擬倉重置：歸檔舊紀錄並重新開帳（不刪除任何東西）"
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="舊紀錄為什麼失效。必填，會寫進 MANIFEST.json 與 paper.yaml 的 changelog。",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只印出會做什麼，不動任何檔案。"
    )
    parser.add_argument(
        "--yes", action="store_true", help="跳過互動確認（給腳本用，請小心）。"
    )
    parser.add_argument(
        "--skip-changelog",
        action="store_true",
        help="不要動 config/paper.yaml；changelog 那則會印出來讓你自己貼。",
    )
    args = parser.parse_args(argv)

    try:
        plan = build_plan(args.reason)
    except (ValueError, RuntimeError) as exc:
        print(f"\n✗ {exc}\n")
        return 1

    print(render_plan(plan))

    if not args.skip_changelog:
        try:
            check_changelog_appendable()
        except ChangelogNotAppendable as exc:
            print(f"\n✗ changelog 貼不上去，所以這次不執行（檔案都還沒動）：\n{exc}")
            print("\n確認 config/paper.yaml 之後再跑一次，或加 --skip-changelog 自己補。\n")
            return 1

    if args.dry_run:
        print("\n--dry-run：沒有動任何檔案。")
        print("下面是會寫進 config/paper.yaml 的 changelog：\n")
        print(render_changelog_entry(plan, "\n"))
        return 0

    if not args.yes and not confirm():
        print("\n已取消，沒有動任何檔案。\n")
        return 1

    try:
        archive_dir = execute_plan(plan)
    except (OSError, FileExistsError) as exc:
        print(f"\n✗ 歸檔失敗：{exc}\n")
        return 1

    print(f"\n✓ 已歸檔到 {_rel(archive_dir)}/")
    print(f"✓ 新帳已開：現金 {_money(plan['initial_cash'])}、無持股")

    if args.skip_changelog:
        print("\n--skip-changelog：下面這則請自己貼到 config/paper.yaml 的 changelog：\n")
        print(render_changelog_entry(plan, "\n"))
    else:
        try:
            append_changelog(plan)
        except ChangelogNotAppendable as exc:
            # 檔案已經歸檔完了，不因為設定檔寫不進去就回捲——
            # 印出來讓人自己貼，比留下一個半套狀態好。
            print(f"\n⚠️ changelog 寫不進去（紀錄已經歸檔完成）：{exc}")
            print("請自己把下面這則貼到 config/paper.yaml 的 changelog：\n")
            print(render_changelog_entry(plan, "\n"))
            return 1
        print(f"✓ 已寫進 {_rel(PAPER_CONFIG)} 的 changelog")

    print(
        "\n下一步：確認 config/paper.yaml 的參數就是你要量的那組，然後讓排程照常跑。"
        "\n新的紀錄從下一個交易日開始累積。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
