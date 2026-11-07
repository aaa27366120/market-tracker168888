"""Fetch market data, calculate neutral indicators, and write docs/data.json.

This project is an information dashboard, not investment advice.  A successful
workflow run is not treated as proof that every source returned fresh data.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
from typing import Any

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


def analyse(sym: str, name: str, has_vol: bool) -> dict[str, Any]:
    errors = []
    df = None
    for source in (from_yahoo, from_stooq):
        if source is from_stooq and sym not in STOOQ:
            continue
        try:
            candidate = source(sym)
            if candidate is not None and len(candidate) >= SUPPORT_DAYS + 2:
                df = candidate
                break
            errors.append(f"{source.__name__}: 歷史資料不足")
        except Exception as exc:  # keep trying the fallback source
            errors.append(f"{source.__name__}: {exc}")

    if df is None:
        raise RuntimeError("；".join(errors) or "沒有可用行情來源")

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
    vol_ratio = None
    if has_vol and "Volume" in df:
        volumes = pd.to_numeric(df["Volume"], errors="coerce")
        current_volume = volumes.iloc[-1]
        prior_volumes = volumes.iloc[-21:-1].where(volumes.iloc[-21:-1] > 0).dropna()
        if pd.notna(current_volume) and current_volume > 0 and not prior_volumes.empty:
            avg_volume = float(prior_volumes.mean())
            if avg_volume > 0:
                vol_ratio = float(current_volume / avg_volume)

    if close < support:
        text, level = "收盤低於近 60 日支撐，留意後續是否站回", "break"
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

    return {
        "sym": sym, "name": name,
        "date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "chg": round((close / previous_close - 1) * 100, 2),
        "support": round(support, 2), "dist": round(dist, 1),
        "ma60": round(ma60, 2), "vs_ma60": round((close / ma60 - 1) * 100, 1),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
        "volume_status": "不適用" if not has_vol else "有效" if vol_ratio is not None else "無有效成交量資料",
        "drawdown": round(drawdown, 1), "drawdown_status": drawdown_status,
        "text": text, "level": level,
        "spark": [round(float(x), 2) for x in df["Close"].tail(60)],
        "stale": False,
    }


def foreign_flow(previous: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return foreign-investor net flow history and explicit source freshness."""
    fetched_at = dt.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    try:
        response = requests.get(
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


def main() -> None:
    previous = load_previous()
    old_by_symbol = {x.get("sym"): x for x in previous.get("items", []) if isinstance(x, dict)}
    items = []
    success_count = 0
    for symbol in SYMBOLS:
        sym, name, _ = symbol
        try:
            item = analyse(*symbol)
            success_count += 1
        except Exception as exc:
            old = old_by_symbol.get(sym)
            if old and old.get("date") and old.get("close") is not None:
                item = dict(old)
                item["stale"] = True
                item["fetch_error"] = str(exc)
                item["text"] = f"行情更新失敗，顯示舊資料（{item['date']}）"
            else:
                item = {"sym": sym, "name": name, "error": str(exc), "stale": True}
            print(f"[{sym}] 更新失敗：{exc}")
        items.append(item)

    foreign, foreign_meta = foreign_flow(previous.get("foreign", []))
    now = dt.datetime.now(TZ_TAIPEI)
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "update_status": "success" if success_count == len(SYMBOLS) else "partial" if success_count else "failed",
        "successful_items": success_count,
        "total_items": len(SYMBOLS),
        "items": items,
        "foreign": foreign,
        "foreign_meta": foreign_meta,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    temp_out = OUT + ".tmp"
    with open(temp_out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_out, OUT)
    print(f"完成 {data['updated']}；行情成功 {success_count}/{len(SYMBOLS)}；外資狀態 {foreign_meta['status']}")


if __name__ == "__main__":
    main()
