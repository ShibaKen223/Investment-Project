#!/bin/bash
# ============================================================
# 安裝／移除每日自動更新排程（macOS LaunchAgent）
# ============================================================
#   安裝:  bash launch/install_daily.sh
#   移除:  bash launch/install_daily.sh --uninstall
#   查看:  bash launch/install_daily.sh --status
#
# 排程內容：週一到週五 15:00 自動抓收盤行情、產生當日報告，
# 若有部位觸發停損／停利且已設定 config/mail.yaml，就寄一封提醒信。
# 台股 13:30 收盤，資料源約 14:00–15:00 更新，所以排在 15:00。
#
# 這只是「使用者層級」的排程，不需要管理員權限，也不會動到系統設定。
# ------------------------------------------------------------
set -euo pipefail

LABEL="local.investment.daily"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HOUR="${INVEST_HOUR:-15}"
MINUTE="${INVEST_MINUTE:-0}"

case "${1:-install}" in
  --status)
    if launchctl list | grep -q "$LABEL"; then
      echo "✅ 排程已安裝並載入："
      launchctl list | grep "$LABEL"
      echo ""
      echo "設定檔：$PLIST"
      echo "執行紀錄：$ROOT/data/daily.log"
    else
      echo "❌ 排程尚未安裝。執行 bash launch/install_daily.sh 來安裝。"
    fi
    exit 0
    ;;
  --uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✅ 已移除每日排程。（儀表板本身不受影響，仍可正常使用）"
    exit 0
    ;;
esac

PY="$(command -v python3 || echo /usr/bin/python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${ROOT}/src/main.py</string>
    <string>--quiet</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${ROOT}</string>

  <!-- 週一到週五 ${HOUR}:$(printf '%02d' "$MINUTE") -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>${ROOT}/data/daily.log</string>
  <key>StandardErrorPath</key>
  <string>${ROOT}/data/daily.log</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✅ 已安裝每日排程"
echo ""
echo "   時間      週一至週五 ${HOUR}:$(printf '%02d' "$MINUTE")"
echo "   動作      抓收盤行情 → 產生當日報告 → 有觸發訊號才寄信"
echo "   設定檔    $PLIST"
echo "   執行紀錄  $ROOT/data/daily.log"
echo ""
echo "想改時間：INVEST_HOUR=16 bash launch/install_daily.sh"
echo "想要移除：bash launch/install_daily.sh --uninstall"
echo ""
echo "註：Mac 在排程時間處於關機狀態就會略過那天；"
echo "    睡眠狀態則會在喚醒後補跑。"
