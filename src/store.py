"""設定檔讀寫與行情快取。

Web 介面透過這一層存取資料，重點有三個:

1. 用 ruamel.yaml 做 round-trip，從介面改持股時 YAML 裡的中文註解不會被洗掉。
2. 行情有快取，開儀表板不會每次都下載 5MB 全市場資料。
3. 所有寫入前先備份，改壞了救得回來。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import CommentMark
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ
from ruamel.yaml.tokens import CommentToken

import datasource
from datasource import Quote

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
RAW_DIR = DATA_DIR / "raw"

POSITIONS_FILE = CONFIG_DIR / "positions.yaml"
STRATEGY_FILE = CONFIG_DIR / "strategy.yaml"
QUOTE_CACHE = DATA_DIR / "quotes_cache.json"
JOURNAL_FILE = DATA_DIR / "journal.jsonl"

# 行情快取超過這個秒數就視為過期（預設 30 分鐘）
CACHE_TTL_SECONDS = 30 * 60


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096  # 避免長句子被自動折行
    return y


# --------------------------------------------------------------------------
# YAML 讀寫
# --------------------------------------------------------------------------

def load_doc(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"找不到設定檔: {path}")
    with path.open(encoding="utf-8") as fh:
        return _yaml().load(fh) or {}


MAX_BACKUPS_PER_FILE = 50


def save_doc(path: Path, doc) -> None:
    """寫入前先備份到 data/backups/，再原子性置換。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # 精確到微秒——同一秒內連續修改（例如快速連按兩次）不會互相覆蓋
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, BACKUP_DIR / f"{path.stem}-{stamp}{path.suffix}")
        _prune_backups(path.stem, path.suffix)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        _yaml().dump(doc, fh)
    tmp.replace(path)  # 原子性置換，中途失敗不會留下半個檔案


def _prune_backups(stem: str, suffix: str) -> None:
    """只保留最近的備份，避免無限成長。"""
    backups = sorted(BACKUP_DIR.glob(f"{stem}-*{suffix}"))
    for old in backups[:-MAX_BACKUPS_PER_FILE]:
        old.unlink(missing_ok=True)


def _relocate_tail_comment(seq, old_idx: int, new_idx: int) -> None:
    """把清單尾端的「區塊註解」搬到新的最後一筆上。

    YAML 解析時，夾在兩個區塊之間的註解（例如 positions 和 watchlist 中間那段
    「# 觀察清單」）會被掛在「前一筆資料的最後一個欄位」上。直接 append 新項目
    會讓新項目排到那段註解 *後面*，檔案讀起來就會變成「觀察清單」底下擺著持股。

    這裡把註解拆成兩段：空行留給舊項目當分隔，真正的 `#` 註解搬到新項目後面。
    """
    if old_idx < 0 or old_idx >= len(seq) or new_idx >= len(seq):
        return
    old, new = seq[old_idx], seq[new_idx]
    if not (hasattr(old, "ca") and old.ca.items and hasattr(new, "ca")):
        return
    if not old.keys() or not new.keys():
        return

    old_last_key = list(old.keys())[-1]
    entry = old.ca.items.get(old_last_key)
    if not entry:
        return

    for slot, token in enumerate(entry):
        if token is None or isinstance(token, list):
            continue
        value = getattr(token, "value", "")
        if "#" not in value:
            continue
        # 註解連同它前面的換行一起搬走（少了換行會黏在上一行的值後面），
        # 舊項目只留一個換行把自己那行收尾。
        token.value = "\n"
        new_last_key = list(new.keys())[-1]
        new.ca.items.setdefault(new_last_key, [None, None, None, None])
        new.ca.items[new_last_key][slot] = CommentToken(
            value, CommentMark(0), None
        )
        return


def load_positions_doc():
    return load_doc(POSITIONS_FILE)


def load_strategy_doc():
    return load_doc(STRATEGY_FILE)


# --------------------------------------------------------------------------
# 持股異動
# --------------------------------------------------------------------------

def add_position(
    *,
    code: str,
    shares: int,
    cost: float,
    entry_date: str,
    thesis: str = "",
    invalidate: str = "",
    core: bool = False,
) -> None:
    doc = load_positions_doc()
    if doc.get("positions") is None:
        doc["positions"] = []

    code = code.strip()
    for existing in doc["positions"]:
        if str(existing.get("code")).strip() == code and not existing.get("exit_date"):
            raise ValueError(f"{code} 已經在持股清單中且尚未出場。")

    # 必須用 CommentedMap（不是普通 dict），否則新項目沒有 .ca，
    # 區塊註解就無法搬移到它後面。
    entry = CommentedMap(
        {
            "code": DQ(code),  # 保持引號，避免 006208 被讀成數字
            "shares": int(shares),
            "cost": float(cost),
            "entry_date": DQ(entry_date),
            "thesis": thesis.strip(),
            "invalidate": invalidate.strip(),
        }
    )
    if core:
        entry["core"] = True

    seq = doc["positions"]
    previous_last = len(seq) - 1
    seq.append(entry)
    _relocate_tail_comment(seq, previous_last, len(seq) - 1)
    save_doc(POSITIONS_FILE, doc)


def exit_position(code: str, exit_date: str, exit_price: float) -> None:
    doc = load_positions_doc()
    code = code.strip()
    for entry in doc.get("positions") or []:
        if str(entry.get("code")).strip() == code and not entry.get("exit_date"):
            entry["exit_date"] = DQ(exit_date)
            entry["exit_price"] = float(exit_price)
            save_doc(POSITIONS_FILE, doc)
            return
    raise ValueError(f"找不到尚未出場的部位: {code}")


