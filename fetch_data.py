"""Fetch market data, calculate neutral indicators, and write docs/data.json.

This project is an information dashboard, not investment advice.  A successful
workflow run is not treated as proof that every source returned fresh data.
"""
from __future__ import annotations

import datetime as dt
import csv
import html as html_lib
import io
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

OUT = os.path.join(os.path.dirname(__file__), "docs", "data.json")
TZ_TAIPEI = dt.timezone(dt.timedelta(hours=8))

# Symbol, display name, whether the provider's volume is meaningful for a ratio.
# Broad index symbols and FX do not have a comparable consolidated traded volume.
SYMBOLS = [
    ("^TWII", "台灣加權指數", False),
    ("^GSPC", "標普 500", False),
    ("^IXIC", "那斯達克指數", False),
    ("^DJI", "道瓊工業指數", False),
    ("^SOX", "費城半導體", False),
    ("GC=F", "黃金期貨", True),
    ("CL=F", "WTI 原油期貨", True),
    ("TWD=X", "美元兌台幣", False),
]
STOOQ = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji",
    "^SOX": "^sox", "GC=F": "gc.f", "CL=F": "cl.f",
    "TWD=X": "usdtwd",
}
# 第三階段「市場分析」只追蹤這三個標的，順序即畫面欄位順序：
# 加權指數與費城半導體為主，標普 500 作為大盤背景放第三欄。
# 其他標的（那斯達克、道瓊、黃金、原油、匯率）仍在上方行情卡片顯示，但不納入市場分析與風險分數。
MARKET_ANALYSIS_SYMBOLS = ("^TWII", "^SOX", "^GSPC")
SUPPORT_DAYS, NEAR, FAR = 60, 2.0, 5.0
FUT_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"  # 期交所開放資料：期貨每日交易行情（只提供最新一日）
TWSE_TURNOVER_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"  # 證交所：每日市場成交資訊
TWSE_INTRADAY_URL = "https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX"  # 證交所歷史/盤後 CSV 備援
TWSE_MIS_INDEX_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"  # 證交所 MIS 加權指數盤中快照


NY = ZoneInfo("America/New_York")
PAUSE = 3.0  # 每個標的之間的間隔（秒），避免瞬間連續請求

def _yf(t: str) -> str:
    return "https://finance.yahoo.com/quote/" + quote(t, safe="=")

def _tv(s: str) -> str:
    return "https://www.tradingview.com/chart/?symbol=" + quote(s, safe="")

# 卡片外部行情連結：日盤、夜盤分開，夜盤另提供 TradingView 連續合約 K 線。
_TV_TXF = "https://tw.tradingview.com/chart/J8yYzCyl/?symbol=TAIFEX%3ATXF1%21"
LINKS = {
    "^TWII": {"yahoo": "https://tw.stock.yahoo.com/quote/%5ETWII", "tv": _tv("TWSE:TAIEX")},
    "TX": {"cmoney_day": "https://www.cmoney.tw/forum/futures/TXF1"},
    "TXALL": {"cmoney_night": "https://www.cmoney.tw/forum/futures/TXF1?s=p", "tv": _TV_TXF},
    "^GSPC": {"yahoo": "https://tw.stock.yahoo.com/quote/%5EGSPC", "tv": _tv("SP:SPX")},
    "^IXIC": {"yahoo": "https://tw.stock.yahoo.com/quote/%5EIXIC", "tv": _tv("NASDAQ:IXIC")},
    "^DJI": {"yahoo": "https://tw.stock.yahoo.com/quote/%5EDJI", "tv": _tv("DJ:DJI")},
    "^SOX": {"yahoo": "https://tw.stock.yahoo.com/quote/%5ESOX", "tv": _tv("NASDAQ:SOX")},
    "GC=F": {"yahoo": "https://tw.stock.yahoo.com/quote/GC=F", "tv": _tv("COMEX:GC1!")},
    "CL=F": {"yahoo": "https://tw.stock.yahoo.com/quote/CL=F", "tv": _tv("NYMEX:CL1!")},
    "TWD=X": {"yahoo": "https://tw.stock.yahoo.com/quote/USDTWD=X", "tv": _tv("FX_IDC:USDTWD")},
}
KINDS = {"^TWII": "tw", "TX": "twfut", "TXALL": "twfut", "GC=F": "24h", "CL=F": "24h", "TWD=X": "24h"}


def retry(fn, *args, tries: int = 3, wait: float = 3.0, **kwargs):
    """失敗時等一下再試，最多 tries 次；最後一次仍失敗就丟出例外。"""
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == tries:
                raise
            print(f"{getattr(fn, '__name__', 'call')} 第 {attempt} 次失敗（{exc}），{wait * attempt:.0f} 秒後重試")
            time.sleep(wait * attempt)


def market_kind(sym: str) -> str:
    return KINDS.get(sym, "us")


def is_market_open(kind: str, now_utc: dt.datetime) -> bool:
    tp, ny = now_utc.astimezone(TZ_TAIPEI), now_utc.astimezone(NY)
    if kind == "tw":
        return tp.weekday() < 5 and dt.time(9, 0) <= tp.time() < dt.time(13, 30)
    if kind == "twfut":
        return tp.weekday() < 5 and dt.time(8, 45) <= tp.time() < dt.time(13, 45)
    if kind == "us":
        return ny.weekday() < 5 and dt.time(9, 30) <= ny.time() < dt.time(16, 0)
    wd, t = ny.weekday(), ny.time()  # 24 小時商品：美東週日 18:00 ～ 週五 17:00
    return not (wd == 5 or (wd == 4 and t >= dt.time(17, 0)) or (wd == 6 and t < dt.time(18, 0)))


def session_fields(sym: str, item_date: str | None, now_utc: dt.datetime) -> dict[str, str]:
    kind = market_kind(sym)
    is_open = is_market_open(kind, now_utc)
    local = now_utc.astimezone(NY if kind in ("us", "24h") else TZ_TAIPEI).strftime("%Y-%m-%d")
    if kind == "24h":
        return {"session": "交易中" if is_open else "休市", "session_class": "open" if is_open else "closed"}
    if kind == "twfut":
        return {"session": "上一交易日資料" if is_open else "已收盤", "session_class": "closed"}
    if is_open:
        if item_date != local:  # 沒有內建休市日曆：交易時段內最新資料不是今天，就標示休市或無新資料
            return {"session": "休市或無新資料", "session_class": "closed"}
        return {"session": "盤中", "session_class": "open"}
    return {"session": "已收盤", "session_class": "closed"}


def from_yahoo(sym: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
    if df is None or df.empty or "Close" not in df:
        raise ValueError("Yahoo Finance 沒有回傳有效行情")
    if "Low" not in df:
        df["Low"] = df["Close"]
    if "Volume" not in df:
        df["Volume"] = float("nan")
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def from_stooq(sym: str) -> pd.DataFrame:
    if sym not in STOOQ:
        raise ValueError("此標的沒有 Stooq 備援代碼")
    response = requests.get(
        f"https://stooq.com/q/d/l/?s={STOOQ[sym]}&i=d", timeout=20
    )
    response.raise_for_status()
    if not response.text.strip() or "Date" not in response.text.splitlines()[0]:
        raise ValueError("Stooq 回傳內容不是有效 CSV")
    df = pd.read_csv(io.StringIO(response.text), parse_dates=["Date"], index_col="Date").tail(260)
    if "Volume" not in df:
        df["Volume"] = float("nan")
    if "Low" not in df:
        df["Low"] = df["Close"]
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def judge(close: float, support: float, dist: float, vr: float | None) -> tuple[str, str]:
    if close < support:
        return "收盤低於近 60 日支撐，留意後續是否站回", "break"
    if dist <= NEAR:
        extra = "量縮" if vr is not None and vr < 0.8 else "放量" if vr is not None and vr > 1.5 else ""
        return "接近近 60 日支撐" + (f"，{extra}" if extra else ""), "near"
    if dist > FAR:
        return "距近 60 日支撐尚遠", "ok"
    return "與近 60 日支撐有一段距離", "ok"


def twse_turnover() -> dict[str, Any] | None:
    """取得證交所每日成交金額與加權指數正式收盤值。"""
    try:
        first = dt.datetime.now(TZ_TAIPEI).replace(day=1)
        months = [(first - dt.timedelta(days=1)).replace(day=1), first]
        rows = []
        for m in months:
            r = retry(requests.get, TWSE_TURNOVER_URL,
                      params={"date": m.strftime("%Y%m01"), "response": "json"},
                      timeout=20)
            r.raise_for_status()
            payload = r.json()
            if payload.get("stat") not in (None, "OK"):
                raise ValueError(f"證交所回應狀態：{payload.get('stat')}")
            rows += payload.get("data") or []
        series = []
        for row in rows:
            if len(row) < 5:
                continue
            y, mo, d = str(row[0]).split("/")
            date = f"{int(y) + 1911}-{mo}-{d}"
            amount = num(row[2])
            idx = num(row[4])
            if amount is not None and idx is not None:
                series.append((date, amount / 1e8, idx))
        series.sort()
        if len(series) < 2:
            raise ValueError("證交所成交金額或指數收盤資料不足")
        prior = [v for _, v, _ in series[-21:-1] if v > 0]
        if not prior or series[-1][1] <= 0:
            raise ValueError("成交金額數值異常")
        return {
            "date": series[-1][0],
            "amount_yi": round(series[-1][1], 1),
            "ratio": series[-1][1] / (sum(prior) / len(prior)),
            "index_close": series[-1][2],
            "previous_index_close": series[-2][2],
            "source": "證交所 FMTQIK",
        }
    except Exception as exc:
        print(f"加權指數成交金額／正式收盤價抓取失敗：{exc}")
        return None


def parse_twse_intraday_csv(raw: str, expected_date: str) -> dict[str, Any]:
    """解析證交所 MI_5MINS_INDEX CSV，容忍 BOM、引號、等號及欄名空白差異。"""
    rows = list(csv.reader(io.StringIO(raw.lstrip("\ufeff"))))
    header_idx = None
    time_col = index_col = None

    def clean(value: Any) -> str:
        value = str(value or "").strip().lstrip("\ufeff").strip().strip('"').strip("'")
        value = value.lstrip("=").strip().strip('"').strip("'")
        return re.sub(r"\s+", "", value)

    for i, row in enumerate(rows):
        normalized = [clean(value) for value in row]
        time_candidates = [j for j, value in enumerate(normalized)
                           if value == "時間" or value.endswith("時間")]
        index_candidates = [j for j, value in enumerate(normalized)
                            if "發行量加權股價指數" in value or value in ("加權指數", "加權股價指數")]
        if time_candidates and index_candidates:
            header_idx, time_col, index_col = i, time_candidates[0], index_candidates[0]
            break
    if header_idx is None or time_col is None or index_col is None:
        preview = " / ".join(",".join(row[:8]) for row in rows[:3])
        raise ValueError(f"證交所盤中 CSV 找不到時間／加權指數欄位；回應開頭：{preview[:240]}")

    latest = None
    for row in rows[header_idx + 1:]:
        if len(row) <= max(time_col, index_col):
            continue
        clock = clean(row[time_col])
        price = num(clean(row[index_col]))
        if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", clock) or price is None or price <= 0:
            continue
        latest = {"date": expected_date, "time": clock[:8], "price": price}
    if latest is None:
        raise ValueError("證交所盤中資料沒有有效指數紀錄")
    return latest


def parse_twse_mis_intraday(payload: dict[str, Any], expected_date: str) -> dict[str, Any]:
    """解析 TWSE MIS 的上市加權指數即時報價（tse_t00.tw）。"""
    rows = payload.get("msgArray") or []
    if not rows:
        raise ValueError(
            f"MIS 未回傳 msgArray（rtcode={payload.get('rtcode')}；"
            f"rtmessage={payload.get('rtmessage') or payload.get('msg') or '無'}）"
        )
    row = next((x for x in rows if str(x.get("c", "")).lower() in ("t00", "tse_t00")), rows[0])
    raw_date = str(row.get("d") or "")
    date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if re.fullmatch(r"\d{8}", raw_date) else ""
    if date != expected_date:
        raise ValueError(f"MIS 指數日期不符：收到 {raw_date or '空值'}，預期 {expected_date}")
    clock = str(row.get("t") or "").strip()
    price = num(row.get("z"))
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock):
        raise ValueError(f"MIS 指數時間格式錯誤：{clock or '空值'}")
    if price is None or not 1000 < price < 100000:
        raise ValueError(f"MIS 指數價格無效：{row.get('z')!r}")
    return {"date": date, "time": clock, "price": price}


