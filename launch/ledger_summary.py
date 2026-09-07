"""把模擬倉帳本的現況印成三行給人看。

更新腳本（launch/update.sh 與 launch/win/update.ps1）跑完 git pull 之後
呼叫這支。單獨拉成一個檔案的理由是「兩邊要說一樣的話」——
原本 macOS 那支是 shell 裡的 here-doc、Windows 那支是 PowerShell 字串拼接，
同一段邏輯寫兩次，改一邊忘了另一邊時不會有任何人發現。

不 import src/ 底下的東西：更新腳本可能在套件還沒補齊的狀態下執行
（requirements.txt 剛好在這次更新裡變了，就是這種狀態），
這裡只用標準函式庫，任何情況下都印得出來。
"""

import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    path = os.path.join(ROOT, "data", "paper_state.json")
    if not os.path.exists(path):
        print("   還沒有模擬倉帳本（data/paper_state.json 不存在）。")
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as exc:
        # 壞掉的帳本要講出來，不要當成「沒有帳本」——後者會讓人以為
        # 是還沒開始跑，實際上是檔案被寫壞了。
        print(f"   [!!] 帳本讀不開：{exc}")
        return 0

    positions = state.get("positions") or {}
    print(f"   最後成交日：{state.get('last_date') or '（無）'}")
    print(f"   現金：{state.get('cash', 0):,.0f}")
    held = "、".join(positions) if positions else "（無）"
    print(f"   持股 {len(positions)} 檔：{held}")

    # owner 是「這本帳是誰在寫的」（見 docs/HANDOFF.md 的「兩本帳」）。
    # 跟本機主機名不同代表這台是唯讀機：帳本用看的就好，不要在這裡開排程。
    owner = state.get("owner")
    here = socket.gethostname()
    if owner and owner != here:
        print(f"   帳本由 {owner} 記帳，這台（{here}）是唯讀機——看就好，別在這裡開排程。")
    elif owner:
        print(f"   這台（{here}）就是決策機，帳本在這裡寫。")

    equity_path = os.path.join(ROOT, "data", "paper_equity.jsonl")
    if os.path.exists(equity_path):
        with open(equity_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        if lines:
            try:
                last = json.loads(lines[-1])
            except ValueError:
                return 0
            print(f"   最新淨值：{last.get('equity', 0):,.0f}（{last.get('trade_date')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
