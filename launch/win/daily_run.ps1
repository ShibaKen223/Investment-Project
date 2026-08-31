# 每日自動更新的執行包裝（由工作排程器呼叫，不需要手動執行）
#
# 為什麼不讓排程直接跑 python：需要把輸出留下來。排程是在背景跑的，
# 失敗時沒有任何視窗會告訴你，只能靠紀錄檔——而「安靜地不做事」
# 正是這個專案最不想要的失敗方式。
#
# 為什麼不用 cmd.exe /c "... >> log 2>&1" 這種寫法（macOS 版等同做法）：
# 拼接重導向字串再交給 cmd 執行，是 Windows Defender 盯得很緊的模式，
# 整個 .ps1 會被判定成惡意程式然後鎖住。改成 Start-Process 分別導向
# 兩個暫存檔，跑完再合併補進紀錄檔，行為一樣但不會被誤判。

. (Join-Path $PSScriptRoot '_lib.ps1')

$root = Get-ProjectRoot
Assert-ProjectRoot $root
$py = Find-Python
if (-not $py) { exit 1 }

$dataDir = Join-Path $root 'data'
if (-not (Test-Path $dataDir)) {
  New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}
$log = Join-Path $dataDir 'daily.log'

$outTmp = Join-Path $env:TEMP "invest-daily-out-$PID.txt"
$errTmp = Join-Path $env:TEMP "invest-daily-err-$PID.txt"

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "=== $stamp 每日更新開始 ===" -Encoding utf8

$proc = Start-Process -FilePath $py `
          -ArgumentList @((Join-Path $root 'src\main.py'), '--quiet') `
          -WorkingDirectory $root `
          -RedirectStandardOutput $outTmp `
          -RedirectStandardError $errTmp `
          -NoNewWindow -Wait -PassThru

foreach ($f in @($outTmp, $errTmp)) {
  if (Test-Path $f) {
    $body = (Get-Content $f -Raw -Encoding utf8 -ErrorAction SilentlyContinue)
    if ($body -and $body.Trim()) { Add-Content -Path $log -Value $body.TrimEnd() -Encoding utf8 }
    Remove-Item $f -Force -ErrorAction SilentlyContinue
  }
}

$done = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
if ($proc.ExitCode -eq 0) {
  Add-Content -Path $log -Value "=== $done 完成 ===" -Encoding utf8

  # 這台是「決策機」：模擬倉狀態只在這裡寫。跑完就推回 GitHub，
  # 其他機器只讀（git pull 看結果），不再各自跑排程——
  # 否則兩邊會各自成交，帳本再也對不起來（見 docs/HANDOFF.md）。
  Push-Location $root
  try {
    git add data/paper_state.json data/paper_trades.jsonl data/paper_equity.jsonl `
            data/paper_runs.jsonl data/signals.jsonl data/reports 2>$null | Out-Null
    $staged = git diff --cached --name-only
    if ($staged) {
      $commitOut = git commit -m "每日更新 $stamp" 2>$null
      Add-Content -Path $log -Value ($commitOut -join "`n") -Encoding utf8
      $pushOut = git push 2>$null
      if ($LASTEXITCODE -eq 0) {
        Add-Content -Path $log -Value "已推送模擬倉更新到 GitHub。" -Encoding utf8
      } else {
        Add-Content -Path $log -Value "⚠️ git push 失敗，本機資料已更新但沒有同步上去，記得手動 push：" -Encoding utf8
        Add-Content -Path $log -Value ($pushOut -join "`n") -Encoding utf8
      }
    } else {
      Add-Content -Path $log -Value "（今天沒有新的模擬倉資料需要同步。）" -Encoding utf8
    }
  } catch {
    Add-Content -Path $log -Value "⚠️ git 同步時發生例外，本機資料已更新但沒有同步：$_" -Encoding utf8
  }
  Pop-Location
} else {
  Add-Content -Path $log -Value "=== $done 失敗，結束碼 $($proc.ExitCode) ===" -Encoding utf8
}
Add-Content -Path $log -Value '' -Encoding utf8

exit $proc.ExitCode
