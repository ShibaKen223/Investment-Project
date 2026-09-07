"""模擬倉重置（src/paper_reset.py）與交易註銷（paper.append_void）的測試。

跑法:
    python3 tests/test_paper_reset.py

這支測試守的是四件事，每一件都是「壞掉的時候不會有人發現」的那種：

1. **歸檔不是刪除。** 重置的正當性完全建立在「舊紀錄還在、而且沒被改過」
   上面。真的刪掉，它在事後就跟湮滅紀錄無法區分——所以這裡逐檔比對
   SHA-256，而不只是檢查檔案存在。

2. **失敗不留半套狀態。** changelog 貼不上去、或上一次歸檔到一半中斷時，
   要在**動任何檔案之前**就停下來。半套狀態是最難查的：畫面上看起來
   重置成功了，帳卻沒記。

3. **註銷不改寫原行。** paper_trades.jsonl 是 append-only。作廢一筆交易
   是再 append 一筆註銷紀錄，原始那一行必須一個 byte 都沒變。

4. **註銷預設就不計入績效。** 這條要是漏了，作廢的交易會安靜地繼續算進
   勝率跟獲利因子，而且不會報錯——這個專案已經吃過一次「規則只寫在
   註解裡，該用到的那天沒人記得」的虧。

全部用合成資料與暫存目錄，不碰專案裡真正的 data/ 與 config/。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paper  # noqa: E402
import paper_reset  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}  {detail}")


def point_paper_at(tmp: Path) -> None:
    """把 paper 模組的五個路徑一起指到暫存目錄。

    只改 DATA_DIR 沒有用：paper.py 是在 import 時就從 DATA_DIR 推出
    STATE_FILE / TRADES_FILE / ... 的，不會連動。
    """
    paper.DATA_DIR = tmp
    paper.STATE_FILE = tmp / "paper_state.json"
    paper.TRADES_FILE = tmp / "paper_trades.jsonl"
    paper.EQUITY_FILE = tmp / "paper_equity.jsonl"
    paper.RUNS_FILE = tmp / "paper_runs.jsonl"


def make_trade(code: str = "2881", entry: str = "2026-08-24") -> paper.Trade:
    return paper.Trade(
        code=code,
        shares=1000,
        entry_date=entry,
        entry_price=135.0,
        exit_date="2026-09-05",
        exit_price=149.0,
        entry_reason="測試用",
        exit_reason="TARGET",
        exit_detail="停利",
        fees=230.0,
        tax=447.0,
        bars_held=10,
    )


MINIMAL_CONFIG = """\
# 這一行註解必須活過 changelog 的 append。
enabled: true

account:
  initial_cash: 1000000    # 初始虛擬資金
  position_pct: 10.0
  max_positions: 10
  trade_unit: 1

costs:
  fee_rate: 0.001425

strategy:
  trend_ma: 60

data_guard:
  min_ready_codes: 10

changelog:
  - date: "2026-08-20"
    note: "初版。"
