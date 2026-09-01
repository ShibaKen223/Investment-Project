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

# 跑一支 python 腳本，把 stdout/stderr 併進紀錄檔，回傳結束碼。
# 不直接重導向而用 Start-Process 的理由見檔案開頭（Defender 誤判）。
function Invoke-Step {
  param([string]$Label, [string[]]$PyArgs)

  Add-Content -Path $log -Value "--- $Label ---" -Encoding utf8
  $p = Start-Process -FilePath $py `
         -ArgumentList $PyArgs `
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
  return $p.ExitCode
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "=== $stamp 每日更新開始 ===" -Encoding utf8

# ── 順序很重要，而且順序本身就是一個 bug 修正 ──────────────────
# 這三步必須跟 macOS 的 launch/daily_run.sh 一致。
# 之前這支只跑第 3 步，於是 Windows（現在唯一的決策機）從來沒有
# 補過歷史日 K：資料不足 → 模擬倉判定「今天沒有訊號」，
# 而那個「沒有訊號」是假的，它只代表沒有東西可看。
# paper.yaml 的 data_guard 只是擋住不讓錯誤被寫進紀錄，
# 「先補資料」才是真正的解法。
# 前兩步失敗不中斷：抓不到歷史時，持股監控報告仍然要照常產生。

$histExit = Invoke-Step '[1/3] 補歷史日 K' @((Join-Path $root 'src\history.py'), '--months', '2')
if ($histExit -ne 0) {
  Add-Content -Path $log -Value "⚠️ 歷史回補失敗或部分失敗（結束碼 $histExit），繼續往下跑。" -Encoding utf8
}

$gapExit = Invoke-Step '[2/3] 檢查並修補歷史破洞' @((Join-Path $root 'src\history.py'), '--fill-gaps')
if ($gapExit -ne 0) {
  Add-Content -Path $log -Value "⚠️ 破洞修補失敗（結束碼 $gapExit），繼續往下跑。" -Encoding utf8
}

$mainExit = Invoke-Step '[3/3] 產生今日報告（模擬倉也會前進一天）' @((Join-Path $root 'src\main.py'), '--quiet')

$done = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
if ($mainExit -eq 0) {
  Add-Content -Path $log -Value "=== $done 完成 ===" -Encoding utf8

  # 這台是「決策機」：模擬倉狀態只在這裡寫。跑完就推回 GitHub，
  # 其他機器只讀（git pull 看結果），不再各自跑排程——
  # 否則兩邊會各自成交，帳本再也對不起來（見 docs/HANDOFF.md）。
  Push-Location $root
  try {
    # ── pathspec 一定要先過濾 ───────────────────────────────────
    # git 對「不存在的 pathspec」是 fatal（結束碼 128），而且一旦 fatal，
    # 整批檔案都不會被 stage。data/paper_trades.jsonl 要等第一筆平倉
    # 才會被 paper.append_trade() 建立，所以在那之前這裡每天都是：
    # 整批失敗 → 錯誤被 2>$null 吞掉 → $staged 是空的 →
    # 印出「今天沒有新的模擬倉資料需要同步」。看起來一切正常，
    # 實際上從來沒有同步過任何東西（2026-08-22 ~ 09-01 就是這樣）。
    # 這正是本檔案開頭說最不想要的「安靜地不做事」，所以改成：
    # 只加入真的存在的路徑，而且每一步都檢查 $LASTEXITCODE。
    $syncPaths = @(
      'data/paper_state.json'
      'data/paper_trades.jsonl'
      'data/paper_equity.jsonl'
      'data/paper_runs.jsonl'
      'data/signals.jsonl'
      'data/reports'
    ) | Where-Object { Test-Path (Join-Path $root $_) }

    if (-not $syncPaths) {
      Add-Content -Path $log -Value "⚠️ data/ 底下找不到任何可同步的紀錄檔，跳過同步。" -Encoding utf8
    } else {
      $addOut = git add -- $syncPaths 2>&1
      if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $log -Value "⚠️ 暫存失敗（結束碼 $LASTEXITCODE），本機資料已更新但沒有同步：" -Encoding utf8
        Add-Content -Path $log -Value ($addOut -join "`n") -Encoding utf8
      } else {
        $staged = git diff --cached --name-only
        if ($staged) {
          # 推的是「當前分支」。決策機如果不在 main 上，其他機器 git pull
          # 拿到的會是另一條線，帳本一樣會分岔——所以要講出來，不要安靜地推。
          $branch = (git rev-parse --abbrev-ref HEAD).Trim()
          if ($branch -ne 'main') {
            Add-Content -Path $log -Value "⚠️ 目前在分支 '$branch'（不是 main），推上去的紀錄其他機器不一定拉得到。" -Encoding utf8
          }
          $commitOut = git commit -m "每日更新 $stamp" 2>&1
          Add-Content -Path $log -Value ($commitOut -join "`n") -Encoding utf8
          if ($LASTEXITCODE -ne 0) {
            Add-Content -Path $log -Value "⚠️ 提交失敗（結束碼 $LASTEXITCODE），沒有同步上去。" -Encoding utf8
          } else {
            # 原本這裡是 2>$null，於是失敗時 $pushOut 是空的，
            # 那句「記得手動 push」後面永遠不會有原因。改成 2>&1。
            $pushOut = git push 2>&1
            if ($LASTEXITCODE -eq 0) {
              Add-Content -Path $log -Value "已推送模擬倉更新到 GitHub（分支 $branch）。" -Encoding utf8
            } else {
              Add-Content -Path $log -Value "⚠️ 推送失敗，本機資料已更新但沒有同步上去，記得手動處理：" -Encoding utf8
              Add-Content -Path $log -Value ($pushOut -join "`n") -Encoding utf8
            }
          }
        } else {
          Add-Content -Path $log -Value "（今天沒有新的模擬倉資料需要同步。）" -Encoding utf8
        }
      }
    }
  } catch {
    Add-Content -Path $log -Value "⚠️ git 同步時發生例外，本機資料已更新但沒有同步：$_" -Encoding utf8
  }
  Pop-Location
} else {
  Add-Content -Path $log -Value "=== $done 失敗，結束碼 $mainExit ===" -Encoding utf8
}
Add-Content -Path $log -Value '' -Encoding utf8

exit $mainExit