def parse_twse_mis_previous_close(payload: dict[str, Any], price: float) -> float | None:
    """MIS 的 y 欄位是昨收（官方參考價），用來計算盤中漲跌幅。

    數值缺漏或不合理（與現價相差超過 20%）時回傳 None，不影響報價本身是否可用。
    """
    rows = payload.get("msgArray") or []
    row = next((x for x in rows if str(x.get("c", "")).lower() in ("t00", "tse_t00")), rows[0] if rows else {})
    previous = num(row.get("y"))
    if previous is None or previous <= 0 or not 0.8 < price / previous < 1.2:
        return None
    return previous


def resolve_previous_close(live: dict[str, Any], turnover: dict[str, Any] | None,
                           old: dict[str, Any] | None, item: dict[str, Any], today: str) -> float | None:
    """盤中漲跌幅的昨收來源：MIS 自帶昨收優先，其次證交所正式收盤，最後才用舊資料。"""
    previous_close = live.get("previous_close")
    if previous_close is None and turnover and turnover.get("date") == today:
        previous_close = turnover.get("previous_index_close")
    if previous_close is None and old and old.get("date") < today:
        previous_close = old.get("close")
    if previous_close is None and item.get("date") < today:
        previous_close = item.get("close")
    return previous_close


def _http_response_diagnostic(response: requests.Response) -> str:
    """提供 HTTP 與原始回應摘要；JSON 解碼失敗時也可診斷。"""
    content = getattr(response, "content", b"") or b""
    if isinstance(content, bytes):
        byte_count = len(content)
        preview = content[:240].decode(
            getattr(response, "encoding", None) or "utf-8", errors="replace"
        )
    else:
        raw = str(content)
        byte_count = len(raw.encode("utf-8", errors="replace"))
        preview = raw[:240]
    preview = preview.replace("\r", " ").replace("\n", " / ")
    headers = getattr(response, "headers", {}) or {}
    return (
        f"HTTP {getattr(response, 'status_code', 'unknown')}；"
        f"Content-Type={headers.get('Content-Type', 'unknown')}；"
        f"回應位元組數={byte_count}；回應開頭：{preview[:240]}"
    )


