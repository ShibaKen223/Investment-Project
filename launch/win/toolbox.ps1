# 投資工具箱（Windows）—— 雙擊後跳出選單，不用記任何指令。
#
# 設計原則：長工作（補歷史、回測）就在這個視窗裡跑，讓你看得到進度，
# 而不是視窗一閃就沒了、你不知道它到底在幹嘛。
# 對應 macOS 的 launch\投資工具箱.app。

. (Join-Path $PSScriptRoot '_lib.ps1')

$root = Get-ProjectRoot
Assert-ProjectRoot $root
$py = Get-PythonOrExplain
Set-Location $root

function Invoke-Step {
  param([string]$Title, [scriptblock]$Body)
  Write-Host ""
  Write-Host "-- $Title --" -ForegroundColor Cyan
  Write-Host ""
  & $Body
  Write-Host ""
  Write-Host "-- 完成 --" -ForegroundColor Green
  Read-Host "按 Enter 回到選單"
}

while ($true) {
  Clear-Host
  Write-Host "=====================================" -ForegroundColor Cyan
  Write-Host "  投資工具箱" -ForegroundColor Cyan
  Write-Host "=====================================" -ForegroundColor Cyan
  Write-Host ""
  Write-Host "  1  開啟儀表板"
  Write-Host "  2  更新今日行情與報告"
  Write-Host "  3  產生研究筆記"
  Write-Host "  4  補歷史日 K（第一次要跑，很久）"
  Write-Host "  5  查看歷史資料狀態（有沒有缺月份）"
  Write-Host "  6  補歷史資料的破洞"
  Write-Host "  7  跑回測"
  Write-Host "  8  跑測試（確認環境正常）"
  Write-Host ""
  Write-Host "  9  安裝每日自動更新"
  Write-Host " 10  移除每日自動更新"
  Write-Host " 11  查看排程狀態"
  Write-Host " 12  重新安裝套件"
  Write-Host ""
  Write-Host "  0  離開"
  Write-Host ""
  $choice = Read-Host "要做什麼？輸入編號"

  switch ($choice) {
    '1' {
      Write-Host ""
      Write-Host "啟動中…" -ForegroundColor Cyan
      if (Open-Dashboard -Root $root -Py $py) {
        Write-Host "[OK] 儀表板已開啟：$script:DashboardUrl" -ForegroundColor Green
      }
      Start-Sleep -Seconds 2
    }
    '2'  { Invoke-Step '更新今日行情與報告' { & $py src\main.py } }
    '3'  { Invoke-Step '產生研究筆記'       { & $py src\research.py } }
    '4'  { Invoke-Step '補歷史日 K'         { & $py src\history.py --months 24 } }
    '5'  { Invoke-Step '歷史資料狀態'       { & $py src\history.py --status } }
    '6'  { Invoke-Step '補破洞'             { & $py src\history.py --fill-gaps } }
    '7'  { Invoke-Step '回測'               { & $py src\backtest.py } }
    '8'  { Invoke-Step '測試'               { & $py tests\run_all.py } }
    '9'  { Invoke-Step '安裝每日自動更新'   { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_daily.ps1') } }
    '10' { Invoke-Step '移除每日自動更新'   { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_daily.ps1') -Uninstall } }
    '11' { Invoke-Step '排程狀態'           { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_daily.ps1') -Status } }
    '12' { Invoke-Step '重新安裝套件'       { & $py -m pip install -r requirements.txt } }
    '0'  { exit 0 }
    default {
      Write-Host "沒有這個編號。" -ForegroundColor Yellow
      Start-Sleep -Seconds 1
    }
  }
}
