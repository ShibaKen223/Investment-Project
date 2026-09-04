# ============================================================
# 安裝／移除每日自動更新排程（Windows 工作排程器）
# ============================================================
#   安裝:  powershell -ExecutionPolicy Bypass -File launch\win\install_daily.ps1
#   移除:  ... install_daily.ps1 -Uninstall
#   查看:  ... install_daily.ps1 -Status
#
# 排程內容：週一到週五 15:00 自動抓收盤行情、產生當日報告，
# 若有部位觸發停損／停利且已設定 config\mail.yaml，就寄一封提醒信。
# 台股 13:30 收盤，資料源約 14:00-15:00 更新，所以排在 15:00。
# 17:30 會再跑一次當「重試」：資料源偶爾 15:00 還沒發布當天資料，
# 主跑那次會空轉，傍晚這次就補得回來（已跑過的日子會直接跳過）。
#
# 這是「目前使用者」層級的排程，不需要系統管理員權限。
# 對應 macOS 的 launch\install_daily.sh（LaunchAgent）。
# ------------------------------------------------------------

param(
  [switch]$Uninstall,
  [switch]$Status,
  [int]$Hour = 15,
  [int]$Minute = 0,
  # 傍晚的第二次執行（重試）。資料源偶爾在主跑時間還沒發布當天收盤資料
  # （2026-09-03 實際發生：15:01 執行時證交所還沒出資料，該次空轉），
  # 傍晚再跑一次就補得回來。重跑是安全的：同一天已處理過會直接跳過，
  # 提醒信也有去重（src/notify.py），不會寄兩封。設 -RetryHour -1 停用。
  [int]$RetryHour = 17,
  [int]$RetryMinute = 30
)

. (Join-Path $PSScriptRoot '_lib.ps1')

# 工作排程器的名稱刻意用 ASCII。中文名稱在 schtasks 的主控台輸出裡
# 會因為 cp950/GBK 變成亂碼，之後想手動查反而找不到。
$TaskName = 'InvestmentDailyUpdate'

$root = Get-ProjectRoot
Assert-ProjectRoot $root
$logPath = Join-Path $root 'data\daily.log'

function Get-Task {
  return (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
}

# ------------------------------------------------------------
if ($Status) {
  $task = Get-Task
  if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "[OK] 排程已安裝" -ForegroundColor Green
    Write-Host ""
    Write-Host "   名稱      $TaskName"
    Write-Host "   狀態      $($task.State)"
    Write-Host "   上次執行  $($info.LastRunTime)（結果碼 $($info.LastTaskResult)）"
    Write-Host "   下次執行  $($info.NextRunTime)"
    Write-Host "   執行紀錄  $logPath"
  } else {
    Write-Host "[--] 排程尚未安裝。" -ForegroundColor Yellow
    Write-Host "     執行 launch\win\install_daily.ps1 來安裝。"
  }
  exit 0
}

# ------------------------------------------------------------
if ($Uninstall) {
  if (Get-Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] 已移除每日排程。" -ForegroundColor Green
    Write-Host "     （儀表板本身不受影響，仍可正常使用）"
  } else {
    Write-Host "[--] 排程本來就不存在，不用移除。"
  }
  exit 0
}

# ------------------------------------------------------------
# 安裝
# ------------------------------------------------------------
$py = Get-PythonOrExplain

# 實際執行的是 daily_run.ps1，不是 python 本身——它負責把輸出接進紀錄檔，
# 並處理 PYTHONIOENCODING（排程沒有主控台，中文輸出不設就 UnicodeEncodeError）。
$runner = Join-Path $PSScriptRoot 'daily_run.ps1'
if (-not (Test-Path $runner)) {
  Write-Host "[XX] 找不到 daily_run.ps1，無法安裝排程。" -ForegroundColor Red
  exit 1
}

# powershell.exe 用絕對路徑。排程執行時的 PATH 跟互動式登入不一樣，
# 靠 PATH 找檔名有機會找不到——而且失敗是安靜的，
# 你要好幾天後才會發現報告根本沒更新。
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

$action = New-ScheduledTaskAction -Execute $psExe `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`"" `
            -WorkingDirectory $root

$at = (Get-Date -Hour $Hour -Minute $Minute -Second 0)
$triggers = @(
  New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $at
)
if ($RetryHour -ge 0) {
  $retryAt = (Get-Date -Hour $RetryHour -Minute $RetryMinute -Second 0)
  $triggers += New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $retryAt
}

# StartWhenAvailable：排程時間電腦剛好關機／睡著的話，開機後補跑一次。
# 沒有這個的話那天就整天沒有報告，而且不會有任何提示。
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
              -MultipleInstances IgnoreNew

if (Get-Task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
  -Settings $settings `
  -Description '台股投資決策輔助系統：每日抓收盤行情並產生報告' | Out-Null

$hhmm = '{0:00}:{1:00}' -f $Hour, $Minute
if ($RetryHour -ge 0) {
  $hhmm += '、{0:00}:{1:00}（重試）' -f $RetryHour, $RetryMinute
}
Write-Host "[OK] 已安裝每日排程" -ForegroundColor Green
Write-Host ""
Write-Host "   時間      週一至週五 $hhmm"
Write-Host "   動作      抓收盤行情 -> 產生當日報告 -> 有觸發訊號才寄信"
Write-Host "   名稱      $TaskName（可在「工作排程器」裡看到）"
Write-Host "   執行紀錄  $logPath"
Write-Host ""
Write-Host "想改時間：install_daily.ps1 -Hour 16（重試時間 -RetryHour 18；-RetryHour -1 停用重試）"
Write-Host "想要移除：install_daily.ps1 -Uninstall"
Write-Host "查看狀態：install_daily.ps1 -Status"
Write-Host ""
Write-Host "註：電腦在排程時間關機的話，開機後會補跑當天那一次。"
