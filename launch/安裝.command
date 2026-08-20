#!/bin/bash
# ============================================================
# 一次把環境設定好 —— 在 Finder 裡雙擊這個檔案就會執行
# ============================================================
# 做四件事：裝套件 → 驗證能跑 → 把捷徑放到桌面 → 問你要不要補歷史和開排程。
# 每一步都會問過你，不會自作主張。全程不需要管理員權限。
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

ask() {
  # ask "問題" → 回答 y 才回傳 0。直接按 Enter 視為 n。
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]]
}

echo "${BOLD}台股投資決策輔助系統 · 安裝${RESET}"
echo "${DIM}專案位置：$ROOT${RESET}"

# ------------------------------------------------------------
step "1/4　檢查 Python"
# ------------------------------------------------------------
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  fail "找不到 python3。"
  echo "   macOS 可以裝 Xcode 命令列工具：在終端機執行 xcode-select --install"
  echo ""
  read -r -p "按 Enter 關閉…"
  exit 1
fi
ok "$($PY --version) （$PY）"

# ------------------------------------------------------------
step "2/4　安裝套件"
# ------------------------------------------------------------
if "$PY" -m pip install -r requirements.txt; then
  ok "套件安裝完成"
else
  fail "套件安裝失敗，請把上面的錯誤訊息記下來。"
  echo ""
  read -r -p "按 Enter 關閉…"
  exit 1
fi

echo ""
echo "${DIM}跑一次測試確認裝好了…${RESET}"
if "$PY" tests/test_signals.py >/dev/null 2>&1 \
   && "$PY" tests/test_store.py >/dev/null 2>&1; then
  ok "核心測試通過"
else
  warn "測試沒過。系統可能還是能用，但建議在終端機執行 python3 tests/test_signals.py 看看原因。"
fi

# ------------------------------------------------------------
step "3/4　把捷徑放到桌面"
# ------------------------------------------------------------
DESKTOP="$HOME/Desktop"
if [[ -d "$DESKTOP" ]]; then
  for app in "投資儀表板" "投資工具箱"; do
    src="$ROOT/launch/$app.app"
    dst="$DESKTOP/$app.app"
    [[ -d "$src" ]] || continue
    chmod +x "$src/Contents/MacOS/launcher" 2>/dev/null
    if [[ -e "$dst" || -L "$dst" ]]; then
      echo "   $app.app 已在桌面上，略過"
    else
      ln -s "$src" "$dst" && ok "$app.app 已放到桌面"
    fi
  done
  echo ""
  echo "   ${DIM}投資儀表板${RESET} = 雙擊直接開網頁介面（每天用這個）"
  echo "   ${DIM}投資工具箱${RESET} = 雙擊跳出選單：補歷史、回測、研究筆記、排程"
else
  warn "找不到桌面資料夾，捷徑請自己從 launch/ 拉出來。"
fi

# ------------------------------------------------------------
step "4/4　選用設定"
# ------------------------------------------------------------

echo ""
echo "${BOLD}補歷史日 K${RESET}"
echo "  波段策略和研究筆記需要歷史資料才能算。"
echo "  每檔每月一次請求、間隔 3 秒，5 檔 × 24 個月大約要 6 分鐘。"
if ask "現在補嗎？（也可以之後從工具箱補）"; then
  "$PY" src/history.py --months 24
  echo ""
  "$PY" src/history.py --status
else
  echo "   ${DIM}略過。之後可從「投資工具箱 → 補歷史日 K」執行。${RESET}"
fi

echo ""
echo "${BOLD}每日自動更新${RESET}"
echo "  週一至週五 15:00 自動抓收盤行情、產生報告。"
if ask "要安裝嗎？"; then
  bash launch/install_daily.sh
else
  echo "   ${DIM}略過。之後可從「投資工具箱 → 安裝每日自動更新」執行。${RESET}"
fi

# ------------------------------------------------------------
echo ""
echo "${GREEN}${BOLD}安裝完成。${RESET}"
echo ""
echo "接下來："
echo "  1. 雙擊桌面的「投資儀表板」"
echo "  2. 在「持股異動」把範例持股（006208 / 2412 / 2330）換成你自己的"
echo "  3. 「研究」分頁可以看每檔的價格分析、產業背景和名詞辭典"
echo ""
read -r -p "按 Enter 關閉這個視窗…"
