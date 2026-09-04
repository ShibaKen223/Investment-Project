# ============================================================
# Windows 捷徑共用工具
# ============================================================
# 路徑解析、Python 偵測、主控台編碼、啟動儀表板。
# launch\win\ 底下每一支腳本開頭都先載入這個檔案。
#
# ⚠️ 改這個檔案時請避開三個寫法，它們湊在一起會被 Windows Defender
#    判定成 PowerShell reverse shell（誤判，但檔案會直接被鎖住無法執行）：
#      1. New-Object System.Net.Sockets.TcpClient
#      2. 用 cmd.exe /c 搭配 >> 與 2>&1 轉一手
#      3. -WindowStyle Hidden
#    第 3 個單獨用沒事，跟前兩個之一組合就會中。
#    這也是底下用 Get-NetTCPConnection 而不是自己開 socket 的原因。
# ------------------------------------------------------------

# ------------------------------------------------------------
# 主控台改成 UTF-8
# ------------------------------------------------------------
# Windows 主控台預設是 GBK/cp950，而底下所有訊息都是中文，
# 不改就是一整片亂碼——看起來像程式壞了，其實只是編碼。
# 子行程（python）的輸出另外靠 PYTHONIOENCODING 處理。
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$env:PYTHONIOENCODING = 'utf-8'

$script:DashboardPort = 5173
$script:DashboardUrl  = 'http://127.0.0.1:5173/'


function Show-Alert {
  param([string]$Title, [string]$Message)
  # 有圖形介面就跳對話框（雙擊捷徑時沒有主控台可看），沒有就印出來。
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    [System.Windows.Forms.MessageBox]::Show($Message, $Title, 'OK', 'Error') | Out-Null
  } catch {
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor Red
    Write-Host $Message
    Read-Host '按 Enter 關閉'
  }
}


function Get-ProjectRoot {
  # 這支檔案在 <專案>\launch\win\ 之下，往上兩層就是專案根目錄。
  return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}


function Assert-ProjectRoot {
  param([string]$Root)
  # 確認算出來的路徑真的是專案，而不是隨便一個資料夾。
  # 沒有這道檢查的話，捷徑一旦失效，程式會安靜地在錯的地方建一個 data\
  # 並把錯誤寫進去，而你在專案資料夾裡怎麼找都找不到。
  if (Test-Path (Join-Path $Root 'webapp\app.py')) { return }
  Show-Alert '找不到專案資料夾' @"
這個捷徑找不到專案程式。

桌面捷徑記的是專案的絕對路徑，所以專案資料夾搬過位置之後就會失效。

正確做法：到專案資料夾裡雙擊 launch\win\安裝.bat，
它會重新建立指向新位置的捷徑。

（目前解析到的位置：$Root）
"@
  exit 1
}


function Find-Python {
  # 回傳可用的 Python 指令名稱，找不到回傳 $null。
  #
  # 為什麼不直接用 python：Windows 的 python.exe 很常是 Microsoft Store 的
  # app execution alias——一個空殼，執行起來直接 exit 49，不會有任何
  # 看得懂的錯誤訊息，只會跳出應用程式商店。所以不能只看指令存不存在，
  # 每個候選都要實際跑一次、確認它真的是 Python 3 才算數。
  foreach ($cand in @('py', 'python3', 'python')) {
    if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
    try {
      $out = & $cand -c 'import sys; print(sys.version_info[0])' 2>$null
      if ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq '3') { return $cand }
    } catch { }
  }
  return $null
}


function Get-PythonOrExplain {
  # 找不到 Python 就直接說清楚該怎麼辦，然後結束。
  $py = Find-Python
  if ($py) { return $py }
  Show-Alert '找不到 Python' @'
這個系統需要 Python 3。

安裝方式（擇一）：

1. 到 python.org/downloads 下載安裝，
   安裝畫面第一頁記得勾選「Add python.exe to PATH」。

2. 或在 PowerShell 執行：winget install Python.Python.3.12

裝完之後重新雙擊這個捷徑。

註：如果你覺得「我明明裝過了」——Windows 內建的 python 指令
很可能是 Microsoft Store 的空殼，那個不算數。
'@
  exit 1
}


