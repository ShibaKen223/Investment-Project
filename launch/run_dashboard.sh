#!/bin/bash
# 實際啟動 Flask 儀表板伺服器。
#
# 為什麼獨立成一支檔案、要在終端機裡執行：
# launch/投資儀表板.app 與 launch/投資工具箱.app 都沒有蘋果的簽章，
# Finder 直接雙擊執行時，macOS 會擋掉寫入 ~/Documents 底下檔案的動作
# （保護「文件」資料夾的隱私機制），而且是那種「完全不問你、直接擋」
# 的擋法——系統設定的「檔案與檔案夾」清單裡也找不到這個 app、沒有地方
# 能開權限。終端機本身早就被授權過，所以兩個 .app 都改成請終端機代勞
# 執行這支腳本，繞開這個限制。
#
# 也可以直接雙擊這支檔案，或在終端機執行：bash launch/run_dashboard.sh

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# 這台是唯讀機：模擬倉的決策只在另一台機器跑，這裡開儀表板前
# 先拉一次最新結果，才不會看到還沒同步過來的舊資料。
# 拉不到（沒網路、還沒設定遠端）不該擋住儀表板開啟，所以失敗就跳過。
git pull --ff-only 2>/dev/null || true

URL="http://127.0.0.1:5173/"
LOG="$ROOT/data/app.log"
mkdir -p "$ROOT/data"

alive() { curl -s -o /dev/null --max-time 2 "$URL"; }

if alive; then
  echo "儀表板已經在執行了，直接開瀏覽器。"
  open "$URL"
  exit 0
fi

PY="$(command -v python3 || echo /usr/bin/python3)"
MARKER="--- $(date '+%Y-%m-%d %H:%M:%S') 啟動 ---"
echo "$MARKER" >> "$LOG"

echo "啟動投資儀表板…"
"$PY" webapp/app.py 2>&1 | tee -a "$LOG" &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if alive; then
    open "$URL"
    echo ""
    echo "關閉這個視窗即可結束伺服器。"
    wait "$SERVER_PID"
    exit 0
  fi
  sleep 0.25
done

echo ""
echo "啟動失敗。常見原因："
echo "  - 缺套件：pip3 install -r requirements.txt"
echo "  - 已經在跑但沒回應：pkill -f 'webapp/app.py'，再重跑一次"
echo ""
kill "$SERVER_PID" 2>/dev/null
echo "按 Enter 關閉這個視窗…"
read -r
