#!/bin/bash
# ============================================================
# 更新到最新版本 —— 在 Finder 裡雙擊這個檔案就會執行
# ============================================================
# 做三件事，順序不能換：
#   1. git pull 拉最新的程式與帳本
#   2. 重新指向桌面捷徑（專案搬過家、或捷徑被複製成一份死的，都靠這步救回來）
#   3. 把拉下來的帳本印出來給你看
#
# 為什麼要獨立一支：安裝.command 會裝套件、問你要不要補歷史和開排程，
# 那是「第一次」在做的事；而且它遇到已經存在的桌面捷徑會直接略過——
# 捷徑已經失效時，重跑安裝救不回來。日常更新要的是快、而且不問問題。
# ------------------------------------------------------------

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd -P)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

step()  { echo ""; echo "${BOLD}── $1 ──${RESET}"; }
ok()    { echo "${GREEN}✅${RESET} $1"; }
warn()  { echo "${YELLOW}⚠️ ${RESET} $1"; }
fail()  { echo "${RED}❌${RESET} $1"; }
# 雙擊執行時視窗跑完就關，看不到結果，所以要停下來等一下。
# 從工具箱叫進來時外面已經有「完成，可以關掉這個視窗」了，別再問一次——
# 那時用 --no-pause。
PAUSE=1
[[ "${1:-}" == "--no-pause" ]] && PAUSE=0
bye() {
  if [[ "$PAUSE" == "1" ]]; then echo ""; read -r -p "按 Enter 關閉這個視窗…"; fi
  exit "${1:-0}"
}

echo "${BOLD}台股投資決策輔助系統 · 更新${RESET}"
echo "${DIM}專案位置：$ROOT${RESET}"

if [[ ! -f "$ROOT/webapp/app.py" ]]; then
  fail "這個資料夾看起來不是專案（找不到 webapp/app.py）。"
  echo "   請確認 launch/更新.command 還放在專案資料夾裡。"
  bye 1
fi

if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  fail "這份專案不是用 git clone 下來的，沒有辦法更新。"
  echo "   要更新的話，重新 clone 一份："
  echo "   ${DIM}git clone https://github.com/ShibaKen223/Investment-Project.git${RESET}"
  bye 1
fi

BEFORE="$(git -C "$ROOT" rev-parse HEAD)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"

# ------------------------------------------------------------
step "1/3　拉最新版本"
# ------------------------------------------------------------
if [[ "$BRANCH" != "main" ]]; then
  warn "目前在分支 '$BRANCH'，不是 main。"
  echo "   拉下來的會是這條分支的內容，決策機推上去的帳本不一定在這裡。"
fi

# 有沒有還沒提交的變更。這件事一定要在 pull 之前講清楚，理由是帳本：
# data/paper_*.json(l) 在 .gitattributes 裡標了 -merge（見 docs/HANDOFF.md
# 的「兩本帳」），所以本機改過又沒提交時，git 會直接拒絕 pull——
# 那不是壞事，是設計，但錯誤訊息本身看不出來這件事。
# --untracked-files=no 是刻意的：擋 pull 的是「改過的追蹤檔」，
# 尤其是帳本。桌面上隨手丟進專案資料夾的檔案不該讓更新停下來，
# 真的會被覆蓋時 git pull 自己會擋，訊息也講得比這裡清楚。
DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY" ]]; then
  warn "本機有還沒提交的變更："
  echo "$DIRTY" | sed 's/^/     /'
  echo ""
  if echo "$DIRTY" | grep -qE '^.. *"?data/(paper_|signals\.jsonl|reports|history)'; then
    echo "   ${BOLD}其中有帳本或報告${RESET}——這台機器自己跑出來的紀錄還沒推上去。"
    echo "   直接 pull 會把它蓋掉。先在專案資料夾裡執行："
    echo "     ${DIM}git add data && git commit -m '本機帳本' && git push${RESET}"
  else
    echo "   先處理掉（commit 或 git checkout -- <檔案>）再更新。"
  fi
  bye 1
fi

# --ff-only 是刻意的，跟 launch/win/daily_run.ps1 同一個理由：
# 能快轉就快轉，已經分岔就讓它失敗，而不是自動 merge 出第三本帳。
echo "${DIM}git pull --ff-only…${RESET}"
if ! git -C "$ROOT" pull --ff-only; then
  echo ""
  fail "拉不下來——本機與遠端已經分岔了。"
  echo "   這通常表示兩台機器各自跑過排程，各記了一本帳。"
  echo "   要自己決定留哪一邊，做法見 docs/HANDOFF.md 的「兩本帳」那一節。"
  bye 1
fi

AFTER="$(git -C "$ROOT" rev-parse HEAD)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  ok "已經是最新版本了（$(git -C "$ROOT" log -1 --format='%h %s' | cut -c1-60)）"
else
  ok "更新完成，這次拉進來的："
  git -C "$ROOT" log --oneline "$BEFORE..$AFTER" | sed 's/^/     /'

  # 套件清單變了就要講。少了這句，新版程式 import 不到東西時，
  # 使用者看到的會是儀表板打不開，而不是「你該重裝套件了」。
  if ! git -C "$ROOT" diff --quiet "$BEFORE" "$AFTER" -- requirements.txt; then
    echo ""
    warn "requirements.txt 有變動，請執行一次："
    echo "     ${DIM}python3 -m pip install -r requirements.txt${RESET}"
  fi
fi

# ------------------------------------------------------------
step "2/3　重新指向桌面捷徑"
# ------------------------------------------------------------
# 捷徑是符號連結，指向專案裡的 .app。所以只要專案沒搬家，
# pull 完捷徑自動就是新版——但「專案搬過家」和「當初是用複製而不是連結」
# 這兩種情況會讓捷徑指到錯的地方，而且症狀是它在錯的位置建 data/，
# 你在專案裡怎麼找都找不到。這一步就是把它重新指回來。
DESKTOP="$HOME/Desktop"
if [[ -d "$DESKTOP" ]]; then
  for app in "投資儀表板" "投資工具箱"; do
    src="$ROOT/launch/$app.app"
    dst="$DESKTOP/$app.app"
    if [[ ! -d "$src" ]]; then
      warn "launch/$app.app 不存在，略過"
      continue
    fi
    chmod +x "$src/Contents/MacOS/launcher" 2>/dev/null

    if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
      ok "$app：捷徑正確，內容已隨這次更新變成新版"
      continue
    fi

    if [[ -L "$dst" ]]; then
      echo "   ${DIM}$app：捷徑指向 $(readlink "$dst")，重新指回專案${RESET}"
    elif [[ -e "$dst" ]]; then
      echo "   ${DIM}$app：桌面上那份是複製的（不會跟著更新），換成連結${RESET}"
    fi
    rm -rf "$dst"
    if ln -s "$src" "$dst"; then
      ok "$app：捷徑已重新指向 $src"
    else
      fail "$app：建立捷徑失敗，請自己從 launch/ 拉一份替身到桌面。"
    fi
  done
else
  warn "找不到桌面資料夾，捷徑請自己從 launch/ 拉出來。"
fi

# ------------------------------------------------------------
step "3/3　現在的帳本"
# ------------------------------------------------------------
PY="$(command -v python3 || true)"
if [[ -n "$PY" ]]; then
  "$PY" launch/ledger_summary.py
else
  warn "找不到 python3，跳過帳本摘要。"
fi

echo ""
echo "${GREEN}${BOLD}更新完成。${RESET}"
echo "${DIM}雙擊桌面的「投資儀表板」就會看到新版。${RESET}"
bye 0
