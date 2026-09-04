# 投資儀表板啟動器（Windows）
# 行為：伺服器沒在跑就啟動它；已經在跑就直接開瀏覽器分頁。
# 對應 macOS 的 launch\投資儀表板.app。

. (Join-Path $PSScriptRoot '_lib.ps1')

$root = Get-ProjectRoot
Assert-ProjectRoot $root
$py = Get-PythonOrExplain

if (Open-Dashboard -Root $root -Py $py) { exit 0 }
exit 1