def fetch_twse_intraday(now: dt.datetime) -> dict[str, Any]:
    """優先使用證交所 MIS 即時指數；舊 MI_5MINS_INDEX CSV 僅作備援。"""
    expected_date = now.strftime("%Y-%m-%d")
    headers = {
        "User-Agent": "Mozilla/5.0 market-tracker/1.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        "Accept": "application/json, text/plain, */*",
    }
    errors = []
    landing_note = ""
    session = requests.Session()
    try:
        try:
            # 取得 cookie 只是盡力而為：首頁可能回 404 或無法連線，但 API 本身不需要它
            # 也能回傳資料（2026-11-16 實測）。首頁失敗時只記錄，仍繼續呼叫 API。
            try:
                landing = retry(
                    session.get, "https://mis.twse.com.tw/stock/index.jsp",
                    headers=headers, timeout=20,
                )
                landing.raise_for_status()
            except Exception as exc:
                landing_note = f"MIS 首頁失敗（已略過，直接呼叫 API）：{type(exc).__name__}: {exc}"
            response = retry(
                session.get, TWSE_MIS_INDEX_URL,
                params={"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"},
                headers=headers, timeout=20,
            )
            diagnostic = _http_response_diagnostic(response)
            try:
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise RuntimeError(
                    f"MIS 回應處理失敗：{type(exc).__name__}: {exc}；{diagnostic}"
                ) from exc
            try:
                quote = parse_twse_mis_intraday(payload, expected_date)
                quote_time = dt.datetime.strptime(quote["time"], "%H:%M:%S").time()
                if not (dt.time(9, 0) <= quote_time <= dt.time(13, 30)):
                    raise ValueError(f"MIS 最後一筆資料時間不在正常交易時段：{quote['time']}")
            except Exception as exc:
                raise RuntimeError(
                    f"MIS 資料驗證失敗：{type(exc).__name__}: {exc}；{diagnostic}"
                ) from exc
            result = {**quote, "source": "證交所 MIS 即時指數", "checked_at": now.strftime("%Y-%m-%d %H:%M")}
            previous_close = parse_twse_mis_previous_close(payload, quote["price"])
            if previous_close is not None:
                result["previous_close"] = previous_close
            return result
        except Exception as exc:
            errors.append(f"MIS：{type(exc).__name__}: {exc}" + (f"；{landing_note}" if landing_note else ""))
    finally:
        session.close()

    try:
        response = retry(
            requests.get, TWSE_INTRADAY_URL,
            params={"response": "csv", "date": now.strftime("%Y%m%d")},
            headers={"User-Agent": "Mozilla/5.0 market-tracker/1.0"},
            timeout=25,
        )
        response.raise_for_status()
        quote = parse_twse_intraday_csv(response.text, expected_date)
        quote_time = dt.datetime.strptime(quote["time"], "%H:%M:%S" if len(quote["time"]) == 8 else "%H:%M").time()
        if not (dt.time(9, 0) <= quote_time <= dt.time(13, 30)):
            raise ValueError(f"CSV 最後一筆資料時間不在正常交易時段：{quote['time']}")
        return {**quote, "source": "證交所 MI_5MINS_INDEX（備援）", "checked_at": now.strftime("%Y-%m-%d %H:%M")}
    except Exception as exc:
        errors.append(f"CSV：{type(exc).__name__}: {exc}")
    raise RuntimeError("；".join(errors))


# ---------- 台指期（期交所開放資料；欄位名稱未實測，已做容錯） ----------
def num(x: Any) -> float | None:
    try:
        return float(re.sub(r"[,，%▼▲\s]", "", str(x)))
    except (TypeError, ValueError):
        return None


def signed_num(x: Any) -> float | None:
    """解析期交所漲跌幅；向下箭頭代表負值，不能被一般數字清理器吃掉方向。"""
    raw = str(x or "").strip()
    value = num(raw)
    if value is None:
        return None
    if "▼" in raw:
        return -abs(value)
    if "▲" in raw:
        return abs(value)
    return value


def pick(r: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if r.get(k) not in (None, "", "-"):
            return r[k]
    return None


def fut_records(by: dict) -> dict[str, list[dict[str, Any]]]:
    """台指期=只取一般盤；台指期（全）=只取夜盤，絕不合併兩個時段。"""
    out: dict[str, list[dict[str, Any]]] = {"TX": [], "TXALL": []}
    for d, sessions in sorted(by.items()):
        day, night = sessions.get("day"), sessions.get("night")
        if day and day.get("last") is not None:
            out["TX"].append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "m": day["m"], "close": day["last"], "oi": day.get("oi"),
                "chg_pct": day.get("chg_pct"), "high": day.get("high"),
                "low": day.get("low"), "vol": day.get("vol"), "session": "day",
                **_next_fields(day),
            })
        if night and night.get("last") is not None:
            out["TXALL"].append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "m": night["m"], "close": night["last"], "oi": night.get("oi"),
                "chg_pct": night.get("chg_pct"), "high": night.get("high"),
                "low": night.get("low"), "vol": night.get("vol"), "session": "night",
                **_next_fields(night),
            })
    return out


def _next_fields(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("next_m"):
        return {}
    return {"next_m": row["next_m"], "next_close": row.get("next_last"), "next_vol": row.get("next_vol")}



# ---------- 網頁報價（第三方網頁，非官方；可一鍵關閉） ----------
ENABLE_WEB_QUOTES = False  # 第三方網頁解析不穩定；期貨行情僅採期交所官方資料
WEB_SITES = {
    "TX": {"name": "玩股網", "url": "https://www.wantgoo.com/futures/wtx&"},       # 台指期（日盤）
    "TXALL": {"name": "Yahoo 奇摩", "url": "https://tw.stock.yahoo.com/future/WTX&"},  # 台指期（含夜盤）
}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-market-dashboard/1.0)", "Accept-Language": "zh-TW,zh;q=0.9"}
BLOCK_HINTS = ("captcha", "just a moment", "access denied", "cf-chl", "unusual traffic", "請驗證", "驗證您是人類")


def web_enabled() -> bool:
    return ENABLE_WEB_QUOTES and os.environ.get("WEB_QUOTES", "1") != "0"


def web_due(sym: str, now: dt.datetime) -> bool:
    """依你定的時間表：卡片 2＝台股交易時段每小時（含 14:07 收盤）；卡片 3＝16、18、22、00、02、04、06 點的 :07 那次。"""
    if now.minute >= 25:  # 只在每小時的 :07 那次抓；排程延遲太多就略過
        return False
    wd, h = now.weekday(), now.hour
    if sym == "TX":
        return wd < 5 and 9 <= h <= 14
    return (wd < 5 and h in (16, 18, 22)) or (1 <= wd <= 5 and h in (0, 2, 4, 6))


def html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", "\n", raw))
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def fetch_page(url: str) -> tuple[int, str, str]:
    """回傳 (狀態碼, 內容, 標記)。403/429 或驗證畫面視為被擋，不重試；逾時或 5xx 才重試一次。"""
    last: Exception | None = None
    for _ in range(2):
        try:
            r = requests.get(url, headers=WEB_HEADERS, timeout=20)
        except requests.RequestException as exc:
            last = exc
            time.sleep(3)
            continue
        if r.status_code in (403, 429):
            return r.status_code, r.text, "blocked"
        if r.status_code >= 500:
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(3)
            continue
        low = r.text[:6000].lower()
        if "wtx&" not in low and any(h in low for h in BLOCK_HINTS):
            return r.status_code, r.text, "blocked"
        return r.status_code, r.text, "ok"
    raise last or RuntimeError("無法連線")


def parse_wantgoo(text: str) -> dict[str, Any] | None:
    m = re.search(r"WTX&\s*([\d,]+(?:\.\d+)?)\s*([\d,]+(?:\.\d+)?)\s*([\d.]+)%", text)
    if not m:
        return None
    return {"price": num(m[1]), "pct_abs": num(m[3]), "ref": None, "quote_time": None}


def parse_yahoo(text: str) -> dict[str, Any] | None:
    m = re.search(r"WTX&\s*([\d,]+\.\d+)\s*([\d,]+\.\d+)\s*\(\s*([\d.]+)%\s*\)", text)
    if not m:
        return None
    ref = re.search(r"昨收\s*([\d,]+\.\d+)", text)
    t = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})\s*更新", text)
    return {"price": num(m[1]), "pct_abs": num(m[3]), "ref": num(ref[1]) if ref else None,
            "quote_time": f"{t[1]}-{t[2]}-{t[3]} {t[4]}:{t[5]}" if t else None}


def scrape_quote(sym: str, now: dt.datetime, ref_close: float | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """抓一次網頁報價。回傳 (報價或 None, 診斷紀錄)。報價時間一律是台灣時間 'YYYY-MM-DD HH:MM'。"""
    site = WEB_SITES[sym]
    att: dict[str, Any] = {"ts": now.strftime("%Y-%m-%d %H:%M"), "sym": sym, "site": site["name"], "url": site["url"]}
    try:
        status, body, flag = fetch_page(site["url"])
        att.update(http_status=status, bytes=len(body))
        if flag == "blocked":
            att.update(status="blocked", reason=f"網站拒絕自動存取（HTTP {status}），今天不再嘗試")
            return None, att
        text = html_text(body)
        i = text.find("WTX&")
        att["snippet"] = re.sub(r"\s+", " ", text[max(0, i - 30): i + 150]) if i >= 0 else ""
        parsed = (parse_wantgoo if sym == "TX" else parse_yahoo)(text)
        if not parsed or not parsed["price"]:
            att.update(status="parse_failed", reason="找不到報價欄位（網頁格式可能改變，或內容要用 JavaScript 才會載入）")
            return None, att
        price = parsed["price"]
        ref = parsed["ref"] or ref_close  # 網頁文字沒有正負號：用昨收（或我們已有的上一交易日收盤）判斷漲跌方向
        if not 10000 < price < 100000 or (ref and abs(price / ref - 1) > 0.2):
            att.update(status="parse_failed", reason=f"報價數字不合理（{price}），已捨棄")
            return None, att
        pct = None if not ref else round((1 if price >= ref else -1) * (parsed["pct_abs"] or 0), 2)
        qt = parsed["quote_time"] or now.strftime("%Y-%m-%d %H:%M")
        att.update(status="ok", price=price)
        return {"price": price, "chg_pct": pct, "quote_time": qt, "source": site["name"]}, att
    except Exception as exc:
        att.update(status="error", reason=f"{type(exc).__name__}: {exc}")
        return None, att


def write_probe(attempts: list[dict[str, Any]], updated: str) -> None:
    """診斷檔：最近 40 次網頁報價抓取的結果，抓不到時用來找原因。"""
    if not attempts:
        return
    path = os.path.join(os.path.dirname(OUT), "fut_probe.json")
    old: list = []
    try:
        old = json.load(open(path, encoding="utf-8")).get("attempts", [])
    except Exception:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump({"updated": updated, "attempts": (old + attempts)[-40:]}, handle, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def web_newer(sym: str, q: dict[str, Any], last_date: str) -> bool:
    """網頁報價是否比期交所最新的日盤收盤更新。卡片 2 看日期；卡片 3 看是否晚於當日 13:45 日盤收盤。"""
    qt = q.get("quote_time") or ""
    return bool(qt) and (qt[:10] > last_date if sym == "TX" else qt > f"{last_date} 13:45")


# ---------- 台指期新鮮度模型 ----------
# 第一性原理：卡片要回答的只有一個問題——「畫面上的數字，是不是期交所目前已公布的最新一筆？」
# 期交所 DailyMarketReportFut 在交易日 D 的「隔天早上」才公布 D（實測 07:46、08:59，也曾延到下午），
# 從不在 D 當天公布。所以：
#   預期日期 = 最近一個 07:00 所在日的前一個工作日（從不預期當天）
#   手上最新日期 < 預期日期 → 每次排程都去查一次（不再限制時段）；已追上 → 不查。
#   狀態由「日期落後幾個交易日」與「這次請求是否出錯」決定，兩者分開。
FUT_PUBLISH_FROM = dt.time(7, 0)
# 「過久未更新」門檻（落後幾個交易日）。有交易日曆時為 2；日曆無法載入或日期超出日曆範圍時，
# 只能用平日近似（國定假日會被多算一天），門檻放寬為 3，避免假日後誤報紅字。
FUT_OVERDUE_DAYS = 2
FUT_OVERDUE_DAYS_APPROX = 3
# 落後 1 天時，連續失敗達此次數才變紅；單次暫時性錯誤（期交所 API 常見）仍顯示「尚未公布」。
FUT_FAIL_STREAK_RED = 2
FUT_STATUS_LABEL = {
    "success": "最新", "waiting": "尚未公布", "overdue": "過久未更新",
    "failed": "抓取失敗", "unavailable": "無資料",
}


# ---------- 台股交易日曆 ----------
# 期交所與證交所休市日相同。來源：exchange_calendars 的 XTAI（台灣證券交易所）日曆，
# 涵蓋已公告的國定假日、補假與春節封關前的「僅辦理結算交割、無交易」日。
# 颱風等臨時休市不在其中，寫在 config/tw_extra_closed.json（可直接在 GitHub 網頁上編輯）。
# 未來年度的假日是函式庫依規則推算；每年人事行政總處公告辦公日曆後，請核對 XTAI 的休市日，
# 核對完成後把年份加進 TW_CALENDAR_VERIFIED_YEARS。未核對的年份會在 Actions 顯示警告。
TW_EXTRA_CLOSED_FILE = os.path.join(os.path.dirname(__file__), "config", "tw_extra_closed.json")
TW_CALENDAR_VERIFIED_YEARS = frozenset({2026})


def load_extra_closed(path: str = TW_EXTRA_CLOSED_FILE) -> frozenset[dt.date]:
    """讀取臨時休市日。格式：[{"date": "YYYY-MM-DD", "reason": "..."}]。檔案不存在視為沒有；
    格式錯誤的項目略過並警告，不中斷抓取。"""
    try:
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
    except FileNotFoundError:
        return frozenset()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning title=臨時休市設定::無法讀取 {os.path.basename(path)}：{exc}")
        return frozenset()
    days = set()
    for entry in entries if isinstance(entries, list) else []:
        try:
            days.add(dt.date.fromisoformat(str(entry["date"] if isinstance(entry, dict) else entry)))
        except (KeyError, ValueError):
            print(f"::warning title=臨時休市設定::略過格式錯誤的項目：{entry!r}")
    return frozenset(days)


TW_EXTRA_CLOSED: frozenset[dt.date] = load_extra_closed()


class TradingCalendar:
    def __init__(self, holidays: frozenset[dt.date] = frozenset(), valid_from: dt.date | None = None,
                 valid_until: dt.date | None = None, source: str = "平日近似（未載入交易日曆）"):
        self.holidays = frozenset(holidays) | TW_EXTRA_CLOSED
        self.valid_from, self.valid_until, self.source = valid_from, valid_until, source

    def covers(self, day: dt.date) -> bool:
        """這一天是否在交易日曆範圍內（範圍外只能用平日近似）。"""
        return bool(self.valid_from and self.valid_until and self.valid_from <= day <= self.valid_until)

    def is_trading_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def previous_trading_day(self, day: dt.date) -> dt.date:
        day -= dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day -= dt.timedelta(days=1)
        return day

    def next_trading_day(self, day: dt.date) -> dt.date:
        day += dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day += dt.timedelta(days=1)
        return day

    def trading_days_between(self, lo: dt.date, hi: dt.date) -> int:
        """(lo, hi] 之間有幾個交易日。"""
        count, day = 0, hi
        while day > lo:
            if self.is_trading_day(day):
                count += 1
            day -= dt.timedelta(days=1)
        return count


def load_tw_calendar() -> TradingCalendar:
    try:
        import importlib.metadata
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XTAI")
        start, end = cal.first_session.date(), cal.last_session.date()
        sessions = {ts.date() for ts in cal.sessions}
        holidays = frozenset(
            start + dt.timedelta(days=n) for n in range((end - start).days + 1)
            if (start + dt.timedelta(days=n)).weekday() < 5 and start + dt.timedelta(days=n) not in sessions
        )
        version = importlib.metadata.version("exchange_calendars")
        return TradingCalendar(holidays, start, end, f"exchange_calendars {version} XTAI（至 {end}）")
    except Exception as exc:  # 沒裝套件或日曆資料異常：退回平日近似，不中斷抓取
        print(f"::warning title=交易日曆::無法載入交易日曆，改用平日近似：{type(exc).__name__}: {exc}")
        return TradingCalendar()


_TW_CAL: TradingCalendar | None = None


def tw_calendar() -> TradingCalendar:
    global _TW_CAL
    if _TW_CAL is None:
        _TW_CAL = load_tw_calendar()
    return _TW_CAL


def calendar_warnings(expected: str | None) -> list[str]:
    cal = tw_calendar()
    if not cal.covers(dt.date.today()):
        return []  # 已在載入時警告過
    if expected and dt.date.fromisoformat(expected).year not in TW_CALENDAR_VERIFIED_YEARS:
        return [f"{expected[:4]} 年的休市日尚未人工核對（{cal.source}）；請對照人事行政總處辦公日曆，"
                f"核對後加入 TW_CALENDAR_VERIFIED_YEARS。"]
    return []


def overdue_threshold(expected: str | None) -> int:
    cal = tw_calendar()
    if expected and cal.covers(dt.date.fromisoformat(expected)):
        return FUT_OVERDUE_DAYS
    return FUT_OVERDUE_DAYS_APPROX


def expected_futures_dates(now: dt.datetime) -> dict[str, str]:
    """目前「最晚可能已公布」的期交所交易日期（日盤、夜盤相同）。

    交易日 D 的資料最早在 D+1 07:00 後出現，所以只要現在已過 07:00，
    預期日期就是今天之前的最後一個交易日；07:00 前則再往前推一天。休市日依交易日曆跳過。
    """
    local = now.astimezone(TZ_TAIPEI)
    ref = local.date() if local.time() >= FUT_PUBLISH_FROM else local.date() - dt.timedelta(days=1)
    expected = tw_calendar().previous_trading_day(ref).isoformat()
    return {"TX": expected, "TXALL": expected}


def trading_days_behind(latest: str | None, expected: str) -> int | None:
    """latest 落後 expected 幾個交易日（0 表示已追上）。"""
    if not latest:
        return None
    return tw_calendar().trading_days_between(dt.date.fromisoformat(latest), dt.date.fromisoformat(expected))


# ---------- 台指期換月 ----------
def contract_last_trading_day(month: str) -> dt.date:
    """台指期月契約最後交易日：交割月份第三個星期三；遇休市順延至次一交易日。

    換月判斷本身是資料驅動的（近月合約代碼改變），此日期只用於畫面提示。
    """
    year, mon = int(month[:4]), int(month[4:6])
    first = dt.date(year, mon, 1)
    third_wednesday = first + dt.timedelta(days=(2 - first.weekday()) % 7 + 14)
    cal = tw_calendar()
    return third_wednesday if cal.is_trading_day(third_wednesday) else cal.next_trading_day(third_wednesday)


def continuous_futures(hist: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把近月歷史接成連續序列（差價後調法，back-adjusted）。

    原始歷史不修改；回傳調整後的副本與換月紀錄。換月時，用舊合約最後一天同時記錄到的
    次近月價格算出價差（新 − 舊），把更早的價格整段平移，讓支撐與走勢圖不因換月跳動。
    缺少價差時無法調整，序列就從換月後重新開始，絕不把兩個合約的價格直接接在一起。
    """
    if not hist:
        return [], []
    out: list[dict[str, Any]] = []
    rolls: list[dict[str, Any]] = []
    adj = 0.0
    current = hist[-1].get("m")
    for i in range(len(hist) - 1, -1, -1):
        row = hist[i]
        if row.get("m") != current:
            roll = {"from": row.get("m"), "to": current, "date": hist[i + 1]["date"], "last_old_date": row["date"]}
            spread, spread_date = _roll_spread(hist, i, current)
            if spread is None:
                rolls.append({**roll, "spread": None, "spread_date": None, "adjusted": False})
                break
            adj += spread
            rolls.append({**roll, "spread": round(spread, 2), "spread_date": spread_date, "adjusted": True})
            current = row.get("m")
        shifted = dict(row)
        for key in ("close", "high", "low"):
            if shifted.get(key) is not None:
                shifted[key] = round(shifted[key] + adj, 2)
        out.append(shifted)
    out.reverse()
    return out, rolls


FUT_SPREAD_LOOKBACK = 5
# 量比的分母至少要有幾天「同一合約」的成交量；與支撐的最少天數一致。不足時不算、不標「有效」。
FUT_VOL_MIN_DAYS = 5  # 換月前最後一天缺少價差時，最多往前找幾筆舊合約資料


def _roll_spread(hist: list[dict[str, Any]], i: int, new_month: str | None) -> tuple[float | None, str | None]:
    """換月價差（新合約 − 舊合約）。優先用舊合約最後一天；缺少時往前找最近一筆同時記錄到
    兩個合約價格的資料（最多 FUT_SPREAD_LOOKBACK 筆）。相鄰月份的價差主要反映持有成本，
    數日內變動有限，用前幾天的價差遠比整段丟掉歷史合理；提示文字會註明實際使用的日期。"""
    old_month = hist[i].get("m")
    for j in range(i, max(i - FUT_SPREAD_LOOKBACK, -1), -1):
        row = hist[j]
        if row.get("m") != old_month:
            break
        if row.get("next_m") == new_month and row.get("next_close") is not None and row.get("close") is not None:
            return row["next_close"] - row["close"], row["date"]
    return None, None


def roll_note(rolls: list[dict[str, Any]], series_len: int, window: list[dict[str, Any]]) -> str | None:
    shown = {x["date"] for x in window}
    latest = next((r for r in rolls if r["date"] in shown or not r["adjusted"]), None)
    if not latest:
        return None
    if latest["adjusted"]:
        basis = latest.get("spread_date") or latest["last_old_date"]
        fallback = (f"（{latest['last_old_date']} 缺少價差資料，改用最近一筆）"
                    if basis != latest["last_old_date"] else "")
        return (f"{latest['date']} 起近月改為 {latest['to']} 合約；換月前的價格已依 {basis} "
                f"兩合約價差（{latest['spread']:+,.0f} 點）調整{fallback}，支撐與走勢圖為連續序列。")
    return (f"{latest['date']} 起近月改為 {latest['to']} 合約；缺少換月當天的兩合約價差，"
            f"支撐與量比改用新合約重新累積（{series_len}/60 天）。")


def expiry_note(last: dict[str, Any]) -> str | None:
    month = str(last.get("m") or "")
    if len(month) != 6 or not month.isdigit():
        return None
    expiry = contract_last_trading_day(month)
    data_day = dt.date.fromisoformat(last["date"])
    nxt = f"改以 {last['next_m']} 合約為近月" if last.get("next_m") else "改以下一月份合約為近月"
    if data_day == expiry:
        return f"此為 {month} 合約最後交易日的資料；下一筆起{nxt}。"
    if data_day < expiry and tw_calendar().trading_days_between(data_day, expiry) <= 3:
        return f"近月合約 {month} 將於 {expiry.month}/{expiry.day} 到期，之後{nxt}，價格可能出現價差。"
    return None


def futures_fetch_due(hist: dict[str, Any], now: dt.datetime) -> bool:
    """任何一個時段的最新資料還沒追上預期日期，就需要查詢期交所。"""
    expected = expected_futures_dates(now)
    for key in ("TX", "TXALL"):
        rows = (hist or {}).get(key) or []
        if key == "TXALL":
            rows = [x for x in rows if x.get("session") == "night"]
        latest = rows[-1].get("date") if rows else None
        if not latest or latest < expected[key]:
            return True
    return False


def quote_age_hours(qt: str, now: dt.datetime | None) -> float:
    if now is None:
        return 0.0
    return (now.replace(tzinfo=None) - dt.datetime.strptime(qt, "%Y-%m-%d %H:%M")).total_seconds() / 3600


def _fut_status(latest: str | None, expected: str | None, fetch_error: str | None,
                fail_streak: int) -> tuple[str, int | None]:
    """狀態只由兩件事決定：資料落後幾個交易日，以及連續查詢失敗幾次。"""
    if not latest:
        return ("failed" if fetch_error else "unavailable"), None
    lag = (trading_days_behind(latest, expected) or 0) if expected else 0
    if lag == 0:
        return "success", 0
    threshold = overdue_threshold(expected)
    if fetch_error and (fail_streak >= FUT_FAIL_STREAK_RED or lag >= threshold):
        return "failed", lag
    return ("overdue" if lag >= threshold else "waiting"), lag


def _fut_note(status: str, latest: str | None, expected: str | None, lag: int | None,
              fetch_error: str | None) -> str | None:
    if status == "waiting" and fetch_error:
        return f"期交所尚未提供 {expected} 資料（本次查詢暫時失敗），目前顯示 {latest}；下次排程會再試。"
    if status == "waiting":
        return f"期交所尚未公布 {expected} 資料，目前顯示 {latest}；之後每次排程都會自動再檢查。"
    if status == "overdue":
        return f"已落後 {lag} 個交易日沒有新資料（最新 {latest}，預期 {expected}），請留意。"
    if status == "failed":
        return f"期交所連線或解析失敗，顯示上一筆資料（{latest or '無'}）。"
    if status == "unavailable":
        return "尚無可用的期交所官方資料。"
    return None


def fut_item(sym: str, name: str, hist: list[dict[str, Any]], now: dt.datetime | None = None,
             fetch_error: str | None = None, *, attempted: bool = True,
             checked_at: str | None = None, fail_streak: int = 0) -> dict[str, Any]:
    """建立台指期卡片。狀態由資料落後天數與連續失敗次數決定（見 _fut_status）。"""
    expected = expected_futures_dates(now).get(sym) if now is not None else None
    latest = hist[-1]["date"] if hist else None
    status, lag = _fut_status(latest, expected, fetch_error, fail_streak)
    note = _fut_note(status, latest, expected, lag, fetch_error)
    stale = status in ("failed", "overdue", "unavailable")
    common = {
        "data_status": status, "status_label": FUT_STATUS_LABEL[status], "status_note": note,
        "expected_date": expected, "days_behind": lag,
        "checked_at": checked_at, "checked_this_run": attempted, "fail_streak": fail_streak,
        "publish_note": "官方資料於下一個交易日公布，非即時報價",
    }
    # fetch_error 只描述「目前狀態不是最新」的原因；資料已是最新時不留錯誤，避免 success 卡片帶著錯誤。
    if fetch_error and status != "success":
        common["fetch_error"] = fetch_error
    if not hist:
        return {"sym": sym, "name": name, "error": note, "stale": True, **common}
    last = hist[-1]
    series, rolls = continuous_futures(hist)
    prior = series[:-1][-SUPPORT_DAYS:]
    lows = [x["low"] for x in prior if x.get("low") is not None]
    # 量比只比同一個合約：換月前後兩個合約的成交量不可比（舊合約到期前量會萎縮）。
    vols = [x["vol"] for x in prior[-20:] if x.get("vol") and x.get("m") == last.get("m")]
    vol_ready = len(vols) >= FUT_VOL_MIN_DAYS
    is_night = sym == "TXALL"
    session_text = "最近夜盤收盤" if is_night else "最近日盤收盤"
    if is_night and now is not None:
        local_time = now.timetz().replace(tzinfo=None)
        if local_time >= dt.time(15, 0) or local_time < dt.time(5, 0):
            session_text = "夜盤時段"
    item = {
        "sym": sym, "name": "台指期（全）・夜盤" if is_night else "台指期（近月）・日盤",
        "date": last["date"], "close": last["close"], "chg": last.get("chg_pct"),
        "oi": last.get("oi"), "volume": last.get("vol"), "volume_unit": "口",
        "support": None, "dist": None, "vol_ratio": None,
        "volume_status": "累積中", "level": "ok", "stale": stale,
        "history_days": len(series), "spark": [x["close"] for x in series[-SUPPORT_DAYS:]],
        "contract": last.get("m"), "rolls": rolls[:3],
        "contract_note": " ".join(n for n in (
            roll_note(rolls, len(series), series[-SUPPORT_DAYS:]), expiry_note(last)) if n) or None,
        "price_source": "期交所官方・夜盤" if is_night else "期交所官方・日盤",
        "price_time": last["date"], "session": session_text,
        "session_class": "open" if session_text == "夜盤時段" else "closed",
        "last_success_date": last["date"],
        **common,
    }
    if len(lows) >= 5:
        item["support"] = min(lows)
        item["dist"] = round((last["close"] / item["support"] - 1) * 100, 1)
        if vol_ready and last.get("vol") and not stale:
            item["vol_ratio"] = round(last["vol"] / (sum(vols) / len(vols)), 2)
            item["volume_status"] = "有效"
        elif stale:
            item["volume_status"] = "資料過舊"
        elif not vol_ready:
            prefix = "換月後累積中" if rolls else "累積中"
            item["volume_status"] = f"{prefix}（{len(vols)}/{FUT_VOL_MIN_DAYS}）"
        else:
            item["volume_status"] = "盤中不顯示"
        item["text"], item["level"] = judge(last["close"], item["support"], item["dist"], item["vol_ratio"])
    else:
        item["text"] = f"歷史資料累積中（{len(series)}/60 天）"
    if stale:
        item["text"] = note
    if is_night:
        item["session_note"] = (
            "此卡只顯示期交所已公布的最近夜盤資料，不含日盤；"
            "依期交所規則，夜盤歸屬「下一個交易日」，例如週一的夜盤資料其實是上週五晚上的交易。"
        )
    return item


def tx_rows() -> dict[str, dict[str, dict[str, Any]]]:
    """{YYYYMMDD: {"day": 近月一般盤, "night": 近月盤後}}；回應異常時拋出含 HTTP 診斷的錯誤。

    每一列另外附上「次近月」合約的價格（next_m／next_last／next_vol）。換月時需要同一天
    兩個合約的價格才能算出價差，把舊合約的歷史接成連續序列（見 continuous_futures）。
    """
    resp = retry(requests.get, FUT_URL, timeout=30)
    diagnostic = _http_response_diagnostic(resp)
    try:
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        raise RuntimeError(f"期交所回應處理失敗：{type(exc).__name__}: {exc}；{diagnostic}") from exc
    if not isinstance(rows, list):
        raise RuntimeError(f"期交所回應不是清單；{diagnostic}")
    by: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r, dict) or pick(r, "Contract", "契約") not in ("TX", "臺股期貨"):
            continue
        m = str(pick(r, "ContractMonth(Week)", "到期月份(週別)") or "")
        d = re.sub(r"\D", "", str(pick(r, "Date", "日期") or ""))
        if not (m.isdigit() and len(m) == 6 and len(d) == 8):
            continue  # 排除週契約與格式不明的列
        sess = str(pick(r, "TradingSession", "交易時段") or "")
        k = "night" if ("盤後" in sess or "after" in sess.lower()) else "day"
        row = {"m": m, "high": num(pick(r, "High", "最高價")), "low": num(pick(r, "Low", "最低價")),
               "last": num(pick(r, "Last", "最後成交價")), "chg_pct": signed_num(pick(r, "%", "漲跌%")),
               "vol": num(pick(r, "Volume", "成交量")), "oi": num(pick(r, "OpenInterest", "未沖銷契約量"))}
        by.setdefault(d, {}).setdefault(k, {})[m] = row
    if not by:
        first = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else "空"
        raise RuntimeError(f"期交所回應中沒有可解析的台指期資料（共 {len(rows)} 列，第一列欄位：{first}）")
    return {d: {k: near_with_next(months) for k, months in sessions.items()} for d, sessions in by.items()}


def near_with_next(months: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """從同一天、同一時段的各月份合約中取近月，並附上次近月的價格。"""
    ordered = sorted(months)
    near = dict(months[ordered[0]])
    if len(ordered) > 1:
        nxt = months[ordered[1]]
        near.update({"next_m": ordered[1], "next_last": nxt.get("last"), "next_vol": nxt.get("vol")})
    return near


def update_futures(hist: dict[str, Any], fetch: bool = True, now: dt.datetime | None = None,
                   previous_items: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, list], list[dict[str, Any]]]:
    hist = {k: list(v) for k, v in (hist or {}).items()}
    # 舊版 TXALL 是日夜盤混合值，沒有 session 標記，不能再當作夜盤歷史。
    hist["TXALL"] = [x for x in hist.get("TXALL", []) if x.get("session") == "night"]
    error: str | None = None
    checked_now = now.strftime("%Y-%m-%d %H:%M") if now is not None else None
    if fetch:
        try:
            records = fut_records(tx_rows())
            for key in ("TX", "TXALL"):
                if records.get(key):
                    merged = {x["date"]: x for x in hist.get(key, [])}
                    merged.update({x["date"]: x for x in records[key]})
                    hist[key] = [merged[d] for d in sorted(merged)][-130:]
        except Exception as exc:
            error = str(exc)
            print(f"[台指期] 查詢失敗：{exc}")

    items = []
    previous_items = previous_items or {}
    for key, name in (("TX", "台指期（近月）"), ("TXALL", "台指期（全）・夜盤")):
        previous = previous_items.get(key) or {}
        # 連續失敗次數：本次查詢失敗就累加，查詢成功歸零；沒查詢代表資料已是最新，也歸零。
        fail_streak = int(previous.get("fail_streak") or 0) + 1 if (fetch and error) else 0
        items.append(fut_item(
            key, name, hist.get(key, []), now,
            fetch_error=error, attempted=fetch,
            checked_at=checked_now if fetch else previous.get("checked_at"),
            fail_streak=fail_streak,
        ))
    return hist, items


def futures_log_line(item: dict[str, Any]) -> str:
    """GitHub Actions 日誌用：每次都說清楚台指期這一輪有沒有查、結果是什麼。"""
    name = "台指期近月" if item.get("sym") == "TX" else "台指期夜盤"
    status, latest, expected = item.get("data_status"), item.get("date"), item.get("expected_date")
    if not item.get("checked_this_run"):
        reason = "已是最新" if status == "success" else "資料未追上，但本次未查詢"
        return f"[{name}] 本次未查詢（{reason}；資料 {latest}，預期 {expected}；上次查詢 {item.get('checked_at') or '無'}）"
    if status == "success" and item.get("fail_streak"):
        return f"[{name}] 已查詢但失敗；手上資料 {latest} 已是最新，不影響顯示"
    if status == "success":
        return f"[{name}] 已查詢：取得 {latest} 資料 ✓"
    if status == "failed":
        return f"[{name}] 查詢失敗（連續 {item.get('fail_streak')} 次）：{item.get('fetch_error')}"
    if item.get("fail_streak"):
        return (f"[{name}] 查詢失敗（第 {item.get('fail_streak')} 次，暫不變紅）；"
                f"最新日期 {latest}，預期 {expected} → {item.get('status_label')}：{item.get('fetch_error')}")
    return f"[{name}] 已查詢：API 正常，最新日期 {latest}，預期 {expected} → {item.get('status_label')}"


def analyse(sym: str, name: str, has_vol: bool, override: dict[str, Any] | None = None,
            kind: str = "us", is_open: bool = False) -> dict[str, Any]:
    errors = []
    df = None
    provider = None
    for source in (from_yahoo, from_stooq):
        if source is from_stooq and sym not in STOOQ:
            continue
        try:
            candidate = retry(source, sym)
            if candidate is not None and len(candidate) >= SUPPORT_DAYS + 2:
                df = candidate
                provider = "Yahoo Finance" if source is from_yahoo else "Stooq"
                break
            errors.append(f"{source.__name__}: 歷史資料不足")
        except Exception as exc:  # keep trying the fallback source
            errors.append(f"{source.__name__}: {exc}")

    if df is None:
        raise RuntimeError("；".join(errors) or "沒有可用行情來源")

    df = df.sort_index()
    yahoo_source_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    yahoo_source_close = float(df["Close"].iloc[-1])
    # 收盤後以證交所 FMTQIK 的正式指數收盤價覆蓋 Yahoo 日線；技術指標也使用同一正式收盤值。
    if sym == "^TWII" and override and override.get("index_close") is not None and override.get("date"):
        official_date = pd.Timestamp(override["date"])
        if getattr(df.index, "tz", None) is not None:
            official_date = official_date.tz_localize(df.index.tz)
        official_day = official_date.date()
        latest_day = pd.Timestamp(df.index[-1]).date()
        if official_day >= latest_day:
            date_matches = [pd.Timestamp(x).date() == official_day for x in df.index]
            if any(date_matches):
                df.loc[date_matches, "Close"] = float(override["index_close"])
                df.loc[date_matches, "Low"] = float(override["index_close"])
            else:
                new_row = df.iloc[-1].copy()
                new_row["Close"] = float(override["index_close"])
                new_row["Low"] = float(override["index_close"])
                new_row["Volume"] = float("nan")
                df.loc[official_date] = new_row
                df = df.sort_index()
    last, prev = df.iloc[-1], df.iloc[-2]
    hist = df.iloc[-1 - SUPPORT_DAYS:-1]
    support = float(hist["Low"].min())
    ma60 = float(df["Close"].tail(60).mean())
    close, previous_close = float(last["Close"]), float(prev["Close"])
    if support <= 0 or ma60 <= 0 or previous_close <= 0:
        raise ValueError("行情數值異常，無法計算指標")

    dist = (close / support - 1) * 100
    one_year_high = float(df["Close"].max())
    drawdown = max(0.0, (one_year_high - close) / one_year_high * 100)

    # Treat zero/missing provider volume as unavailable, never as a genuine 0x ratio.
    vol_ratio, volume, unit, volume_note = None, None, "", ""
    intraday = is_open and kind in ("tw", "us")  # 盤中的量只有半天，不拿來算量比
    if has_vol and "Volume" in df and not intraday:
        volumes = pd.to_numeric(df["Volume"], errors="coerce")
        vi = len(df) + (-2 if kind == "24h" else -1)  # 24 小時商品最新一根未完成，改用前一根
        current_volume = volumes.iloc[vi]
        prior_volumes = volumes.iloc[vi - 20:vi].where(volumes.iloc[vi - 20:vi] > 0).dropna()
        if pd.notna(current_volume) and current_volume > 0 and not prior_volumes.empty:
            avg_volume = float(prior_volumes.mean())
            if avg_volume > 0:
                vol_ratio = float(current_volume / avg_volume)
                volume, unit = float(current_volume), "口"
                volume_note = "前一交易日" if kind == "24h" else ""

    last_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    if override and override.get("date") == last_date and not intraday:  # 加權指數：改用證交所成交金額
        vol_ratio, volume, unit = override["ratio"], override["amount_yi"], "億元"
        volume_note = "證交所每日市場成交資訊口徑"

    if close < support:
        text, level = ("盤中低於近 60 日支撐，待收盤確認" if is_open else "收盤低於近 60 日支撐，留意後續是否站回"), "break"
    elif dist <= NEAR:
        extra = "量縮" if vol_ratio is not None and vol_ratio < 0.8 else "放量" if vol_ratio is not None and vol_ratio > 1.5 else ""
        text, level = "接近近 60 日支撐" + (f"，{extra}" if extra else ""), "near"
    elif dist > FAR:
        text, level = "距近 60 日支撐尚遠", "ok"
    else:
        text, level = "與近 60 日支撐有一段距離", "ok"

    # Drawdown is a descriptive risk statistic, not an entry/buy signal.
    if drawdown >= 25:
        drawdown_status = "回落超過 25%（高回落觀察）"
    elif drawdown >= 15:
        drawdown_status = "回落超過 15%（回落觀察）"
    else:
        drawdown_status = "未達 15% 回落門檻"

    result = {
        "sym": sym, "name": name,
        "date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "chg": round((close / previous_close - 1) * 100, 2),
        "support": round(support, 2), "dist": round(dist, 1),
        "ma60": round(ma60, 2), "vs_ma60": round((close / ma60 - 1) * 100, 1),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
        "volume": volume, "volume_unit": unit,
        "volume_note": volume_note,
        "volume_status": ("有效" if vol_ratio is not None
                          else "盤中不顯示" if intraday and (has_vol or sym == "^TWII")
                          else "尚無成交金額資料" if sym == "^TWII"
                          else "不適用" if not has_vol else "無有效成交量資料"),
        "drawdown": round(drawdown, 1), "drawdown_status": drawdown_status,
        "text": text, "level": level,
        "spark": [round(float(x), 2) for x in df["Close"].tail(60)],
        "stale": False,
        "source": ("證交所官方收盤（FMTQIK）" if sym == "^TWII" and override
                   and override.get("date") == pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
                   else provider or "公開日線行情"),
        "source_date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        "yahoo_close": round(yahoo_source_close, 2) if sym == "^TWII" else None,
        "yahoo_date": yahoo_source_date if sym == "^TWII" else None,
    }
    result["technical"] = technical_analysis(df)
    return result


def _trend_label(close: float, fast: float | None, slow: float | None) -> str:
    if fast is None or slow is None: return "資料不足"
    if close > fast > slow: return "多頭"
    if close < fast < slow: return "空頭"
    if close > fast and fast <= slow: return "轉強觀察"
    if close < fast and fast >= slow: return "轉弱觀察"
    return "盤整"


def _period_snapshot(series: pd.Series, fast_n: int, slow_n: int) -> dict[str, Any]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty: return {"trend":"資料不足","close":None,"fast_ma":None,"slow_ma":None,"bars":0}
    close = float(clean.iloc[-1])
    fast = float(clean.tail(fast_n).mean()) if len(clean) >= fast_n else None
    slow = float(clean.tail(slow_n).mean()) if len(clean) >= slow_n else None
    return {"trend":_trend_label(close,fast,slow),"close":round(close,2),
            "fast_ma":round(fast,2) if fast is not None else None,
            "slow_ma":round(slow,2) if slow is not None else None,"bars":int(len(clean))}


def technical_analysis(df: pd.DataFrame) -> dict[str, Any]:
    close = pd.to_numeric(df["Close"],errors="coerce").dropna().sort_index()
    if len(close) < 35: return {"status":"insufficient","message":"有效日線少於 35 筆，技術指標資料不足"}
    weekly, monthly = close.resample("W-FRI").last().dropna(), close.resample("ME").last().dropna()
    delta=close.diff(); gains=delta.clip(lower=0); losses=-delta.clip(upper=0)
    ag=gains.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    al=losses.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=ag/al.replace(0,float("nan")); rsi=100-(100/(1+rs))
    rsi_value=round(float(rsi.iloc[-1]),1) if pd.notna(rsi.iloc[-1]) else (100.0 if pd.notna(ag.iloc[-1]) and float(ag.iloc[-1])>0 and float(al.iloc[-1])==0 else None)
    e12=close.ewm(span=12,adjust=False).mean(); e26=close.ewm(span=26,adjust=False).mean()
    ml=e12-e26; sl=ml.ewm(span=9,adjust=False).mean(); hist=ml-sl
    ma20=float(close.tail(20).mean()); ma60=float(close.tail(60).mean()) if len(close)>=60 else None
    ma120=float(close.tail(120).mean()) if len(close)>=120 else None
    mom=(float(close.iloc[-1])/float(close.iloc[-21])-1)*100 if len(close)>=21 else None
    daily=_period_snapshot(close,20,60); daily["trend"]=_trend_label(float(close.iloc[-1]),ma20,ma60)
    return {"status":"success","daily":daily,"weekly":_period_snapshot(weekly,12,26),
            "monthly":_period_snapshot(monthly,3,6),"rsi14":rsi_value,
            "macd":{"line":round(float(ml.iloc[-1]),3),"signal":round(float(sl.iloc[-1]),3),
                    "histogram":round(float(hist.iloc[-1]),3),
                    "direction":"偏多" if hist.iloc[-1]>0 else "偏空" if hist.iloc[-1]<0 else "中性"},
            "ma20":round(ma20,2),"ma60":round(ma60,2) if ma60 is not None else None,
            "ma120":round(ma120,2) if ma120 is not None else None,
            "momentum20":round(mom,2) if mom is not None else None,"bars":int(len(close))}


def build_market_analysis(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol={x.get("sym"):x for x in items}
    selected=[by_symbol[s] for s in MARKET_ANALYSIS_SYMBOLS if s in by_symbol]  # 固定順序；其餘標的不納入
    core=[x for x in selected if not x.get("stale")
          and not x.get("technical_stale")
          and x.get("technical",{}).get("status")=="success"]
    scores=[]
    for item in core:
        t=item["technical"]; score=0
        score += 25 if t.get("ma60") is not None and item.get("close",0)<t["ma60"] else 0
        score += 20 if t.get("ma20") is not None and t.get("ma60") is not None and t["ma20"]<t["ma60"] else 0
        score += 20 if t.get("rsi14") is not None and t["rsi14"]<40 else 0
        score += 20 if t.get("macd",{}).get("histogram") is not None and t["macd"]["histogram"]<0 else 0
        score += 15 if t.get("momentum20") is not None and t["momentum20"]<0 else 0
        scores.append({"sym":item["sym"],"name":item["name"],"score":score})
    risk=round(sum(x["score"] for x in scores)/len(scores)) if len(scores)>=2 else None
    if risk is None: level,summary="資料不足","主要股價指數有效資料不足，暫不計算市場風險分數。"
    elif risk>=60: level,summary="警戒","主要股價指數的技術面風險訊號偏多，宜提高風險控管。"
    elif risk>=30: level,summary="注意","市場出現部分偏弱訊號，宜留意趨勢是否持續惡化。"
    else: level,summary="一般","目前主要股價指數的技術面風險訊號相對有限。"
    assets=[]
    for item in selected:
        t=item.get("technical")
        if not t or t.get("status")!="success": continue
        assets.append({"sym":item.get("sym"),"name":item.get("name"),
            "date":item.get("technical_date") if item.get("technical_stale") else item.get("date"),
            "close":item.get("close"),"chg":item.get("chg"),"trend":t.get("daily",{}).get("trend","資料不足"),
            "weekly":t.get("weekly",{}).get("trend","資料不足"),"monthly":t.get("monthly",{}).get("trend","資料不足"),
            "rsi14":t.get("rsi14"),"macd_direction":t.get("macd",{}).get("direction"),
            "momentum20":t.get("momentum20"),"stale":bool(item.get("stale") or item.get("technical_stale")),
            "source":item.get("source","公開日線行情"),
            "intraday":bool(item.get("captured_open")),  # 最新一根是盤中抓的，尚未收盤
            "technical_date":item.get("technical_date"),
            "ma20":t.get("ma20"),"ma60":t.get("ma60"),
            "vs_ma20":round((item["close"]/t["ma20"]-1)*100,1) if item.get("close") and t.get("ma20") else None,
            "vs_ma60":round((item["close"]/t["ma60"]-1)*100,1) if item.get("close") and t.get("ma60") else None,
            "crosscheck":item.get("crosscheck")})
    dates=[x["date"] for x in assets if x.get("date")]
    return {"risk_score":risk,"risk_level":level,"summary":summary,"coverage":len(scores),
            "coverage_total":len(MARKET_ANALYSIS_SYMBOLS),"scored_indices":scores,"assets":assets,
            "includes_intraday":any(bool(x.get("captured_open")) for x in core),
            "updated":max(dates) if dates else None,
            "method":"三個指數（台灣加權、費城半導體、標普 500）等權平均；收盤低於 MA60 +25、MA20 低於 MA60 +20、RSI14<40 +20、MACD 柱狀體<0 +20、20 日動能<0 +15。至少 2 個有效指數才計分。"}


def foreign_flow(previous: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return foreign-investor net flow history and explicit source freshness."""
    fetched_at = dt.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    try:
        response = retry(
            requests.get,
            "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&response=json",
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("stat") not in (None, "OK"):
            raise ValueError(f"證交所回應狀態：{payload.get('stat')}")
        rows = payload.get("data") or []
        row = next(r for r in rows if str(r[0]).startswith("外資及陸資(不含外資自營商)"))
        net = round(int(str(row[3]).replace(",", "")) / 1e8, 1)
        raw_date = str(payload.get("date", ""))
        source_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date
        if not source_date:
            raise ValueError("證交所資料缺少日期")
        history = [x for x in previous if x.get("date") != source_date]
        history.append({"date": source_date, "net_yi": net})
        history = sorted(history, key=lambda x: x.get("date", ""))[-20:]
        return history, {"status": "success", "message": "最新抓取成功", "source_date": source_date, "checked_at": fetched_at}
    except Exception as exc:
        if previous:
            source_date = previous[-1].get("date")
            return previous[-20:], {
                "status": "stale", "message": f"本次抓取失敗，沿用舊資料：{exc}",
                "source_date": source_date, "checked_at": fetched_at,
            }
        return [], {
            "status": "unavailable", "message": f"目前無可用外資資料：{exc}",
            "source_date": None, "checked_at": fetched_at,
        }


def load_previous() -> dict[str, Any]:
    try:
        with open(OUT, encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def crosscheck_close(item: dict[str, Any], turnover: dict[str, Any] | None) -> dict[str, Any]:
    """加權指數：拿 Yahoo 收盤價和證交所公布的收盤比對（只在收盤後、日期相同時比）。差距超過 0.05% 標示異常。"""
    if item.get("error") or item.get("stale") or not turnover or turnover.get("index_close") is None:
        return {"status": "unavailable"}
    if item.get("captured_open") or item.get("yahoo_date") != turnover.get("date") or item.get("yahoo_close") is None:
        return {"status": "pending"}  # Yahoo 與證交所資料尚未到同一交易日
    official = float(turnover["index_close"])
    diff = (float(item["yahoo_close"]) / official - 1) * 100
    return {"status": "ok" if abs(diff) <= 0.05 else "mismatch", "official": round(official, 2),
            "diff_pct": round(diff, 3), "date": item.get("date")}


def should_fetch(old: dict[str, Any] | None, kind: str, mkt_open: bool, manual: bool, post_close: bool) -> bool:
    """盤中只抓開盤中的市場；剛收盤的市場補抓一次最終價；其餘沿用上次資料。沒有休市日曆，休市日照常當作開盤處理。"""
    if manual or not old or old.get("error") or old.get("stale") or old.get("close") is None:
        return True
    if mkt_open or old.get("captured_open", True):  # 舊資料沒有標記時，視為盤中抓的，補抓一次
        return True
    if kind == "tw" and post_close and old.get("volume_status") != "有效":  # 收盤後補上成交金額量比
        return True
    return False


def main(now_utc: dt.datetime | None = None) -> None:
    previous = load_previous()
    old_by_symbol = {x.get("sym"): x for x in previous.get("items", []) if isinstance(x, dict)}
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    now = now_utc.astimezone(TZ_TAIPEI)
    today = now.strftime("%Y-%m-%d")
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    post_close = now.weekday() < 5 and now.time() >= dt.time(14, 0)
    pre_market = 5 <= now.hour < 9

    def need(prev_date: str | None, allowed: bool) -> bool:
        return manual or not prev_date or (allowed and prev_date != today)

    prev_turnover = previous.get("turnover")
    turnover_due = need((prev_turnover or {}).get("date"), post_close)
    fetched_turnover = twse_turnover() if turnover_due else prev_turnover
    turnover = fetched_turnover or prev_turnover
    close_official_today = bool(turnover and turnover.get("date") == today)
    close_fetch_failed = bool(turnover_due and not close_official_today)

    hist0 = previous.get("fut_hist", {})
    # 期交所資料於下一個交易日早上之後才公布，時間不固定（曾延到下午）。
    # 不設查詢時段：只要手上資料還沒追上預期日期，每次排程都查一次（一個請求）；追上後就不再查。
    fut_hist, fut_items = update_futures(
        hist0, manual or futures_fetch_due(hist0, now), now=now,
        previous_items={
            x.get("sym"): x for x in previous.get("items", [])
            if isinstance(x, dict) and x.get("sym") in ("TX", "TXALL")
        },
    )

    prev_foreign = previous.get("foreign", [])
    if need(prev_foreign[-1].get("date") if prev_foreign else None, post_close):
        foreign, foreign_meta = foreign_flow(prev_foreign)
    else:
        foreign = prev_foreign
        foreign_meta = previous.get("foreign_meta") or {
            "status": "unavailable", "message": "尚無可用外資資料",
            "source_date": None, "checked_at": now.strftime("%Y-%m-%d %H:%M"),
        }

    print(f"[交易日曆] {tw_calendar().source}；臨時休市 {len(TW_EXTRA_CLOSED)} 天")
    for message in calendar_warnings(fut_items[0].get("expected_date") if fut_items else None):
        print(f"::warning title=交易日曆::{message}")
    for fi in fut_items:
        print(futures_log_line(fi))
        if fi.get("contract_note"):
            print(f"[{'台指期近月' if fi.get('sym') == 'TX' else '台指期夜盤'}] {fi['contract_note']}")
        if (fi.get("days_behind") or 0) >= overdue_threshold(fi.get("expected_date")):
            print(f"::warning title=台指期過久未更新::{fi.get('status_note')}")

    items = []
    success_count = sum(1 for x in fut_items if x.get("data_status") == "success")
    waiting_names = [x.get("name") for x in fut_items if x.get("data_status") == "waiting"]
    fetched_syms: set[str] = {x["sym"] for x in fut_items if x.get("checked_this_run")}
    total = len(SYMBOLS) + len(fut_items)
    fetched_before = False
    for symbol in SYMBOLS:
        sym, name, _ = symbol
        kind = market_kind(sym)
        mkt_open = is_market_open(kind, now_utc)
        old = old_by_symbol.get(sym)
        force_twse_close_refresh = (
            sym == "^TWII" and post_close and (
                not old
                or old.get("date") != today
                or old.get("source") != "證交所官方收盤（FMTQIK）"
            )
        )
        if not should_fetch(old, kind, mkt_open, manual or force_twse_close_refresh, post_close):
            item = dict(old)
            success_count += 1
            print(f"[{sym}] 市場未開盤，沿用上次資料（{item.get('date')}）")
        else:
            if fetched_before:
                time.sleep(PAUSE)
            fetched_before = True
            fetched_syms.add(sym)
            try:
                item = analyse(*symbol, override=turnover if sym == "^TWII" else None,
                               kind=kind, is_open=mkt_open)
                item["captured_open"] = mkt_open
                item["data_status"] = "success"
                item["last_success_date"] = item.get("date")
                item["checked_at"] = now.strftime("%Y-%m-%d %H:%M")
                success_count += 1
            except Exception as exc:
                if old and old.get("date") and old.get("close") is not None:
                    item = dict(old)
                    item["stale"] = True
                    item["data_status"] = "failed"
                    item["checked_at"] = now.strftime("%Y-%m-%d %H:%M")
                    item["last_success_date"] = old.get("date")
                    item["fetch_error"] = str(exc)
                    item["text"] = f"行情更新失敗，顯示舊資料（最後成功日期：{old['date']}）"
                else:
                    item = {"sym": sym, "name": name, "error": str(exc),
                            "stale": True, "data_status": "failed",
                            "checked_at": now.strftime("%Y-%m-%d %H:%M")}
                print(f"[{sym}] 更新失敗：{exc}")

        if sym == "^TWII":
            item["crosscheck"] = crosscheck_close(item, turnover)
            item["close_status"] = "success" if close_official_today else (
                "failed" if close_fetch_failed else "pending")
            item["close_checked_at"] = now.strftime("%Y-%m-%d %H:%M") if turnover_due else item.get("close_checked_at")
            if close_official_today:
                official_close = float(turnover["index_close"])
                if item.get("date") != today or item.get("close") is None or abs(float(item.get("close", 0)) - official_close) > 0.005:
                    old_technical_date = item.get("date")
                    item["technical_stale"] = True
                    item["technical_date"] = old_technical_date
                    item.setdefault("name", "台灣加權指數")
                    item.setdefault("spark", [])
                    item.setdefault("support", None)
                    item.setdefault("dist", None)
                    item.setdefault("vol_ratio", None)
                    item.setdefault("volume_status", "技術分析資料不足")
                    item["close"] = round(official_close, 2)
                    item["date"] = today
                    previous_close = turnover.get("previous_index_close")
                    item["chg"] = round((official_close / float(previous_close) - 1) * 100, 2) if previous_close else None
                    item["stale"] = False
                    item.pop("error", None)
                    item["level"] = item.get("level") or "ok"
                    item["text"] = "證交所正式收盤價已取得；技術指標沿用舊資料" if item["technical_stale"] else "證交所正式收盤價"
                    item["last_success_date"] = today
                    item["data_status"] = "partial"
                    item["price_source"] = "證交所官方收盤（FMTQIK）"
                    item["price_time"] = today
                item["source"] = "證交所官方收盤（FMTQIK）"
                item["close_status_note"] = (
                    f"正式收盤價資料日期：{today}；" +
                    (f"技術指標仍使用 {item.get('technical_date')} 的資料" if item.get("technical_stale") else "已使用官方收盤價更新技術分析")
                )
                item["live_status"] = "closed"
            elif post_close:
                item["close_status_note"] = (
                    f"證交所當日正式收盤資料尚未取得；目前保留最後成功資料（{item.get('date', '未知')}）"
                )
                item["live_status"] = "close_pending"

            # 每 30 分鐘工作流程執行時，另取證交所盤中指數；不覆寫日線技術分析。
            if mkt_open:
                try:
                    live = fetch_twse_intraday(now)
                    previous_close = resolve_previous_close(live, turnover, old, item, today)
                    live_chg = ((live["price"] / previous_close) - 1) * 100 if previous_close else None
                    item.update({
                        "live_price": round(live["price"], 2),
                        "live_chg": round(live_chg, 2) if live_chg is not None else None,
                        "live_date": live["date"], "live_time": live["time"],
                        "live_source": live["source"], "live_checked_at": live["checked_at"],
                        "live_status": "success",
                    })
                except Exception as exc:
                    old_live = old or {}
                    if old_live.get("live_date") == today and old_live.get("live_price") is not None:
                        for key in ("live_price", "live_chg", "live_date", "live_time", "live_source", "live_checked_at"):
                            if key in old_live:
                                item[key] = old_live[key]
                    if "MIS 指數日期不符" in str(exc):
                        item["live_status"] = "休市或無新資料"
                        item["live_error"] = ""
                    else:
                        item["live_status"] = "failed"
                        item["live_error"] = str(exc)
                    item["live_checked_at"] = now.strftime("%Y-%m-%d %H:%M")
                    print(f"[TWSE intraday] {item['live_status']}：{exc}")

        items.append(item)
        if sym == "^TWII":
            items.extend(fut_items)

    for it in items:
        if not it.get("error") and it.get("sym") not in ("TX", "TXALL"):
            it.update(session_fields(it.get("sym", ""), it.get("date"), now_utc))
        it["links"] = LINKS.get(it.get("sym"))

    market_analysis = build_market_analysis(items)
    data = {
        "market_analysis": market_analysis,
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        # 「尚未公布」不是失敗：官方本來就還沒出資料，不應觸發部分失敗警示。
        "update_status": overall_update_status(success_count, len(waiting_names), total),
        "successful_items": success_count,
        "waiting_items": len(waiting_names),
        "total_items": total,
        "items": items,
        "foreign": foreign,
        "foreign_meta": foreign_meta,
        "turnover": turnover,
        "fut_hist": fut_hist,
        "fut_web": {},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    temp_out = OUT + ".tmp"
    with open(temp_out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_out, OUT)
    problems = [x.get("name") or x.get("sym") for x in items if x.get("stale") or x.get("error")]
    detail = "；".join(part for part in (
        f"等待官方公布：{'、'.join(waiting_names)}" if waiting_names else "",
        f"失敗或過舊：{'、'.join(problems)}" if problems else "",
    ) if part)
    print(f"完成 {data['updated']}；行情成功 {success_count}/{total}"
          f"{f'（{detail}）' if detail else ''}；外資狀態 {foreign_meta.get('status')}")
    write_step_summary(items, fetched_syms, data)


def overall_update_status(success: int, waiting: int, total: int) -> str:
    """整體狀態。「尚未公布」不是失敗，不觸發部分失敗警示；
    但完全沒有任何一項是最新資料（success == 0）時，一律視為 failed，不會因為「都在等待」而顯示成功。"""
    if success == 0:
        return "failed"
    return "success" if success + waiting == total else "partial"


STATUS_ICON = {"success": "✓", "partial": "✓", "waiting": "⏳", "overdue": "⚠", "failed": "✗", "unavailable": "—"}


def step_summary_markdown(items: list[dict[str, Any]], fetched: set[str], data: dict[str, Any]) -> str:
    """GitHub Actions 執行頁的摘要表；不用展開日誌就能看出每個標的的狀況。"""
    def cell(value: Any) -> str:
        return str(value if value not in (None, "") else "").replace("|", "／").replace("\n", " ")[:160]

    lines = [
        f"### 市場資料更新 {data.get('updated')}（台灣時間）",
        "",
        f"行情成功 {data.get('successful_items')}/{data.get('total_items')}"
        f"，等待官方公布 {data.get('waiting_items', 0)}，整體狀態 `{data.get('update_status')}`",
        "",
        f"交易日曆：{tw_calendar().source}",
        "",
        "| 標的 | 狀態 | 資料日期 | 本次是否查詢 | 說明 |",
        "|---|---|---|---|---|",
    ]
    for it in items:
        status = it.get("data_status") or ("failed" if it.get("error") or it.get("stale") else "success")
        if it.get("sym") in ("TX", "TXALL"):
            note = it.get("status_note") or ""
        elif it.get("stale") or it.get("error"):
            note = it.get("fetch_error") or it.get("error") or ""
        elif it.get("sym") not in fetched:
            note = "市場未開盤，沿用上次資料"
        else:
            note = ""
        lines.append("| " + " | ".join(cell(v) for v in (
            it.get("name") or it.get("sym"), STATUS_ICON.get(status, status), it.get("date") or "—",
            "是" if it.get("sym") in fetched else "否", note,
        )) + " |")
    return "\n".join(lines) + "\n"


def write_step_summary(items: list[dict[str, Any]], fetched: set[str], data: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(step_summary_markdown(items, fetched, data))
    except OSError as exc:
        print(f"寫入 Job Summary 失敗：{exc}")



if __name__ == "__main__":
    main()
