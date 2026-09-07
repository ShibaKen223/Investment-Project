# ============================================================
# 更新到最新版本（Windows）—— 雙擊 launch\win\更新.bat 就會執行這支
# ============================================================
# 做三件事，順序不能換：
#   1. git pull 拉最新的程式與帳本
#   2. 重新建立桌面捷徑（.lnk 記的是絕對路徑，專案搬家之後會失效）
#   3. 把拉下來的帳本印出來給你看
#
# 為什麼要獨立一支：安裝.ps1 會裝套件、問你要不要補歷史和開排程，
# 那是「第一次」在做的事。日常更新要的是快、而且不問問題。
# 對應 macOS 的 launch\update.sh。
# ------------------------------------------------------------

# 雙擊執行時視窗跑完就關，看不到結果，所以預設會停下來等一下。
# 從工具箱叫進來時外面已經有「按 Enter 回到選單」了，那時傳 -NoPause。
param([switch]$NoPause)

. (Join-Path $PSScriptRoot '_lib.ps1')

function Step { param([string]$T) Write-Host ""; Write-Host "-- $T --" -ForegroundColor White }
function OK   { param([string]$T) Write-Host "[OK] $T" -ForegroundColor Green }
function Warn { param([string]$T) Write-Host "[!!] $T" -ForegroundColor Yellow }
function Fail { param([string]$T) Write-Host "[XX] $T" -ForegroundColor Red }
function Bye {
  param([int]$Code = 0)
  if (-not $NoPause) { Write-Host ""; Read-Host "按 Enter 關閉這個視窗" }
  exit $Code
}

$root = Get-ProjectRoot
Write-Host "台股投資決策輔助系統 · 更新（Windows）" -ForegroundColor White
Write-Host "專案位置：$root" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $root 'webapp\app.py'))) {
  Fail "這個資料夾看起來不是專案（找不到 webapp\app.py）。"
  Write-Host "   請確認 launch\win\更新.bat 還放在專案資料夾裡。"
  Bye 1
}

Set-Location $root

git rev-parse --git-dir 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Fail "這份專案不是用 git clone 下來的，沒有辦法更新。"
  Write-Host "   要更新的話，重新 clone 一份："
  Write-Host "   git clone https://github.com/ShibaKen223/Investment-Project.git" -ForegroundColor DarkGray
  Bye 1
}

$before = (git rev-parse HEAD).Trim()
$branch = (git rev-parse --abbrev-ref HEAD).Trim()

# ------------------------------------------------------------
Step "1/3  拉最新版本"
# ------------------------------------------------------------
if ($branch -ne 'main') {
  Warn "目前在分支 '$branch'，不是 main。"
  Write-Host "   拉下來的會是這條分支的內容，決策機推上去的帳本不一定在這裡。"
}

# 有沒有還沒提交的變更。這件事一定要在 pull 之前講清楚，理由是帳本：
# data\paper_*.json(l) 在 .gitattributes 裡標了 -merge（見 docs\HANDOFF.md
# 的「兩本帳」），所以本機改過又沒提交時 git 會直接拒絕 pull——
# 那不是壞事，是設計，但錯誤訊息本身看不出來這件事。
#
# 這台很可能就是決策機（每日排程在這裡跑），所以這個分支不是例外情況，
# 是常態：排程跑完 push 失敗時，隔天手動更新就會走到這裡。
# --untracked-files=no 是刻意的：擋 pull 的是「改過的追蹤檔」，
# 尤其是帳本。隨手丟進專案資料夾的檔案不該讓更新停下來，
# 真的會被覆蓋時 git pull 自己會擋，訊息也講得比這裡清楚。
$dirty = @(git status --porcelain --untracked-files=no)
if ($dirty) {
  Warn "本機有還沒提交的變更："
  $dirty | ForEach-Object { Write-Host "     $_" }
  Write-Host ""
  if ($dirty -match 'data/(paper_|signals\.jsonl|reports|history)') {
    Write-Host "   其中有帳本或報告——這台機器自己跑出來的紀錄還沒推上去。" -ForegroundColor White
    Write-Host "   直接 pull 會把它蓋掉。先在專案資料夾裡執行："
    Write-Host "     git add data; git commit -m '本機帳本'; git push" -ForegroundColor DarkGray
  } else {
    Write-Host "   先處理掉（commit 或 git checkout -- <檔案>）再更新。"
  }
  Bye 1
}

