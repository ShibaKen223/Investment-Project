"""一次跑完所有測試。

    python3 tests/run_all.py            # 只印每支的結果
    python3 tests/run_all.py --verbose  # 連每一項 PASS/FAIL 都印出來

有東西壞掉時結束碼為 1，所以也可以直接掛在排程或 git hook 上。

刻意不用 pytest：這個專案只需要 requests / PyYAML / flask / ruamel，
不想為了跑測試再多裝一個東西。每支測試檔都是可以單獨執行的普通 script，
這裡只是幫你一次叫過去而已。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# 測試的輸出全是中文。Windows 的主控台預設是 GBK/cp950，子行程一印中文
# 就 UnicodeEncodeError，於是每一支都「失敗」——失敗的是編碼，不是被測的東西。
# 那種紅字最浪費時間：它看起來像測試壞了，實際上程式好好的。
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("找不到任何測試檔。")
        return 1

    failed: list[str] = []
    for path in files:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CHILD_ENV,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0

        if verbose:
            print(f"\n{'=' * 60}\n{path.name}\n{'=' * 60}")
            print(output.rstrip())
        else:
            last = next(
                (ln for ln in reversed(output.splitlines()) if ln.strip()), ""
            )
            print(f"{'✅' if ok else '❌'}  {path.name:22} {last}")
            # 失敗的話把完整輸出印出來，不然使用者還要自己再跑一次才知道哪裡爛
            if not ok:
                print(output.rstrip())

        if not ok:
            failed.append(path.name)

    print()
    if failed:
        print(f"{len(failed)} 支測試檔失敗 ❌　{'、'.join(failed)}")
        return 1
    print(f"{len(files)} 支測試檔全部通過 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
