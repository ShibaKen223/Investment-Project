"""設定檔讀寫的回歸測試。

重點是驗證「透過介面改設定，不會弄壞 YAML 檔案」——
這是使用者看不見、但壞掉會很痛的部分。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml as pyyaml  # noqa: E402

SAMPLE = """\
# ============================================================
# 目前持股
# ============================================================
# 這段檔頭註解必須留著。

positions:
  - code: "2330"
    shares: 1000
    cost: 1980.0
    entry_date: "2026-06-20"
    thesis: "原有部位。"
    invalidate: "砍單。"

# ------------------------------------------------------------
# 觀察清單
# ------------------------------------------------------------
watchlist:
  - code: "2603"
    note: "只觀察。"
"""

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL':4} {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = tmp / "config"
        cfg.mkdir()
        positions = cfg / "positions.yaml"
        positions.write_text(SAMPLE, encoding="utf-8")

        import store

        store.POSITIONS_FILE = positions
        store.BACKUP_DIR = tmp / "backups"

        # --- 新增兩筆 ---
        store.add_position(
            code="2603", shares=2000, cost=195.5, entry_date="2026-07-29",
            thesis="測試一", invalidate="失效一",
        )
        store.add_position(
            code="00919", shares=5000, cost=28.0, entry_date="2026-07-29",
            thesis="測試二", invalidate="失效二", core=True,
        )
        text = positions.read_text(encoding="utf-8")
        data = pyyaml.safe_load(text)

        check("檔頭註解保留", "這段檔頭註解必須留著。" in text)
        check(
            "區塊註解仍在 watchlist 正上方",
            text.index("# 觀察清單") < text.index("watchlist:")
            and text.index("00919") < text.index("# 觀察清單"),
            "新增的持股跑到了觀察清單註解後面",
        )
        check(
            "註解沒有黏在上一行的值後面",
            "core: true #" not in text and "\n# ---" in text,
        )
        check(
            "前導零代號維持字串",
            isinstance(data["positions"][2]["code"], str)
            and data["positions"][2]["code"] == "00919",
            f"得到 {data['positions'][2]['code']!r}",
        )
        check(
            "positions 與 watchlist 沒有互相污染",
            [p["code"] for p in data["positions"]] == ["2330", "2603", "00919"]
            and [w["code"] for w in data["watchlist"]] == ["2603"],
        )
        check("core 旗標寫入正確", data["positions"][2].get("core") is True)

        # --- 重複新增應被擋下 ---
        duplicated = False
        try:
            store.add_position(
                code="2603", shares=1, cost=1, entry_date="2026-07-29",
            )
        except ValueError:
            duplicated = True
        check("重複持股被擋下", duplicated)

        # --- 出場 ---
        store.exit_position("2603", "2026-07-30", 201.0)
        data = pyyaml.safe_load(positions.read_text(encoding="utf-8"))
        exited = next(p for p in data["positions"] if p["code"] == "2603")
        check(
            "出場欄位寫入正確",
            exited["exit_date"] == "2026-07-30" and exited["exit_price"] == 201.0,
        )

        # 出場後可以再次買回同一檔
        rebought = True
        try:
            store.add_position(
                code="2603", shares=1000, cost=210.0, entry_date="2026-07-31",
            )
        except ValueError:
            rebought = False
        check("出場後可再次買進同一檔", rebought)

        # --- 觀察清單 ---
        store.add_watch("2454", "聯發科")
        store.remove_watch("2603")
        data = pyyaml.safe_load(positions.read_text(encoding="utf-8"))
        check(
            "觀察清單新增／移除正確",
            [w["code"] for w in data["watchlist"]] == ["2454"],
        )

        # --- 從空的持股清單開始（全新使用者的情境）---
        # 專案預設出貨時 positions 是空的，因為新使用者多半還沒買任何股票，
        # 不該一開始就在儀表板上看到一堆假數字。
        # 但空清單是 ruamel round-trip 最容易出事的形狀，所以特別驗一次：
        # 介面的「新增持股」要能從零開始，而且不能把檔案裡的中文註解洗掉。
        empty_file = cfg / "empty.yaml"
        empty_file.write_text(
            "# 這段檔頭註解必須留著。\n"
            "\n"
            "positions: []\n"
            "\n"
            "# 觀察清單的區塊註解也要留著。\n"
            "watchlist:\n"
            "  - code: \"00919\"\n"
            "    note: \"範例。\"\n",
            encoding="utf-8",
        )
        store.POSITIONS_FILE = empty_file

        store.add_position(
            code="2330", shares=1000, cost=1980.0, entry_date="2026-06-20",
            thesis="測試理由", invalidate="測試認錯條件", core=False,
        )
        data = pyyaml.safe_load(empty_file.read_text(encoding="utf-8"))
        check("空清單能新增第一筆", len(data.get("positions") or []) == 1, str(data))
        check(
            "欄位寫入正確",
            data["positions"][0]["code"] == "2330"
            and data["positions"][0]["thesis"] == "測試理由",
            str(data["positions"][0]),
        )

        store.add_position(
            code="006208", shares=2000, cost=210.5, entry_date="2026-03-14",
            thesis="核心部位", invalidate="不設停損", core=True,
        )
        data = pyyaml.safe_load(empty_file.read_text(encoding="utf-8"))
        check("能繼續新增第二筆", len(data["positions"]) == 2)
        check("core 旗標寫入正確", data["positions"][1].get("core") is True)

        store.exit_position("2330", exit_date="2026-08-20", exit_price=2350.0)
        data = pyyaml.safe_load(empty_file.read_text(encoding="utf-8"))
        check(
            "登記出場正常",
            data["positions"][0].get("exit_price") == 2350.0,
            str(data["positions"][0]),
        )

        text = empty_file.read_text(encoding="utf-8")
        check("從空清單開始也不會洗掉檔頭註解", "這段檔頭註解必須留著" in text)
        check("觀察清單的區塊註解也還在", "觀察清單的區塊註解也要留著" in text)
        check(
            "觀察清單沒有被持股的寫入影響",
            [w["code"] for w in data["watchlist"]] == ["00919"],
            str(data.get("watchlist")),
        )

        store.POSITIONS_FILE = positions

        # --- 備份 ---
        check(
            "每次寫入前都有備份",
            store.BACKUP_DIR.exists() and len(list(store.BACKUP_DIR.glob("*.yaml"))) >= 5,
        )

        print()
        if failures:
            print(f"失敗 {len(failures)} 項 ❌  {failures}")
            return 1
        print("全部通過 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
