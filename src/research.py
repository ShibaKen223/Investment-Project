"""個股研究報告：讓你對「現在發生什麼事」有概念。

    python3 src/research.py              # 持股 + 觀察清單全部
    python3 src/research.py 2330         # 單一檔
    python3 src/research.py --glossary   # 印出完整名詞辭典
    python3 src/research.py --term PCB   # 查一個名詞

⚠️ 這份報告能做什麼、不能做什麼，請先看清楚:

  ✅ 能：從日 K 算出**可驗證的事實**——漲了多久、漲幅多少、
     量能有沒有配合、是整個產業一起漲還是它自己獨走、
     現在的價格在過去一年的什麼位置。

  ✅ 能：解釋你會在新聞裡看到的名詞（PCB、封裝、CoWoS、本益比…），
     以及這家公司在供應鏈的哪個位置、營收主要跟著什麼跑。

  ❌ 不能：告訴你「為什麼」漲。價格會動是因為新聞、財報、法說會、
     法人買賣、總體經濟——這套系統一個都沒接。
     它只能告訴你「漲勢的形狀」，形狀背後的原因要你自己去查。

  ❌ 不能：預測未來，也不構成投資建議。

所以報告的結構刻意分成三塊，而且標示得很清楚:
    【系統算出來的】  ← 從日 K 算的，可驗證
    【背景知識】      ← 人工整理的靜態資料，會過時
    【系統不知道的】  ← 要你自己去查的東西，附上該問什麼問題
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

import history  # noqa: E402
from history import Bar  # noqa: E402
from strategy import atr as compute_atr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
RESEARCH_DIR = ROOT / "data" / "research"

TRADING_DAYS_YEAR = 250
# 用市值型 ETF 當大盤的代理。誰先在資料裡就用誰。
BENCHMARKS = ("0050", "006208")


# --------------------------------------------------------------------------
# 知識庫
# --------------------------------------------------------------------------


def load_glossary() -> dict:
    path = CONFIG_DIR / "glossary.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("terms", {}) or {}


def load_sectors() -> tuple[dict, dict]:
    path = CONFIG_DIR / "sectors.yaml"
    if not path.exists():
        return {}, {}
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    stocks = {str(k): v for k, v in (raw.get("stocks") or {}).items()}
    return stocks, raw.get("sector_notes") or {}


# --------------------------------------------------------------------------
# 從日 K 算出來的事實
# --------------------------------------------------------------------------


@dataclass
class Facts:
    """全部都是從歷史日 K 直接算出來的，可以自己拿計算機驗。"""

    code: str
    as_of: str
    close: float
    bars: int

    returns: dict[str, float | None] = field(default_factory=dict)
    high_52w: float | None = None
    low_52w: float | None = None
    pct_from_high: float | None = None
    range_position: float | None = None      # 0 = 一年最低, 100 = 一年最高
    volatility: float | None = None          # 年化波動度 %
    atr14: float | None = None               # ATR(14)，單位是元
    atr_pct: float | None = None             # ATR 佔現價的百分比
    volume_ratio: float | None = None        # 近 5 日均量 / 近 60 日均量
    above_ma20: bool | None = None
    above_ma60: bool | None = None
    rally_start: tuple[str, float] | None = None   # (起漲日, 至今漲幅%)
    relative_strength: float | None = None   # 近 3 個月報酬 − 大盤報酬


def _ret(bars: list[Bar], days: int) -> float | None:
    if len(bars) <= days:
        return None
    past = bars[-1 - days].close
    return (bars[-1].close / past - 1) * 100 if past > 0 else None


def _volatility(bars: list[Bar], days: int = 60) -> float | None:
    """年化波動度：日報酬的標準差 × √250。"""
    window = bars[-(days + 1):]
    if len(window) < 20:
        return None
    rets = [
        window[i].close / window[i - 1].close - 1
        for i in range(1, len(window))
        if window[i - 1].close > 0
    ]
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_YEAR) * 100


def _rally_start(bars: list[Bar], lookback: int = TRADING_DAYS_YEAR) -> tuple[str, float] | None:
    """這波漲勢從哪裡開始：一年內的最低收盤，以及從那裡到現在的漲幅。"""
    window = bars[-lookback:]
    if len(window) < 20:
        return None
    low_bar = min(window, key=lambda b: b.close)
    if low_bar.close <= 0 or low_bar.date == bars[-1].date:
        return None
    gain = (bars[-1].close / low_bar.close - 1) * 100
    return low_bar.date, gain


def compute_facts(code: str, bars: list[Bar], benchmark: list[Bar] | None = None) -> Facts | None:
    if not bars:
        return None

    facts = Facts(
        code=code,
        as_of=bars[-1].date,
        close=bars[-1].close,
        bars=len(bars),
    )

    facts.returns = {
        "1 週": _ret(bars, 5),
        "1 個月": _ret(bars, 20),
        "3 個月": _ret(bars, 60),
        "半年": _ret(bars, 125),
        "1 年": _ret(bars, TRADING_DAYS_YEAR),
    }

    year = bars[-TRADING_DAYS_YEAR:]
    if len(year) >= 20:
        facts.high_52w = max(b.high for b in year)
        facts.low_52w = min(b.low for b in year)
        if facts.high_52w > 0:
            facts.pct_from_high = (facts.close / facts.high_52w - 1) * 100
        span = facts.high_52w - facts.low_52w
        if span > 0:
            facts.range_position = (facts.close - facts.low_52w) / span * 100

    facts.volatility = _volatility(bars)
    facts.atr14 = compute_atr(bars, 14)
    if facts.atr14 is not None and facts.close > 0:
        facts.atr_pct = facts.atr14 / facts.close * 100

    if len(bars) >= 60:
        recent = sum(b.volume for b in bars[-5:]) / 5
        base = sum(b.volume for b in bars[-60:]) / 60
        if base > 0:
            facts.volume_ratio = recent / base

    if len(bars) >= 20:
        facts.above_ma20 = facts.close > sum(b.close for b in bars[-20:]) / 20
    if len(bars) >= 60:
        facts.above_ma60 = facts.close > sum(b.close for b in bars[-60:]) / 60

    facts.rally_start = _rally_start(bars)

    if benchmark:
        own = _ret(bars, 60)
        mkt = _ret(benchmark, 60)
        if own is not None and mkt is not None:
            facts.relative_strength = own - mkt

    return facts


# --------------------------------------------------------------------------
# 敘述
# --------------------------------------------------------------------------


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def describe_shape(facts: Facts, peers: list[tuple[str, str, float]]) -> list[str]:
    """把價格資料翻譯成白話。

    這一段刻意只講「形狀」，不講「原因」——
    價格資料能證明的就只有形狀，說原因就是編故事了。
    """
    lines: list[str] = []

    # 現在的位置
    if facts.range_position is not None:
        if facts.range_position >= 90:
            where = "接近一年來的最高點"
        elif facts.range_position >= 70:
            where = "在一年區間的偏高位置"
        elif facts.range_position >= 30:
            where = "在一年區間的中段"
        elif facts.range_position >= 10:
            where = "在一年區間的偏低位置"
        else:
            where = "接近一年來的最低點"
        lines.append(
            f"- 目前價格 **{where}**"
            f"（一年區間 {facts.low_52w:,.1f} ～ {facts.high_52w:,.1f}，"
            f"現在站在 {facts.range_position:.0f}% 的位置，"
            f"距一年高點 {_pct(facts.pct_from_high)}）。"
        )

    # 漲勢的長度與幅度
    if facts.rally_start:
        start_date, gain = facts.rally_start
        if gain >= 15:
            lines.append(
                f"- 這波是從 **{start_date}** 的低點起算，到現在 **{gain:+.1f}%**。"
                f"換句話說，現在的價格是一段已經走了一陣子的漲勢的結果，"
                f"不是剛剛才發生的事。"
            )
        elif gain <= -15:
            lines.append(f"- 自 {start_date} 低點以來 {gain:+.1f}%，尚未明顯脫離低檔。")

    # 趨勢狀態
    if facts.above_ma20 is not None and facts.above_ma60 is not None:
        if facts.above_ma20 and facts.above_ma60:
            state = "站上 20 日與 60 日均線 → 多頭排列"
        elif not facts.above_ma20 and not facts.above_ma60:
            state = "跌破 20 日與 60 日均線 → 空頭排列"
        elif facts.above_ma60:
            state = "跌破 20 日線但仍在 60 日線之上 → 長期偏多、短期回檔"
        else:
            state = "站上 20 日線但仍在 60 日線之下 → 長期偏空、短期反彈"
        lines.append(f"- 趨勢狀態：{state}。")

    # 量能
    if facts.volume_ratio is not None:
        if facts.volume_ratio >= 1.5:
            lines.append(
                f"- **近期明顯放量**（近 5 日均量是 60 日均量的 "
                f"{facts.volume_ratio:.1f} 倍）。價漲量增通常代表有新資金進場；"
                f"但如果是在高檔爆量，也可能是有人在出貨——量本身不分好壞。"
            )
        elif facts.volume_ratio <= 0.6:
            lines.append(
                f"- **近期量縮**（近 5 日均量只有 60 日均量的 "
                f"{facts.volume_ratio:.1f} 倍），市場關注度下降。"
            )

    # 波動度與 ATR —— 這兩條直接關係到停損該設多寬
    if facts.volatility is not None:
        if facts.volatility >= 50:
            level = "很高"
        elif facts.volatility >= 30:
            level = "中等偏高"
        else:
            level = "偏低"
        lines.append(f"- 年化波動度 **{facts.volatility:.0f}%**（{level}）。")

    if facts.atr14 is not None and facts.atr_pct:
        # 把「8% 停損」換算成這檔的日常波動有幾倍，抽象的百分比才會變具體
        multiple = 8.0 / facts.atr_pct
        if multiple < 1.5:
            verdict = (
                f"**這檔的 8% 停損太緊**——只有 {multiple:.1f} 倍日常波動，"
                f"很容易被雜訊掃出場。這種標的適合改用 ATR 停損"
                f"（`config/paper.yaml` 把 stop_mode 改成 atr）"
            )
        elif multiple > 5:
            verdict = (
                f"8% 停損等於 {multiple:.1f} 倍日常波動，**偏寬**——"
                f"真的觸發時已經賠了不少"
            )
        else:
            verdict = f"8% 停損約等於 {multiple:.1f} 倍日常波動，算合理"
        lines.append(
            f"- ATR(14) **{facts.atr14:,.2f} 元**"
            f"（現價的 {facts.atr_pct:.1f}%，也就是「一天通常會動這麼多」）"
            f"——{verdict}。"
        )

    # 相對大盤
    if facts.relative_strength is not None:
        if facts.relative_strength >= 10:
            lines.append(
                f"- 近 3 個月**明顯強過大盤** {facts.relative_strength:+.1f} 個百分點。"
            )
        elif facts.relative_strength <= -10:
            lines.append(
                f"- 近 3 個月**落後大盤** {facts.relative_strength:.1f} 個百分點。"
            )
        else:
            lines.append(
                f"- 近 3 個月表現與大盤差不多（差距 "
                f"{facts.relative_strength:+.1f} 個百分點）。"
            )

    # 同業比較 —— 判斷是產業共振還是個股獨走
    if peers:
        own = facts.returns.get("3 個月")
        peer_avg = sum(r for _, _, r in peers) / len(peers)
        names = "、".join(f"{n}{r:+.0f}%" for _, n, r in peers[:4])
        if own is not None:
            if own - peer_avg >= 15:
                verdict = (
                    "**它明顯走得比同業強**。同一個產業裡只有它大漲，"
                    "通常代表市場在反映這家公司自己的事（訂單、財報、題材），"
                    "而不是整個產業的景氣——那就更需要去查清楚那件事是什麼。"
                )
            elif own - peer_avg <= -15:
                verdict = "**它明顯落後同業**，值得問一句為什麼。"
            else:
                verdict = (
                    "**它和同業一起動**，代表這比較像是整個產業的行情，"
                    "而不是這家公司獨有的事。"
                )
            lines.append(
                f"- 同業近 3 個月：{names}（平均 {peer_avg:+.0f}%），"
                f"它自己 {own:+.0f}% —— {verdict}"
            )

    return lines


def build_questions(facts: Facts, entry: dict | None) -> list[str]:
    """「去查這幾件事」的清單。

    Markdown 報告和網頁儀表板共用同一份，
    分成兩份的話遲早會有一邊忘了更新。
    """
    questions = [
        "**最近一次法說會怎麼說下一季？** 股價反映的是預期，"
        "財報好但展望差照樣會跌。",
        "**最近三個月的月營收年增率是往上還往下？** 台股每月 10 號前公布。",
        "**毛利率的方向？** 營收成長但毛利下滑 = 賣得多但賺得少。",
    ]
    if entry and entry.get("watch"):
        questions.append(
            f"**{entry['watch'][0]}** 現在的狀況（這是這家公司最關鍵的變數）。"
        )
    if facts.rally_start and facts.rally_start[1] >= 20:
        questions.append(
            f"**{facts.rally_start[0]} 前後發生了什麼事？** "
            f"漲勢是從那裡開始的，去找那段時間的新聞，通常就能找到起因。"
        )
    if facts.pct_from_high is not None and facts.pct_from_high > -5:
        questions.append(
            "**現在的價格已經反映了多少？** 股價在一年高點附近時，"
            "好消息通常已經在價格裡了。要問的是「還有什麼是市場還不知道的」。"
        )
    return questions


def collect_terms(entry: dict, glossary: dict) -> list[str]:
    """這檔股票相關的名詞代號，維持 sectors.yaml 裡的順序且去重。"""
    seen: list[str] = []
    for term in entry.get("terms") or []:
        if term in glossary and term not in seen:
            seen.append(term)
    return seen


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------


def build_stock_report(
    code: str,
    facts: Facts | None,
    entry: dict | None,
    sector_notes: dict,
    glossary: dict,
    peers: list[tuple[str, str, float]],
) -> list[str]:
    name = (entry or {}).get("name", "")
    title = f"## {code}" + (f" {name}" if name else "")
    lines = [title, ""]

    if facts is None:
        lines.append(
            "沒有歷史日 K，無法計算。先跑 `python3 src/history.py --months 24`。"
        )
        lines.append("")
        return lines

    lines.append(
        f"*收盤 {facts.close:,.2f}（{facts.as_of}）｜"
        f"歷史資料 {facts.bars} 根 K*"
    )
    lines.append("")

    # ---------- 系統算出來的 ----------
    lines.append("### 【系統算出來的】價格在說什麼")
    lines.append("")
    lines.append("| 期間 | 報酬 |")
    lines.append("| --- | ---: |")
    for period, value in facts.returns.items():
        lines.append(f"| {period} | {_pct(value)} |")
    lines.append("")

    shape = describe_shape(facts, peers)
    if shape:
        lines.extend(shape)
        lines.append("")
    lines.append(
        "> 以上每個數字都是從日 K 直接算出來的，你可以自己驗證。"
        "但它們只描述**價格的形狀**，不解釋原因。"
    )
    lines.append("")

    # ---------- 背景知識 ----------
    if entry:
        lines.append("### 【背景知識】這家公司在做什麼")
        lines.append("")
        lines.append(f"**產業**：{entry.get('sector', '—')}")
        lines.append("")
        if entry.get("what"):
            lines.append(f"**在賣什麼**：{entry['what'].strip()}")
            lines.append("")
        if entry.get("position"):
            lines.append(f"**在供應鏈的位置**：{entry['position'].strip()}")
            lines.append("")
        if entry.get("revenue_driver"):
            lines.append(f"**營收跟著什麼跑**：{entry['revenue_driver'].strip()}")
            lines.append("")
        if entry.get("watch"):
            lines.append("**該追蹤的指標**：")
            for item in entry["watch"]:
                lines.append(f"- {item}")
            lines.append("")

        # 產業層級的陷阱提醒
        sector_key = str(entry.get("sector", "")).split(" - ")[0]
        note = sector_notes.get(sector_key) or sector_notes.get(entry.get("sector", ""))
        if note:
            if note.get("cycle"):
                lines.append(f"**產業循環特性**：{note['cycle'].strip()}")
                lines.append("")
            if note.get("trap"):
                lines.append(f"> ⚠️ **這個產業最常見的誤判**：{note['trap'].strip()}")
                lines.append("")

        lines.append(
            "> 這一段是人工整理的靜態知識，**會過時**。"
            "它寫的是變化很慢的商業模式，不是財測或目標價。"
        )
        lines.append("")
    else:
        lines.append("### 【背景知識】")
        lines.append("")
        lines.append(
            f"`config/sectors.yaml` 裡還沒有 {code} 的資料，所以少了產業背景這一段。"
            "你可以照檔案裡的格式自己補一筆。"
        )
        lines.append("")

    # ---------- 系統不知道的 ----------
    lines.append("### 【系統不知道的】要你自己去查")
    lines.append("")
    lines.append(
        "這套系統只有價格資料，沒有接新聞、財報、法說會或法人買賣。"
        "上面的漲跌**是結果，不是原因**。想知道原因，去查這幾件事："
    )
    lines.append("")
    for q in build_questions(facts, entry):
        lines.append(f"- {q}")
    lines.append("")

    # ---------- 名詞 ----------
    terms = collect_terms(entry or {}, glossary)
    if terms:
        lines.append("<details><summary>📖 這檔會用到的名詞（點開）</summary>")
        lines.append("")
        for key in terms:
            item = glossary[key]
            lines.append(f"**{item['term']}** — {item['one_liner'].strip()}")
            lines.append("")
            if item.get("detail"):
                lines.append(f"  {item['detail'].strip()}")
                lines.append("")
            if item.get("why_it_matters"):
                lines.append(f"  *為什麼重要*：{item['why_it_matters'].strip()}")
                lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def build_report(codes: list[str], as_of: str | None = None) -> str:
    glossary = load_glossary()
    stocks, sector_notes = load_sectors()

    bars_by_code = {c: history.load_bars(c) for c in codes}

    benchmark: list[Bar] | None = None
    for candidate in BENCHMARKS:
        if bars_by_code.get(candidate):
            benchmark = bars_by_code[candidate]
            break
        extra = history.load_bars(candidate)
        if extra:
            benchmark = extra
            break

    facts_by_code = {
        c: compute_facts(c, bars, benchmark) for c, bars in bars_by_code.items()
    }

    lines: list[str] = []
    today = as_of or date.today().isoformat()
    lines.append(f"# 研究筆記 · {today}")
    lines.append("")
    lines.append(
        "> 這份報告分成三塊：**【系統算出來的】**是從日 K 直接計算、可以驗證的事實；"
        "**【背景知識】**是人工整理的靜態資料，會過時；"
        "**【系統不知道的】**是這套系統沒有資料源、需要你自己去查的部分。"
    )
    lines.append(">")
    lines.append(
        "> 系統**沒有接新聞、財報、法說會或法人買賣**，"
        "所以它能告訴你「漲勢長什麼樣子」，不能告訴你「為什麼漲」。"
        "不構成投資建議。"
    )
    lines.append("")

    if benchmark is None:
        lines.append(
            "> ⚠️ 找不到市值型 ETF（0050 / 006208）的歷史資料，"
            "因此無法計算「相對大盤強弱」。補上其中一檔的歷史日 K 就會出現。"
        )
        lines.append("")

    lines.append("---")
    lines.append("")

    for code in codes:
        entry = stocks.get(code)
        facts = facts_by_code.get(code)
        peers = _peers(code, entry, stocks, facts_by_code)
        lines.extend(
            build_stock_report(code, facts, entry, sector_notes, glossary, peers)
        )

    lines.append(
        "*名詞不懂就查：`python3 src/research.py --term <關鍵字>`，"
        "或 `--glossary` 印出完整辭典。*"
    )
    lines.append("")
    return "\n".join(lines)


def _peers(
    code: str,
    entry: dict | None,
    stocks: dict,
    facts_by_code: dict[str, Facts | None],
) -> list[tuple[str, str, float]]:
    """同產業的其他標的（只在這次分析的範圍內找）。"""
    if not entry:
        return []
    sector = str(entry.get("sector", "")).split(" - ")[0]
    if not sector:
        return []
    out: list[tuple[str, str, float]] = []
    for other, facts in facts_by_code.items():
        if other == code or facts is None:
            continue
        other_entry = stocks.get(other)
        if not other_entry:
            continue
        if str(other_entry.get("sector", "")).split(" - ")[0] != sector:
            continue
        ret = facts.returns.get("3 個月")
        if ret is not None:
            out.append((other, other_entry.get("name", other), ret))
    return sorted(out, key=lambda item: -item[2])


# --------------------------------------------------------------------------
# 名詞查詢
# --------------------------------------------------------------------------


def format_term(key: str, item: dict) -> list[str]:
    lines = [f"### {item['term']}"]
    alias = [a for a in (item.get("alias") or []) if a != item["term"]]
    if alias:
        lines.append(f"*又叫：{'、'.join(alias)}*")
    lines.append("")
    lines.append(item["one_liner"].strip())
    lines.append("")
    if item.get("detail"):
        lines.append(item["detail"].strip())
        lines.append("")
    if item.get("why_it_matters"):
        lines.append(f"**為什麼重要**：{item['why_it_matters'].strip()}")
        lines.append("")
    return lines


def search_terms(query: str, glossary: dict) -> list[tuple[str, dict]]:
    """用關鍵字找名詞。比對正式名稱、別名和代號，不分大小寫。"""
    q = query.strip().lower()
    if not q:
        return []
    exact: list[tuple[str, dict]] = []
    partial: list[tuple[str, dict]] = []
    for key, item in glossary.items():
        names = [key, item.get("term", "")] + list(item.get("alias") or [])
        lowered = [str(n).lower() for n in names]
        if q in lowered:
            exact.append((key, item))
        elif any(q in n for n in lowered):
            partial.append((key, item))
    return exact + partial


def build_glossary_doc(glossary: dict) -> str:
    by_category: dict[str, list[tuple[str, dict]]] = {}
    for key, item in glossary.items():
        by_category.setdefault(item.get("category", "其他"), []).append((key, item))

    lines = ["# 名詞辭典", ""]
    lines.append(
        "> 看懂新聞和法說會在講什麼。這是靜態知識，"
        "解釋「這個詞是什麼、在幹嘛」，不含市占率或財測那類會過期的資料。"
    )
    lines.append("")
    for category, items in by_category.items():
        lines.append(f"## {category}")
        lines.append("")
        for key, item in items:
            lines.extend(format_term(key, item))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令列
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="個股研究筆記與名詞辭典",
        epilog="範例：python3 src/research.py 2330   ／   "
               "python3 src/research.py --term CoWoS",
    )
    parser.add_argument(
        "codes", nargs="*", help="股票代號；省略則用持股 + 觀察清單"
    )
    parser.add_argument("--term", help="查一個名詞")
    parser.add_argument("--glossary", action="store_true", help="印出完整名詞辭典")
    parser.add_argument("--save", action="store_true",
                        help="存到 data/research/YYYY-MM-DD.md")
    args = parser.parse_args()

    glossary = load_glossary()

    if args.term:
        hits = search_terms(args.term, glossary)
        if not hits:
            print(f"辭典裡沒有「{args.term}」。")
            print("用 --glossary 看看有哪些詞，或自己加進 config/glossary.yaml。")
            return 1
        for key, item in hits:
            print("\n".join(format_term(key, item)))
        return 0

    if args.glossary:
        print(build_glossary_doc(glossary))
        return 0

    codes = args.codes or history.universe_from_config()
    if not codes:
        print("沒有要分析的標的。用 python3 src/research.py 2330 指定，")
        print("或在 config/positions.yaml 填入持股與觀察清單。")
        return 1

    missing = [c for c in codes if not history.load_bars(c)]
    if len(missing) == len(codes):
        print("這些標的都沒有歷史日 K，無法計算。先跑：")
        print("    python3 src/history.py --months 24")
        return 1

    markdown = build_report(codes)
    print(markdown)

    if args.save:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        path = RESEARCH_DIR / f"{date.today().isoformat()}.md"
        path.write_text(markdown, encoding="utf-8")
        print(f"\n已存到：{path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------
# 給網頁儀表板用的結構化資料
# --------------------------------------------------------------------------


def _strip_md(text: str) -> str:
    """把 **粗體** 標記拿掉。網頁模板自己處理排版，不需要 Markdown 語法。"""
    return text.replace("**", "")


def build_view(codes: list[str]) -> dict:
    """研究頁需要的一切。跟 build_report() 共用同一批計算與敘述。"""
    glossary = load_glossary()
    stocks, sector_notes = load_sectors()

    bars_by_code = {c: history.load_bars(c) for c in codes}

    benchmark: list[Bar] | None = None
    for candidate in BENCHMARKS:
        bars = bars_by_code.get(candidate) or history.load_bars(candidate)
        if bars:
            benchmark = bars
            break

    facts_by_code = {
        c: compute_facts(c, bars, benchmark) for c, bars in bars_by_code.items()
    }

    items: list[dict] = []
    for code in codes:
        entry = stocks.get(code)
        facts = facts_by_code.get(code)
        sector_key = str((entry or {}).get("sector", "")).split(" - ")[0]
        note = sector_notes.get(sector_key) or sector_notes.get(
            (entry or {}).get("sector", "")
        )
        items.append(
            {
                "code": code,
                "name": (entry or {}).get("name", ""),
                "facts": facts,
                "entry": entry,
                "note": note,
                "shape": [
                    _strip_md(line.lstrip("- "))
                    for line in describe_shape(
                        facts, _peers(code, entry, stocks, facts_by_code)
                    )
                ] if facts else [],
                "questions": [
                    _strip_md(q) for q in build_questions(facts, entry)
                ] if facts else [],
                "terms": [
                    {"key": k, **glossary[k]}
                    for k in collect_terms(entry or {}, glossary)
                ],
            }
        )

    return {
        "items": items,
        "has_benchmark": benchmark is not None,
        "missing": [c for c, b in bars_by_code.items() if not b],
        "glossary_count": len(glossary),
    }
