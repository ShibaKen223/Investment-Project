#!/bin/bash
# 更新到最新版本 —— 在 Finder 裡雙擊這個檔案。
# 真正的內容在 launch/update.sh，這裡只是讓它變成可以雙擊的形式
# （Finder 只認 .command，而 .sh 雙擊會用編輯器打開）。
exec bash "$(dirname "${BASH_SOURCE[0]}")/update.sh"