def update_position(code: str, **fields) -> None:
    """修改既有部位的欄位（成本、股數、理由等）。"""
    doc = load_positions_doc()
    code = code.strip()
    allowed = {"shares", "cost", "entry_date", "thesis", "invalidate", "core"}
    for entry in doc.get("positions") or []:
        if str(entry.get("code")).strip() == code and not entry.get("exit_date"):
            for key, value in fields.items():
                if key not in allowed or value is None:
                    continue
                if key in ("entry_date",):
                    entry[key] = DQ(str(value))
                elif key == "shares":
                    entry[key] = int(value)
                elif key == "cost":
                    entry[key] = float(value)
                elif key == "core":
                    entry[key] = bool(value)
                else:
                    entry[key] = str(value).strip()
            save_doc(POSITIONS_FILE, doc)
            return
    raise ValueError(f"找不到尚未出場的部位: {code}")


def add_watch(code: str, note: str = "") -> None:
    doc = load_positions_doc()
    if doc.get("watchlist") is None:
        doc["watchlist"] = []
    code = code.strip()
    for entry in doc["watchlist"]:
        if str(entry.get("code")).strip() == code:
            raise ValueError(f"{code} 已在觀察清單中。")
    doc["watchlist"].append({"code": DQ(code), "note": note.strip()})
    save_doc(POSITIONS_FILE, doc)


def remove_watch(code: str) -> None:
    doc = load_positions_doc()
    watch = doc.get("watchlist") or []
    code = code.strip()
    doc["watchlist"] = [
        e for e in watch if str(e.get("code")).strip() != code
    ]
    save_doc(POSITIONS_FILE, doc)


# --------------------------------------------------------------------------
# 規則異動
# --------------------------------------------------------------------------

def update_rules(
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    stop_basis: str,
    near_threshold_pct: float,
    reason: str,
) -> None:
    """改規則一定要附理由，並自動寫進 changelog。

    這是刻意的摩擦——規則被改鬆時，你會在兩個月後的紀錄裡看到當時的理由。
    """
    doc = load_strategy_doc()
    rules = doc.setdefault("rules", {})
    before = dict(rules)

    rules["stop_loss_pct"] = float(stop_loss_pct)
    rules["take_profit_pct"] = float(take_profit_pct)
    rules["stop_basis"] = str(stop_basis)
    rules["near_threshold_pct"] = float(near_threshold_pct)

    changed = {
        k: (before.get(k), rules[k]) for k in rules if before.get(k) != rules[k]
    }
    if changed:
        summary = "；".join(f"{k} {old} → {new}" for k, (old, new) in changed.items())
        if doc.get("changelog") is None:
            doc["changelog"] = []
        doc["changelog"].append(
            {
                "date": DQ(datetime.now().strftime("%Y-%m-%d")),
                "note": f"{summary}。理由：{reason.strip() or '（未填寫）'}",
            }
        )
    save_doc(STRATEGY_FILE, doc)


def update_objective(objective: str) -> None:
    doc = load_strategy_doc()
    doc["objective"] = objective.strip()
    save_doc(STRATEGY_FILE, doc)


# --------------------------------------------------------------------------
# 決策紀錄
# --------------------------------------------------------------------------

def add_journal(trade_date: str, text: str, tag: str = "note") -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "trade_date": trade_date,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "text": text.strip(),
    }
    with JOURNAL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_journal(trade_date: str | None = None) -> list[dict]:
    if not JOURNAL_FILE.exists():
        return []
    entries = []
    with JOURNAL_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if trade_date is None or record.get("trade_date") == trade_date:
                entries.append(record)
    return entries


# --------------------------------------------------------------------------
# 行情快取
# --------------------------------------------------------------------------

def _write_cache(quotes: dict[str, Quote], fetched_at: datetime) -> None:
    QUOTE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "trade_date": datasource.market_date(quotes),
        "quotes": {code: asdict(q) for code, q in quotes.items()},
    }
    tmp = QUOTE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(QUOTE_CACHE)


def _read_cache() -> tuple[dict[str, Quote], datetime] | None:
    if not QUOTE_CACHE.exists():
        return None
    try:
        payload = json.loads(QUOTE_CACHE.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        quotes = {
            code: Quote(**data) for code, data in payload["quotes"].items()
        }
        return quotes, fetched_at
    except Exception:  # noqa: BLE001 - 快取壞掉就當作沒有，重抓即可
        return None


def get_quotes(force: bool = False) -> tuple[dict[str, Quote], datetime, bool]:
    """取得行情。回傳 (quotes, 抓取時間, 是否為本次新抓)。

    force=False 且快取還新鮮時直接用快取，開儀表板才不會每次都等下載。
    """
    if not force:
        cached = _read_cache()
        if cached is not None:
            quotes, fetched_at = cached
            age = (datetime.now() - fetched_at).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return quotes, fetched_at, False

    now = datetime.now()
    quotes = datasource.fetch_quotes(raw_dir=RAW_DIR)
    _write_cache(quotes, now)
    return quotes, now, True


def cached_quotes_or_none() -> tuple[dict[str, Quote], datetime] | None:
    """只讀快取，不連網。抓取失敗時用來降級顯示。"""
    return _read_cache()
