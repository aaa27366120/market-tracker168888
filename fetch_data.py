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
    session = requests.Session()
    try:
        try:
            landing = retry(
                session.get, "https://mis.twse.com.tw/stock/index.jsp",
                headers=headers, timeout=20,
            )
            try:
                landing.raise_for_status()
            except Exception as exc:
                raise RuntimeError(
                    f"MIS 首頁 cookie 請求失敗：{type(exc).__name__}: {exc}；"
                    f"{_http_response_diagnostic(landing)}"
                ) from exc
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
            return {**quote, "source": "證交所 MIS 即時指數", "checked_at": now.strftime("%Y-%m-%d %H:%M")}
        except Exception as exc:
            errors.append(f"MIS：{type(exc).__name__}: {exc}")
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


def tx_rows() -> dict[str, dict[str, dict[str, Any]]]:
    """{YYYYMMDD: {"day": 近月一般盤, "night": 近月盤後}}"""
    resp = retry(requests.get, FUT_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    by: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if pick(r, "Contract", "契約") not in ("TX", "臺股期貨"):
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
        cur = by.setdefault(d, {}).get(k)
        if cur is None or m < cur["m"]:
            by[d][k] = row
    if not by:
        print("台指期：沒解析到資料，第一列欄位為", list(rows[0].keys()) if rows else "空")
    return by


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
            })
        if night and night.get("last") is not None:
            out["TXALL"].append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "m": night["m"], "close": night["last"], "oi": night.get("oi"),
                "chg_pct": night.get("chg_pct"), "high": night.get("high"),
                "low": night.get("low"), "vol": night.get("vol"), "session": "night",
            })
    return out



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


def quote_age_hours(qt: str, now: dt.datetime | None) -> float:
    if now is None:
        return 0.0
    return (now.replace(tzinfo=None) - dt.datetime.strptime(qt, "%Y-%m-%d %H:%M")).total_seconds() / 3600


def fut_item(sym: str, name: str, hist: list[dict[str, Any]], live: tuple | None, ok: bool,
             web: dict[str, Any] | None = None, now: dt.datetime | None = None,
             freshness_error: str | None = None) -> dict[str, Any]:
    if not hist:
        return {
            "sym": sym, "name": name,
            "error": freshness_error or "尚無可用的期交所官方資料",
            "fetch_error": freshness_error,
            "stale": True,
            "data_status": "failed" if freshness_error else "unavailable",
        }
    last = hist[-1]
    prior = hist[:-1][-SUPPORT_DAYS:]
    lows = [x["low"] for x in prior if x.get("low") is not None]
    vols = [x["vol"] for x in prior[-20:] if x.get("vol")]
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
        "volume_status": "累積中", "level": "ok", "stale": not ok,
        "history_days": len(hist), "spark": [x["close"] for x in hist[-SUPPORT_DAYS:]],
        "price_source": "期交所官方・夜盤" if is_night else "期交所官方・日盤",
        "price_time": last["date"], "session": session_text,
        "session_class": "open" if session_text == "夜盤時段" else "closed",
        "checked_at": now.strftime("%Y-%m-%d %H:%M") if now is not None else None,
        "data_status": "success" if ok else "failed",
        "last_success_date": last["date"],
    }
    if len(lows) >= 5:
        item["support"] = min(lows)
        item["dist"] = round((last["close"] / item["support"] - 1) * 100, 1)
        if vols and last.get("vol") and not item["stale"]:
            item["vol_ratio"] = round(last["vol"] / (sum(vols) / len(vols)), 2)
            item["volume_status"] = "有效"
        elif item["stale"]:
            item["volume_status"] = "資料更新失敗"
        else:
            item["volume_status"] = "盤中不顯示"
        item["text"], item["level"] = judge(last["close"], item["support"], item["dist"], item["vol_ratio"])
    else:
        item["text"] = f"歷史資料累積中（{len(hist)}/60 天）"
    if not ok:
        item["text"] = f"期交所資料更新失敗，顯示舊資料（最後成功日期：{last['date']}）"
        item["fetch_error"] = freshness_error or "本次期交所官方資料未成功取得"
    if is_night:
        item["session_note"] = (
            "此卡只顯示期交所已公布的最近夜盤資料，不含日盤；"
            "資料日期是交易資料日期，擷取時間另列，夜盤交易中不代表即時逐筆報價。"
        )
    return item