"""


def seed_account(tmp: Path) -> None:
    """做出一本「跑過幾天」的帳。"""
    paper.save_state(paper.Account(cash=500_000.0, last_date="2026-09-07", owner="TESTBOX"))
    paper.append_equity(
        paper.DayResult(
            trade_date="2026-08-20",
            equity=1_000_000.0,
            cash=1_000_000.0,
            holdings=0,
        )
    )
    paper.append_run({"trade_date": "2026-08-20", "status": "ok"})


# ==========================================================================
# 1. 歸檔不是刪除
# ==========================================================================
print("\n--- 歸檔不是刪除 ---")

_tmp = Path(tempfile.mkdtemp(prefix="paper-reset-"))
point_paper_at(_tmp)
_cfg = _tmp / "paper.yaml"
_cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")
paper_reset.PAPER_CONFIG = _cfg

paper.save_state(paper.Account(cash=500_000.0, last_date="2026-09-07", owner="TESTBOX"))
paper.append_trade(make_trade())
_trades_before = paper.TRADES_FILE.read_bytes()
_state_before = paper.STATE_FILE.read_bytes()

_rc = paper_reset.main(["--reason", "測試用的正確性錯誤理由，夠長了", "--yes"])
check("重置回傳 0", _rc == 0, f"實際 {_rc}")

_archives = sorted((_tmp / "archive").glob("paper-reset-*"))
check("產生一個歸檔目錄", len(_archives) == 1, f"實際 {len(_archives)} 個")

_arch = _archives[0]
check("原始成交紀錄一個 byte 都沒變",
      (_arch / "paper_trades.jsonl").read_bytes() == _trades_before)
check("原始狀態檔一個 byte 都沒變",
      (_arch / "paper_state.json").read_bytes() == _state_before)

_manifest = json.loads((_arch / "MANIFEST.json").read_text(encoding="utf-8"))
check("MANIFEST 記下理由",
      _manifest["reason"] == "測試用的正確性錯誤理由，夠長了",
      _manifest.get("reason", ""))
check("MANIFEST 記下重置當下有 1 筆完成交易",
      _manifest["at_reset"]["closed_trades"] == 1)
check("MANIFEST 存了設定快照",
      _manifest["config_after_reset"]["account"]["trade_unit"] == 1)

_hashes = {f["name"]: f["sha256"] for f in _manifest["files"]}
check("MANIFEST 的 SHA-256 對得上歸檔的檔案",
      all(paper_reset.sha256_of(_arch / name) == digest
          for name, digest in _hashes.items()),
      str(_hashes))

check("舊的成交紀錄已離開 data/", not paper.TRADES_FILE.exists())
check("舊的淨值曲線已離開 data/", not paper.EQUITY_FILE.exists())

_fresh = paper.load_state(0.0)
check("新帳現金 = initial_cash", _fresh.cash == 1_000_000.0, str(_fresh.cash))
check("新帳沒有持股", not _fresh.positions)
check("新帳 owner 空白（等下一台機器認領）", _fresh.owner == "")
check("新帳沒有完成交易", paper.load_trades() == [])

shutil.rmtree(_tmp, ignore_errors=True)


# ==========================================================================
# 2. 失敗不留半套狀態
# ==========================================================================
print("\n--- 失敗不留半套狀態 ---")

_tmp = Path(tempfile.mkdtemp(prefix="paper-reset-guard-"))
point_paper_at(_tmp)
_cfg = _tmp / "paper.yaml"
_cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")
paper_reset.PAPER_CONFIG = _cfg
seed_account(_tmp)

check("理由太短會被擋下來",
      paper_reset.main(["--reason", "重來", "--yes"]) == 1)
check("被擋下來時檔案沒有動", paper.STATE_FILE.exists() and paper.EQUITY_FILE.exists())

# changelog 不在最後 → 動檔案之前就要停
_cfg.write_text(MINIMAL_CONFIG + "\nowner_note: 這一行讓 changelog 不再是最後一個 key\n",
                encoding="utf-8")
check("changelog 不在最後時拒絕執行",
      paper_reset.main(["--reason", "測試用的正確性錯誤理由，夠長了", "--yes"]) == 1)
check("拒絕時一個檔案都沒動",
      paper.STATE_FILE.exists() and paper.EQUITY_FILE.exists()
      and not (_tmp / "archive").exists())
_cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")

# 上一次歸檔到一半（有目錄、沒 MANIFEST）→ 拒絕
_half = _tmp / "archive" / "paper-reset-20260101-000000"
_half.mkdir(parents=True)
(_half / "paper_state.json").write_text("{}", encoding="utf-8")
check("偵測到沒歸檔完的目錄", paper_reset.incomplete_archives() == [_half])
check("有半套歸檔時拒絕執行",
      paper_reset.main(["--reason", "測試用的正確性錯誤理由，夠長了", "--yes"]) == 1)
check("拒絕時原始檔還在", paper.STATE_FILE.exists() and paper.EQUITY_FILE.exists())

# 補上 MANIFEST 之後就不再擋
(_half / "MANIFEST.json").write_text("{}", encoding="utf-8")
check("補上 MANIFEST 之後不再視為半套", paper_reset.incomplete_archives() == [])

# dry-run 什麼都不動
_before = {p.name: p.read_bytes() for p in _tmp.iterdir() if p.is_file()}
check("dry-run 回傳 0",
      paper_reset.main(["--reason", "測試用的正確性錯誤理由，夠長了", "--dry-run"]) == 0)
check("dry-run 之後每個檔案都一模一樣",
      {p.name: p.read_bytes() for p in _tmp.iterdir() if p.is_file()} == _before)
check("dry-run 沒有新增歸檔目錄",
      sorted((_tmp / "archive").glob("paper-reset-*")) == [_half])

shutil.rmtree(_tmp, ignore_errors=True)


# ==========================================================================
# 3. changelog：只多一則，註解不能掉
# ==========================================================================
print("\n--- changelog ---")

_tmp = Path(tempfile.mkdtemp(prefix="paper-reset-log-"))
point_paper_at(_tmp)
_cfg = _tmp / "paper.yaml"
_cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")
paper_reset.PAPER_CONFIG = _cfg
seed_account(_tmp)

_rc = paper_reset.main(["--reason", "只能買整張讓掃描池少掉一半，那段紀錄不算數", "--yes"])
check("重置成功", _rc == 0, f"實際 {_rc}")

_after_text = _cfg.read_text(encoding="utf-8")
check("設定檔的註解活下來了",
      "# 這一行註解必須活過 changelog 的 append。" in _after_text)
check("account 區塊的行內註解也活著", "# 初始虛擬資金" in _after_text)

import yaml  # noqa: E402

_parsed = yaml.safe_load(_after_text)
check("changelog 從 1 則變成 2 則", len(_parsed["changelog"]) == 2,
      str(len(_parsed["changelog"])))
check("新那則帶著理由",
      "只能買整張讓掃描池少掉一半" in _parsed["changelog"][-1]["note"])
check("新那則寫了判準",
      "搬門柱" in _parsed["changelog"][-1]["note"])
check("account 區塊沒被動到",
      _parsed["account"] == {"initial_cash": 1000000, "position_pct": 10.0,
                             "max_positions": 10, "trade_unit": 1})
check("strategy 區塊沒被動到", _parsed["strategy"] == {"trend_ma": 60})
check("沒留下 .tmp 檔", not (_tmp / "paper.yaml.tmp").exists())

shutil.rmtree(_tmp, ignore_errors=True)


# ==========================================================================
# 4. 註銷交易：不改寫原行，預設不計入績效
# ==========================================================================
print("\n--- 註銷交易 ---")

_tmp = Path(tempfile.mkdtemp(prefix="paper-void-"))
point_paper_at(_tmp)

_kept = make_trade(code="2330", entry="2026-08-01")
_voided = make_trade(code="2881", entry="2026-08-24")
paper.append_trade(_kept)
paper.append_trade(_voided)
_raw_before = paper.TRADES_FILE.read_bytes()

paper.append_void("2881", "2026-08-24", "2026-09-05", reason="設定壞掉時買的")

check("原本那兩行一個 byte 都沒變",
      paper.TRADES_FILE.read_bytes().startswith(_raw_before))
check("檔案多了一行（註銷是 append，不是改寫）",
      len(paper.TRADES_FILE.read_text(encoding="utf-8").strip().splitlines()) == 3)

_live = paper.load_trades()
check("預設讀不到被註銷的交易", [t.code for t in _live] == ["2330"],
      str([t.code for t in _live]))
check("include_void=True 看得到全部",
      sorted(t.code for t in paper.load_trades(include_void=True)) == ["2330", "2881"])

_stats = paper.performance(paper.load_trades(), [1_000_000.0], 1_000_000.0)
check("績效只算沒被註銷的那筆", _stats["trades"] == 1, str(_stats["trades"]))

# 同一檔反覆進出時，只該註銷指名的那一筆
paper.append_trade(make_trade(code="2881", entry="2026-09-10"))
check("同一檔的另一筆不受影響",
      sorted(t.entry_date for t in paper.load_trades() if t.code == "2881")
      == ["2026-09-10"])

_raised = False
try:
    paper.append_void("2330", "2026-08-01", "2026-09-05", reason="   ")
except ValueError:
    _raised = True
check("沒寫理由的註銷會被擋下來", _raised)

shutil.rmtree(_tmp, ignore_errors=True)


# ==========================================================================
print()
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通過。")