# --ff-only 是刻意的，跟 daily_run.ps1 同一個理由：
# 能快轉就快轉，已經分岔就讓它失敗，而不是自動 merge 出第三本帳。
Write-Host "git pull --ff-only…" -ForegroundColor DarkGray
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Fail "拉不下來——本機與遠端已經分岔了。"
  Write-Host "   這通常表示兩台機器各自跑過排程，各記了一本帳。"
  Write-Host "   要自己決定留哪一邊，做法見 docs\HANDOFF.md 的「兩本帳」那一節。"
  Bye 1
}

$after = (git rev-parse HEAD).Trim()
if ($before -eq $after) {
  OK "已經是最新版本了（$(git log -1 --format='%h %s')）"
} else {
  OK "更新完成，這次拉進來的："
  git log --oneline "$before..$after" | ForEach-Object { Write-Host "     $_" }

  # 套件清單變了就要講。少了這句，新版程式 import 不到東西時，
  # 使用者看到的會是儀表板打不開，而不是「你該重裝套件了」。
  git diff --quiet $before $after -- requirements.txt
  if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Warn "requirements.txt 有變動，請從「投資工具箱 -> 重新安裝套件」跑一次。"
  }
}

# ------------------------------------------------------------
Step "2/3  重新建立桌面捷徑"
# ------------------------------------------------------------
# .lnk 記的是絕對路徑，所以 pull 完捷徑指到的腳本自動就是新版——
# 但專案資料夾搬過位置之後捷徑會失效，而症狀是雙擊沒反應或跳錯誤框。
# 每次更新都重寫一遍，成本是幾毫秒，換掉一個很難自己查出來的故障。
$desktop = [Environment]::GetFolderPath('Desktop')
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Set-Shortcut {
  param([string]$Name, [string]$Script, [string]$Desc, [string]$Icon, [string]$WindowStyle)
  $target = Join-Path $PSScriptRoot $Script
  if (-not (Test-Path $target)) { Warn "找不到 $Script，略過"; return }
  $lnkPath = Join-Path $desktop "$Name.lnk"
  $existed = Test-Path $lnkPath
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut($lnkPath)
  $lnk.TargetPath = $psExe
  $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle $WindowStyle -File `"$target`""
  $lnk.WorkingDirectory = $root
  $lnk.Description = $Desc
  $lnk.IconLocation = $Icon
  $lnk.Save()
  if ($existed) { OK "$Name：捷徑已重新指向 $target" }
  else          { OK "$Name：捷徑已放到桌面" }
}

if (Test-Path $desktop) {
  Set-Shortcut -Name '投資儀表板' -Script 'dashboard.ps1' `
    -Desc '開啟投資儀表板網頁介面' `
    -Icon "$env:SystemRoot\System32\shell32.dll,13" -WindowStyle 'Hidden'
  Set-Shortcut -Name '投資工具箱' -Script 'toolbox.ps1' `
    -Desc '補歷史、回測、研究筆記、排程' `
    -Icon "$env:SystemRoot\System32\shell32.dll,21" -WindowStyle 'Normal'
} else {
  Warn "找不到桌面資料夾，捷徑請自己從 launch\win\ 建立。"
}

# ------------------------------------------------------------
Step "3/3  現在的帳本"
# ------------------------------------------------------------
$py = Find-Python
if ($py) {
  & $py (Join-Path $root 'launch\ledger_summary.py')
} else {
  Warn "找不到 Python，跳過帳本摘要。"
}

Write-Host ""
Write-Host "更新完成。" -ForegroundColor Green
Write-Host "雙擊桌面的「投資儀表板」就會看到新版。" -ForegroundColor DarkGray
Bye 0