def _previous_weekday(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def expected_futures_dates(now: dt.datetime) -> dict[str, str] | None:
    """依期交所每日行情 API 的資料發布時點判斷日盤、夜盤預期日期。

    期交所 API 約在日盤結束後（下午）才一起放出當天的日盤與夜盤資料；
    夜盤日期是交易量的「歸屬日」，不是夜盤開盤的日曆日期。
    因此早上 07:00–09:00 窗口，日盤與夜盤預期日期相同，都是前一個工作日。
    16:00 後則預期當天（週末回推至前一個工作日）。休市日曆尚未建模。
    """
    local = now.astimezone(TZ_TAIPEI)
    if local.time() < dt.time(7, 0):
        return None
    today = local.date()
    if local.time() >= dt.time(16, 0):
        session_date = today if today.weekday() < 5 else _previous_weekday(today)
        day_date = night_date = session_date
    elif dt.time(7, 0) <= local.time() < dt.time(9, 0):
        day_date = night_date = _previous_weekday(today)
    else:
        return None
    return {"TX": day_date.isoformat(), "TXALL": night_date.isoformat()}


def update_futures(hist: dict[str, Any], fetch: bool = True,
                   web: dict[str, Any] | None = None, now: dt.datetime | None = None,
                   previous_items: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, list], list[dict[str, Any]]]:
    hist = {k: list(v) for k, v in (hist or {}).items()}
    # 舊版 TXALL 是日夜盤混合值，沒有 session 標記，不能再當作夜盤歷史。
    hist["TXALL"] = [x for x in hist.get("TXALL", []) if x.get("session") == "night"]
    records: dict[str, list[dict[str, Any]]] = {"TX": [], "TXALL": []}
    errors: dict[str, str | None] = {"TX": None, "TXALL": None}
    try:
        by = tx_rows() if fetch else {}
        if fetch:
            records = fut_records(by) if by else {"TX": [], "TXALL": []}
        for key in ("TX", "TXALL"):
            session_records = records.get(key, [])
            if fetch and not session_records:
                errors[key] = "期交所本次回應未包含此交易時段資料"
            if session_records:
                merged = {x["date"]: x for x in hist.get(key, [])}
                merged.update({x["date"]: x for x in session_records})
                hist[key] = [merged[d] for d in sorted(merged)][-130:]
            # DailyMarketReportFut 的夜盤日期依期交所交易日歸屬規則標示。
            # 此資料約於夜盤結束後 07:00 產製；在可預期的工作日更新窗口，
            # 必須確認每個時段都已取得當日資料，不能用另一時段的成功掩蓋缺漏。
            if fetch and session_records and now is not None:
                expected = expected_futures_dates(now)
                expected_date = expected.get(key) if expected else None
                if expected_date:
                    latest_date = max(x.get("date", "") for x in session_records)
                    if latest_date < expected_date:
                        errors[key] = (
                            f"期交所{'夜盤' if key == 'TXALL' else '日盤'}資料日期為 "
                            f"{latest_date or '未知'}，預期 {expected_date}"
                        )
    except Exception as exc:
        for key in ("TX", "TXALL"):
            errors[key] = f"期交所資料更新失敗：{exc}"
        print(f"台指期更新失敗：{exc}")

    items = []
    previous_items = previous_items or {}
    for key, name in (("TX", "台指期（近月）"), ("TXALL", "台指期（全）・夜盤")):
        # 非更新時段沿用上一輪的失敗狀態；不能因 fetch=False 就把舊資料洗成成功。
        if not fetch:
            previous = previous_items.get(key) or {}
            if previous.get("stale") or previous.get("data_status") == "failed":
                errors[key] = (
                    previous.get("fetch_error")
                    or previous.get("error")
                    or "上次期交所資料更新失敗，等待下一次官方資料更新"
                )
        ok = errors[key] is None
        items.append(fut_item(
            key, name, hist.get(key, []), None, ok, None, now,
            freshness_error=errors[key],
        ))
    return hist, items



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
    core_symbols={"^TWII","^GSPC","^IXIC","^SOX"}
    core=[x for x in items if x.get("sym") in core_symbols and not x.get("stale")
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
    for item in items:
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
            "coverage_total":len(core_symbols),"scored_indices":scores,"assets":assets,
            "includes_intraday":any(bool(x.get("captured_open")) for x in core),
            "updated":max(dates) if dates else None,
            "method":"四個主要指數等權平均；收盤低於 MA60 +25、MA20 低於 MA60 +20、RSI14<40 +20、MACD 柱狀體<0 +20、20 日動能<0 +15。至少 2 個有效指數才計分。"}


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
    # 期交所每日行情約於日盤收盤後 16:00、夜盤收盤後 07:00 產製；
    # 14:07 / 06:07 的排程早於資料產製時間，不應以舊回應覆蓋新鮮度狀態。
    fut_allowed = (
        (now.weekday() < 5 and now.time() >= dt.time(16, 0))
        or (now.weekday() <= 5 and dt.time(7, 0) <= now.time() < dt.time(9, 0))
    )
    # 日盤與夜盤分別檢查日期；其中一個時段更新成功，不代表另一個時段也已更新。
    fut_hist, fut_items = update_futures(
        hist0, manual or fut_allowed, now=now,
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

    items = []
    success_count = sum(1 for x in fut_items if not x.get("error") and not x.get("stale"))
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
                    previous_close = None
                    if turnover and turnover.get("date") == today:
                        previous_close = turnover.get("previous_index_close")
                    if previous_close is None and old and old.get("date") < today:
                        previous_close = old.get("close")
                    if previous_close is None and item.get("date") < today:
                        previous_close = item.get("close")
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
        "update_status": "success" if success_count == total else "partial" if success_count else "failed",
        "successful_items": success_count,
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
    print(f"完成 {data['updated']}；行情成功 {success_count}/{total}；外資狀態 {foreign_meta.get('status')}")



if __name__ == "__main__":
    main()