function Test-DashboardAlive {
  # 連接埠有沒有人在聽。
  # 用 Get-NetTCPConnection 查系統的連線表，而不是自己開一個 socket 去連——
  # 後者（TcpClient）是 reverse shell 的招牌寫法，會被防毒擋掉整個檔案。
  try {
    $conn = Get-NetTCPConnection -LocalPort $script:DashboardPort -State Listen -ErrorAction Stop
    return ($null -ne $conn)
  } catch {
    return $false
  }
}


function Get-DashboardLogPaths {
  param([string]$Root)
  $dataDir = Join-Path $Root 'data'
  if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
  }
  return @{
    Out = (Join-Path $dataDir 'app.log')
    Err = (Join-Path $dataDir 'app.err.log')
  }
}


function Open-Dashboard {
  param([string]$Root, [string]$Py)
  # 已經在跑就只開分頁；沒在跑就啟動它，起不來就說明原因。
  if (Test-DashboardAlive) {
    Start-Process $script:DashboardUrl
    return $true
  }

  $logs = Get-DashboardLogPaths -Root $Root

  # 直接啟動 Python，不經 cmd.exe 轉一手。
  # 代價是 stdout 與 stderr 必須分成兩個檔（Start-Process 的兩個 -Redirect
  # 參數不接受同一個目標檔），好處是不會被防毒判定成惡意程式。
  # 兩個檔每次啟動都會覆寫，所以裡面永遠只有「這一次」的輸出——
  # 也就不需要在紀錄檔裡放分隔線去找最後一段了。
  $appPy = Join-Path $Root 'webapp\app.py'
  Start-Process -FilePath $Py -ArgumentList @($appPy) `
                -WorkingDirectory $Root `
                -RedirectStandardOutput $logs.Out `
                -RedirectStandardError $logs.Err `
                -WindowStyle Minimized

  # app.py 就緒之後會自己開瀏覽器，這裡只負責等它起來。
  for ($i = 0; $i -lt 60; $i++) {
    if (Test-DashboardAlive) { return $true }
    Start-Sleep -Milliseconds 250
  }

  Show-DashboardFailure -Root $Root
  return $false
}


function Show-DashboardFailure {
  param([string]$Root)
  # 重點是直接告訴使用者發生什麼事，而不是丟一句「請查看 data\app.log」
  # ——需要看說明的人不會去翻紀錄檔。
  $logs = Get-DashboardLogPaths -Root $Root
  $text = ''
  foreach ($f in @($logs.Err, $logs.Out)) {
    if (Test-Path $f) {
      $text += (Get-Content $f -Raw -Encoding utf8 -ErrorAction SilentlyContinue)
      $text += "`n"
    }
  }

  if ($text -match 'Address already in use|10048|已經被佔用') {
    $headline = '儀表板已經在執行了'
    $detail = @"
連接埠 $script:DashboardPort 有人在用，但它沒有回應——
多半是之前那個行程當掉了。

處理方式：打開「工作管理員」，在「詳細資料」分頁找到 python.exe，
結束它，然後再雙擊一次捷徑。

（如果有好幾個 python.exe 分不出來，重新開機也可以。）
"@
  } elseif ($text -match "No module named '?([\w\.]+)") {
    $missing = $Matches[1]
    $headline = "缺少套件：$missing"
    $detail = @"
雙擊 launch\win\安裝.bat 就會補齊。

或在 PowerShell 手動執行：
cd "$Root"
py -m pip install -r requirements.txt
"@
  } elseif ($text -match 'SyntaxError|IndentationError') {
    $headline = '程式碼有語法錯誤'
    $detail = @"
通常是更新到一半中斷了。在 PowerShell 執行 git status 看看。

$text
"@
  } else {
    $tail = ''
    if ($text.Trim()) {
      $keep = @($text -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
      $tail = ($keep | Select-Object -Last 8) -join "`n"
    }
    if (-not $tail) { $tail = '（紀錄檔裡沒有內容，可能是 Python 本身有問題）' }
    $headline = '儀表板啟動失敗'
    $detail = "錯誤訊息：`n`n$tail`n`n完整紀錄：`n$($logs.Err)`n$($logs.Out)"
  }

  Show-Alert $headline $detail
}
