"""還原權值：把除權息與股票分割造成的價格斷崖修掉。

為什麼一定要做這件事
--------------------
資料源（TWSE／TPEX 每日收盤）給的是**當天實際成交的價格**，
不會替你把除權息和股票分割還原回去。於是歷史檔案裡會出現這種東西：

    0050  2025-06-17  收 188.65
    0050  2025-06-18  收  47.57      ← -74.8%

那天 0050 沒有崩盤，它是 1 股拆成 4 股（受益權單位分割）。
但系統看到的就是一根 -74.8% 的 K，後果是全面的：

  * 60 日均線在分割後 60 個交易日內全部是垃圾（跨越那道假斷崖）
  * 研究筆記的「一年報酬」「一年區間」只要窗口跨過去就是錯的
  * **真實持股監控會在除權息當天誤觸發停損** —— 這是最貴的一種錯
  * 模擬倉會在除息日誤判出場，然後把那筆假虧損寫進績效紀錄

這支模組做兩件事:

  1. **偵測** —— 台股有 10% 漲跌幅限制，所以單日收盤對收盤跌超過 11%
     在數學上不可能是「真的跌」，一定是除權息或分割。這種斷崖可以
     直接從資料本身認出來，而且因子也算得出來（今收 ÷ 昨收）。

  2. **還原** —— 採「往前調整」：**最近的價格保持原樣**，把更早的價格
     乘上因子往下壓。這個方向很重要——現價、停損線、你券商 App 上看到的
     數字必須是同一個，不能為了讓歷史好看而去動今天的價格。

三個資料來源，準確度由高到低
----------------------------
1. **TWSE 除權除息計算結果表**（`--fetch`，優先用這個）
   官方直接給「除權息前收盤價」與「除權息參考價」，因子是這兩個數字
   相除，不需要任何估計。涵蓋上市股票與 ETF。

2. **每日行情反推**（`scan_quotes()`，抓當天發生的事件）
   TWSE 的每日行情裡，除權息當天的「漲跌」是相對**除權息參考價**算的，
   不是相對昨收。所以 `收盤 − 漲跌 ≠ 昨天的收盤` 就是除權息的指紋。
   在每日流程裡當警報用（見 main.py）。

3. **價格序列偵測**（`--scan`，最後手段）
   台股有 10% 漲跌幅限制，跌超過 11% 不可能是真的跌。
   這條抓得到 1、2 都漏掉的東西——例如 0050 的受益權單位分割，
   那不是除權息，不會出現在結果表裡。

⚠️ 第 3 條**只可靠地認得出分割**。一般現金股息從價格序列分不出來
   （跌 5% 跟真的跌 5% 長得一樣），而且用開盤價估出來的因子會錯得
   很難看——實測 3034 估出來是「2026-07-13 參考價 494.00」，
   官方是「2026-07-10 參考價 519.00」，日期和數字都不對。
   所以偵測到的疑似配息一律標 `ignore`，不套用猜來的數字。

上櫃（TPEX）目前沒有接對應的結果表，上櫃標的的除權息要手動補登，
格式見 `config/corporate_actions.yaml` 的說明。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from history import Bar  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACTIONS_FILE = ROOT / "config" / "corporate_actions.yaml"

# 台股漲跌幅上限 10%。跌超過這個數字就不可能是「真的跌」。
# 抓 11% 留一點浮動空間，跌停（-10%）不會被誤判成除權息。
DEFAULT_LIMIT_PCT = 11.0

# 因子小於這個值就當成股票分割（而不是配息）：分割會改變股數，
# 歷史成交量要跟著放大才有可比性；配息不改變股數，量不能動。
SPLIT_FACTOR_MAX = 0.6


@dataclass(frozen=True)
class Action:
    """一次會造成價格斷崖的公司行為。

    date   除權息日／分割生效日。**當天的價格已經是新基準**，
           所以還原的對象是 date「之前」的每一根 K。
    factor 參考價 ÷ 前一日收盤，落在 (0, 1]。
           0050 一拆四 → 47.57 / 188.65 = 0.2522
    """

    code: str
    date: str
    factor: float
    kind: str = "split"        # "split"（改變股數）| "dividend"（不改變股數）
    note: str = ""
    source: str = "manual"     # "manual" | "detected" | "quote"
    ignore: bool = False       # 誤判時設 true，保留紀錄但不套用

    @property
    def adjusts_volume(self) -> bool:
        return self.kind == "split"

    def describe(self) -> str:
        pct = (self.factor - 1) * 100
        label = "分割" if self.kind == "split" else "除權息"
        text = f"{self.code} {self.date} {label} 因子 {self.factor:.6f}（{pct:+.1f}%）"
        if self.note:
            text += f" — {self.note}"
        return text


# --------------------------------------------------------------------------
# 設定檔
# --------------------------------------------------------------------------


def load_actions(path: Path | None = None) -> dict[str, list[Action]]:
    """讀出所有已登記的公司行為，依代號分組、每組依日期排序。"""
    path = path or ACTIONS_FILE
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    out: dict[str, list[Action]] = {}
    for code, entries in (raw.get("actions") or {}).items():
        code = str(code).strip()
        items: list[Action] = []
        for entry in entries or []:
            try:
                factor = _entry_factor(entry)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue     # 壞掉的一筆跳過，不要讓整份設定讀不出來
            if not 0 < factor <= 1.0000001:
                continue
            items.append(
                Action(
                    code=code,
                    date=str(entry["date"]).strip(),
                    factor=factor,
                    kind=str(entry.get("kind") or _infer_kind(factor)),
                    note=str(entry.get("note", "")),
                    source=str(entry.get("source", "manual")),
                    ignore=bool(entry.get("ignore", False)),
                )
            )
        if items:
            out[code] = sorted(items, key=lambda a: a.date)
    return out


def _entry_factor(entry: dict) -> float:
    """因子可以直接寫，也可以寫「前一日收盤 + 參考價」讓系統自己算。

    後者比較好稽核——那兩個數字就是公告上印的，因子是推導出來的。
    """
    if entry.get("factor") is not None:
        return float(entry["factor"])
    return float(entry["ref_price"]) / float(entry["prev_close"])


def _infer_kind(factor: float) -> str:
    return "split" if factor < SPLIT_FACTOR_MAX else "dividend"


def save_actions(actions: dict[str, list[Action]], path: Path | None = None) -> None:
    """寫回設定檔。人會讀這個檔，所以保留註解標頭與可讀的排版。"""
    path = path or ACTIONS_FILE
    lines = [_ACTIONS_HEADER.rstrip(), "", "actions:"]
    if not actions:
        lines.append("  {}")
    for code in sorted(actions):
        lines.append(f'  "{code}":')
        for action in sorted(actions[code], key=lambda a: a.date):
            lines.append(f'    - date: "{action.date}"')
            lines.append(f"      factor: {action.factor:.8f}")
            lines.append(f"      kind: {action.kind}")
            lines.append(f"      source: {action.source}")
            if action.ignore:
                lines.append("      ignore: true")
            if action.note:
                lines.append(f'      note: "{action.note}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# 還原
# --------------------------------------------------------------------------


def apply_actions(bars: list[Bar], actions: list[Action]) -> list[Bar]:
    """往前調整：date 之前的每一根 K 乘上因子，date 當天與之後不動。

    多筆行為會累乘。做法是從最後一根往前走，遇到一個生效日就把
    累積因子乘進去——這樣每根 K 都會被它「之後」發生的所有行為調整到。

    分割會同時放大歷史成交量（股數變多了），配息不會動量。
    量調錯的後果很具體：策略有一條「近 20 日均量 ≥ 500 張」的流動性門檻，
    分割後如果量沒跟著還原，那檔會在分割後 20 天內被誤判成流動性不足。
    """
    live = [a for a in actions if not a.ignore]
    if not bars or not live:
        return bars

    ordered = sorted(live, key=lambda a: a.date, reverse=True)
    out: list[Bar] = []
    price_factor = 1.0
    volume_factor = 1.0
    idx = 0

    for bar in reversed(bars):
        # 把所有「生效日 > 這根 K」的行為都累乘進來
        while idx < len(ordered) and ordered[idx].date > bar.date:
            price_factor *= ordered[idx].factor
            if ordered[idx].adjusts_volume:
                volume_factor /= ordered[idx].factor
            idx += 1

        if price_factor == 1.0 and volume_factor == 1.0:
            out.append(bar)
            continue

        out.append(
            replace(
                bar,
                open=bar.open * price_factor,
                high=bar.high * price_factor,
                low=bar.low * price_factor,
                close=bar.close * price_factor,
                volume=int(round(bar.volume * volume_factor)),
            )
        )

    out.reverse()
    return out


# --------------------------------------------------------------------------
# 偵測
# --------------------------------------------------------------------------


# 分割的因子一定是乾淨的比例（1 拆 4 → 0.25）。偵測到的數字會因為
# 資料有缺口或當天的漲跌而偏一點，落在容差內就吸附到標準比例上。
_SPLIT_RATIOS = [1 / n for n in (2, 3, 4, 5, 6, 8, 10, 20)] + [2 / 3, 3 / 4, 4 / 5]
_SPLIT_SNAP_TOLERANCE = 0.03      # 相對誤差 3% 以內就吸附

# 兩根 K 之間超過這麼多日曆天，就不能當成「單日」事件。
# 農曆年最長休到 9 天，抓 12 天留一點餘裕。
MAX_GAP_DAYS = 12


def _snap_split_factor(factor: float) -> tuple[float, str]:
    """把偵測到的分割因子吸附到最接近的標準比例。

    回傳 (因子, 說明)。找不到夠接近的就原樣退回。
    """
    for ratio in _SPLIT_RATIOS:
        if abs(factor - ratio) / ratio <= _SPLIT_SNAP_TOLERANCE:
            return ratio, f"吸附到 1:{1 / ratio:.0f} 分割"
    return factor, ""


def _days_between(earlier: str, later: str) -> int:
    from datetime import date as _date

    try:
        a = _date.fromisoformat(earlier)
        b = _date.fromisoformat(later)
    except ValueError:
        return 0
    return (b - a).days


def detect_from_bars(
    code: str, bars: list[Bar], limit_pct: float = DEFAULT_LIMIT_PCT
) -> list[Action]:
    """找出跌幅超過漲跌幅上限的斷崖——那些不可能是真的跌。

    只抓「跌」不抓「漲」：漲超過 10% 的來源太多（無漲跌幅限制的新股、
    ETF、興櫃轉上市），而除權息與分割一律讓價格往下跳。
    誤判一筆的代價是把真實的漲勢抹掉，比漏抓一筆嚴重得多。

    兩道防誤判:

    1. **必須是連續交易日。** 歷史檔案有缺月份時，「前一根」可能是一個月前，
       那段期間的正常跌幅會被誤認成除權息。實測踩過：2408 的 2026-06-30
       與 2026-08-03 相鄰（中間整個 7 月是缺的），-12.4% 其實是一個月的走勢。

    2. **分割與配息分開處理。** 分割的因子是乾淨的比例，吸附之後可以直接套用；
       配息的因子沒辦法從價格序列準確反推——收盤對收盤會把當天的漲跌
       也算進去（3034 除息當天開盤 -8.9%、收盤 -13.7%，真正的權值是前者），
       所以配息一律標成 ignore，等人填上公告的參考價再啟用。
       寧可留著一個看得見的待辦，也不要套用一個錯的因子。
    """
    found: list[Action] = []
    threshold = 1 - limit_pct / 100

    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if prev.close <= 0:
            continue

        factor = cur.close / prev.close
        if factor >= threshold:
            continue

        gap = _days_between(prev.date, cur.date)
        if gap > MAX_GAP_DAYS:
            continue    # 中間缺資料，這不是單日事件

        kind = _infer_kind(factor)
        approx = f"{prev.close:g} → {cur.close:g}"
        if gap > 4:
            approx += f"（中間隔了 {gap} 天，因子可能不準）"

        if kind == "split":
            snapped, snap_note = _snap_split_factor(factor)
            note = f"自動偵測：{approx}"
            if snap_note:
                note += f"；{snap_note}"
            found.append(
                Action(
                    code=code, date=cur.date, factor=snapped, kind="split",
                    note=note, source="detected",
                )
            )
            continue

        # 配息：用開盤價當參考價的估計值，比收盤價接近真相，但仍然只是估計。
        est = cur.open / prev.close if cur.open > 0 else factor
        found.append(
            Action(
                code=code, date=cur.date, factor=min(est, 1.0), kind="dividend",
                note=(
                    f"疑似除權息：{approx}；此因子由開盤價估算，"
                    f"請填入公告的除權息參考價後把 ignore 拿掉"
                ),
                source="detected",
                ignore=True,     # 估來的因子不自動套用
            )
        )
    return found


def implied_action_from_quote(
    code: str, trade_date: str, close: float, change: float, prev_close: float
) -> Action | None:
    """從每日行情反推今天有沒有除權息。

    TWSE／TPEX 在除權息當天，「漲跌」是相對**除權息參考價**算的，
    不是相對昨天的收盤。所以:

        收盤 − 漲跌  ==  今天的計算基準
        昨天的收盤   ==  除權息前的價格

    這兩個對不起來，差額就是被除掉的權值。這是唯一能在**當天**
    抓到一般現金股息的方法——光看價格序列是分不出來的。

    prev_close 要傳「歷史檔案裡昨天那根 K 的收盤」，不是行情自己算的昨收。
    """
    if prev_close <= 0 or close <= 0:
        return None
    reference = close - change
    if reference <= 0:
        return None
    factor = reference / prev_close
    # 0.5% 以內當成四捨五入誤差，不是除權息
    if factor > 0.995 or factor <= 0:
        return None
    return Action(
        code=code,
        date=trade_date,
        factor=factor,
        kind=_infer_kind(factor),
        note=f"行情反推：昨收 {prev_close:g} → 參考價 {reference:g}",
        source="quote",
    )


# TWSE 除權除息計算結果表。這是唯一權威的來源:
# 它直接給「除權息前收盤價」與「除權息參考價」，因子是這兩個數字相除，
# 不需要任何估計。從價格序列反推是不得已的替代方案，而且會錯——
# 實測 3034 用開盤價估出來的除息日是 2026-07-13、參考價 494.00，
# 官方是 2026-07-10、519.00，日期和數字都不對。
TWSE_EXRIGHT_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _roc_date_to_iso(text: str) -> str | None:
    """'115年06月11日' -> '2026-06-11'。看不懂就回 None。"""
    text = str(text).strip()
    try:
        year, rest = text.split("年", 1)
        month, rest = rest.split("月", 1)
        day = rest.replace("日", "").strip()
        return f"{int(year) + 1911:04d}-{int(month):02d}-{int(day):02d}"
    except (ValueError, AttributeError):
        return None


def fetch_twse_month(year: int, month: int, timeout: int = 25) -> list[Action]:
    """抓 TWSE 某個月的除權息結果表。回傳該月所有標的的行為。

    欄位位置不寫死——用標題文字去找，端點改版加欄位時才不會靜靜錯位。
    """
    import requests

    resp = requests.get(
        TWSE_EXRIGHT_URL,
        params={
            "startDate": f"{year:04d}{month:02d}01",
            "endDate": f"{year:04d}{month:02d}31",
            "response": "json",
        },
        timeout=timeout,
        headers={"User-Agent": _BROWSER_UA},
    )
    resp.raise_for_status()
    payload = resp.json()
    if str(payload.get("stat", "")).upper() != "OK":
        return []

    fields = [str(f).strip() for f in (payload.get("fields") or [])]

    def index_of(*keywords: str) -> int | None:
        for idx, name in enumerate(fields):
            if any(kw in name for kw in keywords):
                return idx
        return None

    i_date = index_of("資料日期")
    i_code = index_of("股票代號")
    i_name = index_of("股票名稱")
    i_prev = index_of("除權息前收盤價")
    i_ref = index_of("除權息參考價")
    i_kind = index_of("權/息")
    if None in (i_date, i_code, i_prev, i_ref):
        raise RuntimeError(
            f"除權息結果表的欄位跟預期不符，可能改版了：{fields}"
        )

    out: list[Action] = []
    for row in payload.get("data") or []:
        try:
            iso = _roc_date_to_iso(row[i_date])
            prev_close = float(str(row[i_prev]).replace(",", ""))
            ref_price = float(str(row[i_ref]).replace(",", ""))
        except (IndexError, ValueError, TypeError):
            continue
        if not iso or prev_close <= 0 or ref_price <= 0:
            continue
        factor = ref_price / prev_close
        if not 0 < factor <= 1.0000001:
            continue

        name = str(row[i_name]).strip() if i_name is not None else ""
        label = str(row[i_kind]).strip() if i_kind is not None else ""
        value = round((1 - factor) * prev_close, 4)
        out.append(
            Action(
                code=str(row[i_code]).strip(),
                date=iso,
                factor=factor,
                # 用因子大小分類，而不是「權/息」這個標籤。
                # 配股確實會改變股數，但台股的除權多半只配個幾十股，
                # 因子在 0.99 上下——那種規模的成交量還原純粹是雜訊，
                # 而且「權息一起發」時價格因子跟配股比例本來就不相等，
                # 硬拿它去調量反而引入誤差。
                # 真正需要還原成交量的是 1:2、1:4 那種分割，
                # 它們的因子一定遠低於 SPLIT_FACTOR_MAX。
                kind=_infer_kind(factor),
                note=(
                    f"TWSE 除權息結果表{f'（{name}）' if name else ''}："
                    f"前收 {prev_close:g} → 參考價 {ref_price:g}"
                    f"，權值+息值 {value:g}{f'（{label}）' if label else ''}"
                ),
                source="twse",
            )
        )
    return out


def scan_quotes(quotes: dict, codes: list[str]) -> list[Action]:
    """用今天的行情檢查這些代號有沒有除權息。回傳偵測到的行為。

    這是唯一能在**當天**抓到一般現金股息的辦法。歷史價格序列做不到——
    除息跌 5% 跟真的跌 5% 長得一模一樣。

    只在「歷史檔案裡最後一根 K 就是上一個交易日」時才判斷。
    中間缺了幾天的話，`收盤 − 漲跌` 對不上是因為我們的昨收是舊的，
    不是因為除權息，那種情況下報出來的會是假警報。
    """
    import history

    found: list[Action] = []
    for code in codes:
        quote = quotes.get(code)
        if quote is None or quote.change is None:
            continue
        bars = history.load_bars(code)
        if not bars:
            continue
        last = bars[-1]
        # 已經把今天寫進去了，或中間有缺口 → 這次不判斷
        if last.date >= quote.trade_date:
            continue
        if _days_between(last.date, quote.trade_date) > MAX_GAP_DAYS:
            continue
        action = implied_action_from_quote(
            code, quote.trade_date, quote.close, quote.change, last.close
        )
        if action is not None:
            found.append(action)
    return found


def record_detected(found: list[Action]) -> list[Action]:
    """把偵測到的行為寫進設定檔，回傳真正新增的那幾筆。

    一律以 ignore: true 寫入——因子雖然是交易所自己的參考價推出來的，
    但「我們存的昨收是不是對的」這件事沒辦法在當下確認。
    先留下紀錄並在報告上喊出來，由人確認後再拿掉 ignore。
    """
    if not found:
        return []
    staged = [
        a if a.ignore else Action(**{**a.__dict__, "ignore": True}) for a in found
    ]
    merged, added = merge_actions(load_actions(), staged)
    if added:
        save_actions(merged)
    return added


def merge_actions(
    existing: dict[str, list[Action]], found: list[Action]
) -> tuple[dict[str, list[Action]], list[Action]]:
    """把新偵測到的併進既有設定，回傳 (合併後, 真正新增的)。

    同一個 (代號, 日期) 已經登記過就不覆蓋——手動修正過的內容
    不該被下一次自動偵測蓋回去。
    """
    merged = {code: list(items) for code, items in existing.items()}
    added: list[Action] = []
    for action in found:
        known = {a.date for a in merged.get(action.code, [])}
        if action.date in known:
            continue
        merged.setdefault(action.code, []).append(action)
        added.append(action)
    return merged, added


_ACTIONS_HEADER = '''# ============================================================
# 公司行為（除權息、股票分割）—— 還原權值用
# ============================================================
# 這份檔案存在的理由：資料源給的是當天實際成交的價格，不會替你還原權值。
# 沒有它，0050 在 2025-06-18 會是一根 -74.8% 的 K（其實是 1 拆 4），
# 均線、期間報酬、停損判斷全部跟著錯。
#
# 怎麼產生:
#   python3 src/adjust.py --scan          偵測並列出（不寫檔）
#   python3 src/adjust.py --scan --write  偵測並寫進這個檔案
#
# 自動偵測只抓得到「跌幅超過 10% 漲跌幅上限」的斷崖——那種不可能是真的跌。
# **一般現金股息抓不到**（跌 5% 跟真的跌 5% 長得一樣），要自己補。
#
# 欄位:
#   date        除權息日／分割生效日。當天的價格已經是新基準，
#               系統還原的是這一天「之前」的每一根 K。
#   factor      參考價 ÷ 前一日收盤，例如 1 拆 4 → 0.25
#   ref_price   也可以改填這兩個讓系統自己算 factor，比較好稽核——
#   prev_close    這兩個數字就是公告上印的
#   kind        split（分割，會連成交量一起還原）| dividend（配息，不動量）
#   source      detected（自動偵測）| manual（手動補登）| quote（行情反推）
#   ignore      設 true = 保留這筆紀錄但不套用（用來標記誤判）
#   note        給人看的說明
#
# 手動補一筆現金股息的例子:
#
#   "2412":
#     - date: "2026-07-15"
#       prev_close: 136.5
#       ref_price: 131.0
#       kind: dividend
#       source: manual
#       note: "配息 5.5 元"
'''


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _do_fetch(args, existing: dict[str, list[Action]]) -> int:
    """從 TWSE 抓官方除權息資料，只留下我們有在追蹤的代號。"""
    import time
    from datetime import date

    import history

    tracked = set(
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else history.available_codes()
    )
    if not tracked:
        print("沒有任何追蹤中的代號。先跑 python3 src/history.py 補歷史。")
        return 1

    today = date.today()
    months: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(max(args.months, 1)):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    months.reverse()

    print(f"從 TWSE 除權息結果表抓 {len(months)} 個月，"
          f"比對 {len(tracked)} 檔追蹤中的標的…\n")

    found: list[Action] = []
    failures: list[str] = []
    for year, month in months:
        try:
            rows = fetch_twse_month(year, month)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{year}-{month:02d}（{type(exc).__name__}）")
            continue
        hits = [a for a in rows if a.code in tracked]
        found.extend(hits)
        print(f"  {year}-{month:02d}  該月 {len(rows):>3} 筆，"
              f"其中我們追蹤的 {len(hits)} 筆")
        time.sleep(1.5)   # 對端點客氣一點，被擋了反而更慢

    if failures:
        print(f"\n⚠️  這些月份抓失敗，稍後可以重跑補上：{'、'.join(failures)}")

    if not found:
        print("\n沒有抓到任何相關的除權息紀錄。")
        return 0

    # 官方資料要蓋掉先前用估計值猜出來的那幾筆——
    # 估來的因子連日期都可能是錯的，留著只會擋住正確的資料。
    cleaned: dict[str, list[Action]] = {}
    dropped = 0
    for code, items in existing.items():
        keep = [a for a in items if a.source != "detected" or a.kind == "split"]
        dropped += len(items) - len(keep)
        if keep:
            cleaned[code] = keep

    merged, added = merge_actions(cleaned, found)

    print(f"\n抓到 {len(found)} 筆，其中 {len(added)} 筆是新的：\n")
    for action in added:
        print(f"  ＋  {action.describe()}")
    if dropped:
        print(f"\n（順便清掉 {dropped} 筆先前用價格序列估出來的紀錄，"
              "官方資料比它們準）")

    if not args.write:
        print(f"\n這次沒有寫檔。加上 --write 才會寫進 {ACTIONS_FILE.name}。")
        return 0

    save_actions(merged)
    print(f"\n✅ 已寫入 {ACTIONS_FILE}")
    print("   這些是官方參考價，不是估計值，所以直接生效（沒有標 ignore）。")
    return 0


def main() -> int:
    import argparse

    import history

    parser = argparse.ArgumentParser(
        description="還原權值：偵測並登記除權息與股票分割造成的價格斷崖。"
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="從 TWSE 除權息結果表抓官方參考價（準確，優先用這個）",
    )
    parser.add_argument(
        "--months", type=int, default=24,
        help="--fetch 要往回抓幾個月（預設 24）",
    )
    parser.add_argument(
        "--scan", action="store_true", help="掃描所有歷史檔案，找出價格斷崖"
    )
    parser.add_argument(
        "--write", action="store_true",
        help="把掃描結果寫進 config/corporate_actions.yaml（需搭配 --scan）",
    )
    parser.add_argument(
        "--list", action="store_true", help="列出目前已登記的公司行為"
    )
    parser.add_argument(
        "--codes", help="只處理這些代號（逗號分隔）；預設全部"
    )
    parser.add_argument(
        "--limit-pct", type=float, default=DEFAULT_LIMIT_PCT,
        help=f"跌幅超過幾 %% 才當成公司行為（預設 {DEFAULT_LIMIT_PCT}）",
    )
    args = parser.parse_args()

    existing = load_actions()

    if args.fetch:
        return _do_fetch(args, existing)

    if args.list or not args.scan:
        if not existing:
            print("目前沒有登記任何公司行為。")
            print("執行 python3 src/adjust.py --scan 來偵測。")
            return 0
        total = 0
        for code in sorted(existing):
            for action in existing[code]:
                flag = "  [已忽略]" if action.ignore else ""
                print(f"  {action.describe()}{flag}")
                total += 1
        print(f"\n共 {total} 筆。")
        return 0

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else history.available_codes()
    )

    found: list[Action] = []
    for code in codes:
        bars = history.load_bars(code)      # 一定要用未還原的原始資料來偵測
        if len(bars) < 2:
            continue
        found.extend(detect_from_bars(code, bars, args.limit_pct))

    if not found:
        print(f"掃描 {len(codes)} 檔，沒有發現超過 {args.limit_pct:g}% 的價格斷崖。")
        print("（注意：一般現金股息偵測不到，需要手動補登。）")
        return 0

    merged, added = merge_actions(existing, found)

    print(f"掃描 {len(codes)} 檔，發現 {len(found)} 個價格斷崖：\n")
    for action in found:
        mark = "＋新增" if action in added else "  已登記"
        print(f"  {mark}  {action.describe()}")

    if not args.write:
        print(f"\n這次沒有寫檔。加上 --write 才會寫進 {ACTIONS_FILE.name}。")
        return 0

    save_actions(merged)
    print(f"\n✅ 已寫入 {ACTIONS_FILE}（新增 {len(added)} 筆）")
    print("   請打開檔案確認一下——自動偵測有可能誤判，")
    print("   誤判的那筆加上 ignore: true 就不會被套用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
