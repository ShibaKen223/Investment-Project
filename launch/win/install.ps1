# ============================================================
# 一次把環境設定好（Windows）—— 雙擊 安裝.bat 就會執行這支
# ============================================================
# 做四件事：裝套件 -> 驗證能跑 -> 把捷徑放到桌面 -> 問你要不要補歷史和開排程。
# 每一步都會問過你，不會自作主張。全程不需要系統管理員權限。
# 對應 macOS 的 launch\安裝.command。
# ------------------------------------------------------------

. (Join-Path $PSScriptRoot '_lib.ps1')

function Step { param([string]$T) Write-Host ""; Write-Host "-- $T --" -ForegroundColor White }
function OK   { param([string]$T) Write-Host "[OK] $T" -ForegroundColor Green }
function Warn { param([string]$T) Write-Host "[!!] $T" -ForegroundColor Yellow }
function Fail { param([string]$T) Write-Host "[XX] $T" -ForegroundColor Red }

function Ask {
  # 回答 y 才回傳 $true。直接按 Enter 視為否。
  param([string]$Q)
  $r = Read-Host "$Q [y/N]"
  return ($r -eq 'y' -or $r -eq 'Y')
}

$root = Get-ProjectRoot
Write-Host "台股投資決策輔助系統 · 安裝（Windows）" -ForegroundColor White
Write-Host "專案位置：$root" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $root 'webapp\app.py'))) {
  Fail "這個資料夾看起來不是專案（找不到 webapp\app.py）。"
  Write-Host "   請確認 launch\win\安裝.bat 還放在專案資料夾裡。"
  Read-Host "按 Enter 關閉"
  exit 1
}

# ------------------------------------------------------------
Step "1/4  檢查 Python"
# ------------------------------------------------------------
$py = Find-Python
if (-not $py) {
  Fail "找不到可用的 Python 3。"
  Write-Host ""
  Write-Host "   安裝方式（擇一）："
  Write-Host "   1. python.org/downloads 下載，安裝時勾選「Add python.exe to PATH」"
  Write-Host "   2. 或執行：winget install Python.Python.3.12"
  Write-Host ""
  Write-Host "   註：Windows 內建的 python 指令常常是 Microsoft Store 的空殼"
  Write-Host "       （跑起來直接 exit 49），那個不算數。"
  Write-Host ""
  Read-Host "按 Enter 關閉"
  exit 1
}
$pyVer = (& $py --version 2>&1 | Out-String).Trim()
$pyPath = (Get-Command $py).Source
OK "$pyVer（$pyPath）"

# ------------------------------------------------------------
Step "2/4  安裝套件"
# ------------------------------------------------------------
Set-Location $root
& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  Fail "套件安裝失敗，請把上面的錯誤訊息記下來。"
  Read-Host "按 Enter 關閉"
  exit 1
}
OK "套件安裝完成"

Write-Host ""
Write-Host "跑一次測試確認裝好了…" -ForegroundColor DarkGray
& $py tests\run_all.py | Out-Null
if ($LASTEXITCODE -eq 0) {
  OK "測試全部通過"
} else {
  Warn "測試沒過。系統可能還是能用，但建議執行 py tests\run_all.py 看看原因。"
}

# ------------------------------------------------------------
Step "3/4  把捷徑放到桌面"
# ------------------------------------------------------------
# Windows 的捷徑（.lnk）記的是絕對路徑，所以複製到桌面不會有
# macOS 那種「複製 vs 符號連結」的坑。但專案搬家之後捷徑會失效，
# 屆時重跑這支腳本即可——_lib.ps1 的 Assert-ProjectRoot 會講清楚。
$desktop = [Environment]::GetFolderPath('Desktop')
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function New-Shortcut {
  param(
    [string]$Name, [string]$Script, [string]$Desc,
    [string]$Icon, [string]$WindowStyle
  )
  $target = Join-Path $PSScriptRoot $Script
  if (-not (Test-Path $target)) { Warn "找不到 $Script，略過"; return }
  $lnkPath = Join-Path $desktop "$Name.lnk"
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut($lnkPath)
  $lnk.TargetPath = $psExe
  $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle $WindowStyle -File `"$target`""
  $lnk.WorkingDirectory = $root
  $lnk.Description = $Desc
  $lnk.IconLocation = $Icon
  $lnk.Save()
  OK "$Name 已放到桌面"
}

if (Test-Path $desktop) {
  # 儀表板：不需要主控台，隱藏視窗直接開瀏覽器。
  New-Shortcut -Name '投資儀表板' -Script 'dashboard.ps1' `
    -Desc '開啟投資儀表板網頁介面' `
    -Icon "$env:SystemRoot\System32\shell32.dll,13" -WindowStyle 'Hidden'
  # 工具箱：選單要看得到，用一般視窗。
  New-Shortcut -Name '投資工具箱' -Script 'toolbox.ps1' `
    -Desc '補歷史、回測、研究筆記、排程' `
    -Icon "$env:SystemRoot\System32\shell32.dll,21" -WindowStyle 'Normal'
  Write-Host ""
  Write-Host "   投資儀表板 = 雙擊直接開網頁介面（每天用這個）" -ForegroundColor DarkGray
  Write-Host "   投資工具箱 = 雙擊跳出選單：補歷史、回測、研究筆記、排程" -ForegroundColor DarkGray
} else {
  Warn "找不到桌面資料夾，捷徑請自己從 launch\win\ 建立。"
}

# ------------------------------------------------------------
Step "4/4  選用設定"
# ------------------------------------------------------------
Write-Host ""
Write-Host "補歷史日 K" -ForegroundColor White
Write-Host "  波段策略和研究筆記需要歷史資料才能算。"
Write-Host "  每檔每月一次請求，預設 6 檔併發，通常一到三分鐘。"
if (Ask "現在補嗎？（也可以之後從工具箱補）") {
  & $py src\history.py --months 24
  Write-Host ""
  & $py src\history.py --status
} else {
  Write-Host "   略過。之後可從「投資工具箱 -> 補歷史日 K」執行。" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "每日自動更新" -ForegroundColor White
Write-Host "  週一至週五 15:00 自動抓收盤行情、產生報告。"
if (Ask "要安裝嗎？") {
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_daily.ps1')
} else {
  Write-Host "   略過。之後可從「投資工具箱 -> 安裝每日自動更新」執行。" -ForegroundColor DarkGray
}

# ------------------------------------------------------------
Write-Host ""
Write-Host "安裝完成。" -ForegroundColor Green
Write-Host ""
Write-Host "接下來："
Write-Host "  1. 雙擊桌面的「投資儀表板」"
Write-Host "  2. config\positions.yaml 最上面的 source 決定監控誰"
Write-Host "     （目前是 engine：看程式交易引擎的模擬部位）"
Write-Host "  3. 「研究」分頁可以看每檔的價格分析、產業背景和名詞辭典"
Write-Host ""
Read-Host "按 Enter 關閉這個視窗"
